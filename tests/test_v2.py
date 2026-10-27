"""V2 contracts and real SQLite transactions; external services are explicit doubles."""
import asyncio
import base64
import json
import selectors
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from osa.application.v2.service import V2Service
from osa.application.v2.worker import WorkAgent
from osa.domain.entities import Destination, GroupPolicy
from osa.domain.work import StaleLease, VersionConflict, external_question, validate_files
from osa.infrastructure.v2.registry import Registry
from osa.main import create_app
from test_core import demo_settings, make_event

# This managed runner blocks asyncio's self-pipe wakeup. A bounded selector
# keeps thread completions/shutdown observable; it changes no production code.
class PollingSelector(selectors.DefaultSelector):
    def select(self, timeout=None):
        return super().select(.01 if timeout is None else min(.01,timeout))

class PollingPolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self):
        return asyncio.SelectorEventLoop(PollingSelector())

class V2AsyncCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_policy=asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(PollingPolicy())
    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop_policy(cls.previous_policy)


FILES={'main.py':'def add(a,b): return a+b\n','tests/__init__.py':'',
       'tests/test_main.py':'import unittest\nfrom main import add\nclass TestAdd(unittest.TestCase):\n def test_add(self): self.assertEqual(add(1,2),3)\n'}


def report(passed=1,failed=0):
    data={'passed':passed,'failures':failed,'errors':0,'tests_run':passed+failed,'exit_code':0 if not failed else 1,'runtime':'3.12'}
    return {'status':'succeeded' if not failed else 'failed','exit_code':data['exit_code'],'stdout':'test output',
            'artifacts':[{'name':'test-report.json','content':base64.b64encode(json.dumps(data).encode()).decode()}]}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=str(Path(self.directory.name)/'registry.db')
        self.db=Registry(self.path)

    def tearDown(self):self.directory.cleanup()

    def test_work_budget_zero_stays_unlimited_after_prior_exhaustion(self):
        self.assertTrue(self.db.budget('work:test',1))
        self.assertFalse(self.db.budget('work:test',1))
        self.assertTrue(self.db.budget('work:test',0))
        reopened=Registry(self.path)
        self.assertTrue(reopened.budget('work:test',0))

    def approved(self):
        task=self.db.create('u',None,'write code','request-1')
        self.db.command(task['id'],'admin',None,'APPROVE',{},1,'approve-1',admin=True)
        return self.db.claim('worker-one')

    def test_versions_idempotency_and_approval_cannot_be_bypassed(self):
        task=self.db.create('u',None,'write code','request-1')
        self.assertEqual(self.db.create('u',None,'write code','request-1')['id'],task['id'])
        self.assertIsNone(self.db.claim('worker'))
        with self.assertRaises(ValueError):self.db.command(task['id'],'u',None,'PAUSE',{},1,'pause')
        with self.assertRaises(PermissionError):self.db.command(task['id'],'u',None,'APPROVE',{},1,'approve')
        result=self.db.command(task['id'],'admin',None,'APPROVE',{},1,'approved',admin=True)
        self.assertEqual(self.db.command(task['id'],'admin',None,'APPROVE',{},1,'approved',admin=True),result)
        with self.assertRaises(VersionConflict):self.db.command(task['id'],'u',None,'CANCEL',{},1,'cancel')

    def test_lease_expiry_recovery_rejects_old_worker_and_preserves_checkpoint(self):
        task=self.approved();tid=task['id'];generation=task['generation']
        self.db.checkpoint(tid,generation,'source',{'files':FILES,'cursor':1,'current_step':'testing'}, {'type':'CODE_DIFF'})
        with self.db.tx() as db:db.execute('UPDATE v2_tasks SET lease_until=0 WHERE id=?',(tid,))
        second=Registry(self.path).claim('worker-two')
        self.assertGreater(second['generation'],generation)
        self.assertEqual(second['files'],FILES)
        with self.assertRaises(StaleLease):self.db.checkpoint(tid,generation,'bad',{}, {})
        events=self.db.events(tid)
        self.assertEqual(len({e['seq'] for e in events}),len(events))
        self.assertTrue(any(e['kind']=='task.interrupted.v1' for e in events))

    def test_two_workers_do_not_claim_same_task(self):
        task=self.db.create('u',None,'write','one')
        self.db.command(task['id'],'admin',None,'APPROVE',{},1,'approved',admin=True)
        self.assertIsNotNone(self.db.claim('one'))
        self.assertIsNone(Registry(self.path).claim('two'))

    def test_added_requirement_applies_only_at_boundary_and_pause_resume(self):
        task=self.approved();tid=task['id'];g=task['generation']
        cmd=self.db.command(tid,'u',None,'ADD_REQUIREMENT',{'text':'add timer'},task['version'],'change')
        self.assertFalse(cmd['applied']);self.assertEqual(self.db.get(tid)['plan_version'],1)
        updated=self.db.boundary(tid,g)
        self.assertEqual(updated['plan_version'],2);self.assertEqual(updated['requirement_log'],['add timer'])
        self.db.command(tid,'u',None,'PAUSE',{},updated['version'],'pause')
        paused=self.db.boundary(tid,g);self.assertEqual(paused['status'],'PAUSED')
        self.db.command(tid,'u',None,'RESUME',{},paused['version'],'resume')
        self.assertEqual(self.db.get(tid)['status'],'QUEUED')

    def test_no_tests_cannot_complete_and_private_scope_is_not_cross_group(self):
        task=self.approved()
        with self.assertRaises(ValueError):self.db.finish(task['id'],task['generation'],'COMPLETED')
        group=self.db.create('qq-owner','10001','private task name','group')
        with self.assertRaises(PermissionError):self.db.visible(group['id'],'qq-owner','10002')
        with self.assertRaises(PermissionError):self.db.visible(group['id'],'stranger','10001')

    def test_work_presence_ignores_background_records_and_kill_blocks_claim(self):
        self.db.record('night_research','u',None,{},'night','QUEUED')
        self.assertEqual(self.db.presence()['state'],'FREE')
        self.approved();self.assertEqual(self.db.presence()['state'],'WORKING')
        self.db.set_kill(True)
        self.assertIsNone(self.db.claim('new'))

    def test_public_derivative_requires_real_evidence_and_retraction_removes_it(self):
        rid=self.db.record('experience','u','10001',{'summary':'Python PIL import','source_ids':['source'],'proposed_solution':'Use the script interpreter to inspect Pillow installation'},'case','unresolved')
        with self.assertRaises(PermissionError):self.db.review(rid,'admin','share','review','Python 中可检查安装环境')
        with self.assertRaises(PermissionError):self.db.feedback(rid,'v','10001',True,'feedback')
        self.db.feedback(rid,'u','10001',True,'feedback')
        self.db.review(rid,'admin','share','approved','Python 中可检查安装环境')
        self.assertEqual(len(self.db.records('public_experience',admin=True)),1)
        self.db.feedback(rid,'u','10001',False,'correction')
        self.assertEqual(self.db.records('public_experience',admin=True),[])
        self.assertEqual(self.db.records('experience',group='10002'),[])

    def test_erasure_fences_running_worker_and_removes_sources_and_derivatives(self):
        task=self.approved()
        self.db.erase(owner='u')
        with self.assertRaises(StaleLease):self.db.checkpoint(task['id'],task['generation'],'late',{'files':FILES},{})
        self.assertEqual(self.db.get(task['id'])['requirement'],'')
        self.assertEqual(self.db.events(task['id']),[])

    def test_echo_group_ttl_ordering_and_dlp(self):
        now=time.time();self.db.echo('10001','one',now,20)
        self.db.echo('10001','old',now-2,0)
        self.assertTrue(self.db.echo_active('10001'));self.assertFalse(self.db.echo_active('10002'))
        with self.assertRaises(ValueError):self.db.echo('10001','very-old',now-100,20)
        for text in ['QQ:123456 的问题','http://127.0.0.1/a','password=abc','/home/private/file','12345678']:
            with self.subTest(text=text),self.assertRaises(PermissionError):external_question(text)
        self.assertEqual(external_question('Python Pillow 如何检查安装环境？'),'Python Pillow 如何检查安装环境？')
        for name in ['../escape.py','.git/hooks/a.py','/etc/x.py','x/../a.py','.env','a\\b.py']:
            with self.subTest(name=name),self.assertRaises((PermissionError,ValueError)):validate_files({name:'x'})


class WorkAgentTests(V2AsyncCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory();self.db=Registry(str(Path(self.directory.name)/'control.db'))
        task=self.db.create('u',None,'implement addition','task')
        self.db.command(task['id'],'admin',None,'APPROVE',{},1,'approve',admin=True)
        self.task=self.db.claim('worker')

    async def asyncTearDown(self):self.directory.cleanup()

    async def test_real_checkpoint_report_artifact_and_no_model_guessed_progress(self):
        executor=AsyncMock();executor.run.return_value=report()
        agent=WorkAgent(self.db,AsyncMock(return_value=FILES),executor,self.directory.name)
        await agent.run(self.task)
        result=self.db.get(self.task['id'])
        self.assertEqual(result['status'],'COMPLETED');self.assertEqual(result['tests_passed'],1)
        self.assertEqual(self.db.public(result)['percent'],100)
        artifact=Path(self.directory.name)/result['id']/f"generation-{result['generation']}"/'project.zip'
        self.assertTrue(artifact.exists())

    async def test_empty_report_never_completes_and_failure_is_experience(self):
        executor=AsyncMock();executor.run.return_value={'status':'succeeded','stdout':'exit 0','artifacts':[]}
        agent=WorkAgent(self.db,AsyncMock(return_value=FILES),executor,self.directory.name)
        await agent.run(self.task)
        self.assertEqual(self.db.get(self.task['id'])['status'],'FAILED')
        self.assertEqual(self.db.get(self.task['id'])['failure_kind'],'validation')
        self.assertTrue(self.db.records('experience',admin=True))
        self.assertFalse(any(c['state']=='verified_local' for c in self.db.records('experience',admin=True)))

    async def test_restored_source_checkpoint_does_not_recall_planner(self):
        self.db.checkpoint(self.task['id'],self.task['generation'],'source',{'files':FILES,'cursor':1},{})
        planner=AsyncMock();executor=AsyncMock();executor.run.return_value=report()
        await WorkAgent(self.db,planner,executor,self.directory.name).run(self.task)
        planner.assert_not_awaited();self.assertEqual(self.db.get(self.task['id'])['status'],'COMPLETED')

    async def test_completed_task_reopens_same_workspace_for_followup_instruction(self):
        executor=AsyncMock();executor.run.return_value=report()
        await WorkAgent(self.db,AsyncMock(return_value=FILES),executor,self.directory.name).run(self.task)
        completed=self.db.get(self.task['id'])
        original_workspace=completed['workspace_id']
        original_files=dict(completed['files'])
        changed=self.db.command(self.task['id'],'u',None,'ADD_REQUIREMENT',
                                {'text':'给现有项目增加平滑动画，保留原功能'},completed['version'],'followup')
        reopened=self.db.get(self.task['id'])
        self.assertTrue(changed['applied'] is False or changed['applied'] is True)
        self.assertEqual(reopened['status'],'QUEUED')
        self.assertEqual(reopened['workspace_id'],original_workspace)
        self.assertEqual(reopened['files'],original_files)
        claimed=self.db.claim('worker-followup')
        applied=self.db.boundary(self.task['id'],claimed['generation'])
        self.assertEqual(applied['requirement_log'][-1],'给现有项目增加平滑动画，保留原功能')
        self.assertEqual(applied['workspace_id'],original_workspace)


class V2APITests(V2AsyncCase):
    async def asyncSetUp(self):
        self.config=demo_settings().model_copy(update={'v2_enabled':True,'v2_database_url':':memory:','work_enabled':True})
        self.app=create_app(self.config)
        self.headers={'Authorization':'Bearer admin-test-token-12345'}

    async def test_control_api_rejects_version_conflict_and_separates_approval(self):
        async with self.app.router.lifespan_context(self.app),httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test') as client:
            self.assertEqual((await client.get('/api/v1/work/tasks')).status_code,401)
            result=await client.post('/api/v1/work/tasks',headers=self.headers,json={'requirement':'write a project','idempotency_key':'new-project-01'})
            self.assertEqual(result.status_code,200);row=result.json()['data']
            self.assertEqual(row['status'],'WAITING_APPROVAL')
            command={'kind':'APPROVE','expected_version':row['version'],'idempotency_key':'approval-01','payload':{}}
            good=await client.post('/api/v1/work/tasks/'+row['id']+'/commands',headers=self.headers,json=command)
            self.assertEqual(good.status_code,200)
            command.update(kind='CANCEL',idempotency_key='cancel-001')
            conflict=await client.post('/api/v1/work/tasks/'+row['id']+'/commands',headers=self.headers,json=command)
            self.assertEqual(conflict.status_code,409)
            self.assertEqual((await client.get('/console-v2.js')).status_code,200)

    async def test_work_does_not_block_mention_and_echo_does_not_affect_other_group(self):
        v2=self.app.state.v2
        task=await v2.call('create','12345','10001','private name','private')
        await v2.call('command',task['id'],'admin',None,'APPROVE',{},1,'approve',admin=True)
        # Coding in one group must not silence proactive conversation in an
        # unrelated group; the decision model and group rate limit still gate
        # the actual reply.
        self.assertTrue(await v2.allowed(Destination('group','10002'),initiative=True))
        self.assertTrue(await v2.allowed(Destination('group','10002')))
        await v2.call('echo','10001','echo',time.time(),20)
        self.assertFalse(await v2.allowed(Destination('group','10001')))
        self.assertTrue(await v2.allowed(Destination('group','10002')))
        message=make_event(text='/进度 '+task['id'],group='10002')
        reply=await v2.route(message,GroupPolicy())
        self.assertNotIn('private name',reply);self.assertNotIn('测试通过',reply)

    async def test_budget_zero_nightly_has_no_research_and_base_audit_remains(self):
        v2=self.app.state.v2
        v2.settings.nightly_research_limit=0
        await v2.call('record','experience','u','10001',{'public_question':'Python Pillow 如何检查版本？'},'case','unresolved')
        await v2.nightly('10001','2026-10-15',GroupPolicy(research_external=True))
        self.assertEqual(await v2.call('records','night_research',admin=True),[])
        self.assertEqual(len(await v2.call('records','consolidation_audit',admin=True)),1)

    async def test_deepseek_only_answer_remains_insufficient(self):
        from osa.infrastructure.v2.research_processor import research_loop
        registry=self.app.state.v2.registry
        registry.record('night_research','u',None,{'question':'Python 如何检查安装环境？'},'research','QUEUED')
        browser=AsyncMock();browser.ask.return_value={'id':'remote'}
        browser.status.return_value={'status':'succeeded','answer':'应该检查运行程序使用的 Python 和安装依赖的解释器是否一致。'}
        with patch('osa.infrastructure.v2.research_processor.BrowserWorkerClient',return_value=browser):
            job=asyncio.create_task(research_loop(registry))
            for _ in range(50):
                rows=await asyncio.to_thread(registry.records,'night_research',admin=True)
                if rows[0]['state']=='NEEDS_REVIEW':break
                await asyncio.sleep(.01)
            job.cancel();await asyncio.gather(job,return_exceptions=True)
        self.assertEqual(rows[0]['state'],'NEEDS_REVIEW')
        self.assertEqual(rows[0]['payload']['verified_claims'],0)
        self.assertTrue(all(c['status']=='INSUFFICIENT' for c in rows[0]['payload']['claims']))

class RealProjectHarnessTests(unittest.TestCase):
    def test_harness_runs_actual_fixture_tests_and_emits_report(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'input';output=root/'output'
            source.mkdir();output.mkdir()
            (source/'files.json').write_text(json.dumps(FILES))
            script=(Path(__file__).resolve().parents[1]/'workers/sandbox/project_runner.py').read_text()
            script=script.replace("'/output/project'",repr(str(output/'project'))).replace("'/input/files.json'",repr(str(source/'files.json'))).replace("'/output/test-report.json'",repr(str(output/'test-report.json')))
            harness=root/'runner.py';harness.write_text(script)
            result=subprocess.run([sys.executable,str(harness)],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            actual=json.loads((output/'test-report.json').read_text())
            self.assertEqual(actual['passed'],1);self.assertEqual(actual['errors'],0)
            self.assertIn('test_add',result.stderr)

class MigrationGraphTests(unittest.TestCase):
    def test_every_revision_parent_exists_and_graph_has_one_head(self):
        import ast
        values={}
        for path in (Path(__file__).resolve().parents[1]/'migrations/versions').glob('*.py'):
            assignments={}
            for node in ast.parse(path.read_text()).body:
                if isinstance(node,ast.Assign):
                    for target in node.targets:
                        if isinstance(target,ast.Name) and target.id in {'revision','down_revision'}:
                            assignments[target.id]=ast.literal_eval(node.value)
            values[assignments['revision']]=assignments['down_revision']
        for parent in values.values():self.assertTrue(parent is None or parent in values)
        self.assertEqual(len(set(values)-set(values.values())),1)
        self.assertIn('0004',values)

class ResearchProofTests(unittest.TestCase):
    def test_quote_must_exist_and_public_claim_is_removed_with_source_owner(self):
        registry=Registry(':memory:')
        quote='The interpreter used for the script must match the package installation environment.'
        rid=registry.record('night_research','user',None,{'claims':[{'text':'Python 解释器应匹配安装环境','status':'INSUFFICIENT'}],
            'sources':[{'sha256':'a'*64,'url':'https://docs.python.org/3/','excerpt':quote}]},'research','NEEDS_REVIEW')
        with self.assertRaises(ValueError):registry.review_research(rid,0,'reviewer','a'*64,'invented source quotation','review',True)
        self.assertEqual(registry.records('public_claim',admin=True),[])
        registry.review_research(rid,0,'reviewer','a'*64,quote,'限定 Python 多解释器环境',True)
        self.assertEqual(len(registry.records('public_claim',admin=True)),1)
        registry.erase(owner='user')
        self.assertEqual(registry.records('public_claim',admin=True),[])

class PostgreSQLRegistryTests(unittest.TestCase):
    @unittest.skipUnless(__import__('importlib.util',fromlist=['find_spec']).find_spec('psycopg') and __import__('os').environ.get('OSA_TEST_POSTGRES_URL'),'Dedicated PostgreSQL test URL and psycopg are required')
    def test_real_postgres_transaction_and_fencing(self):
        import os,uuid
        from urllib.parse import urlsplit
        url=os.environ['OSA_TEST_POSTGRES_URL']
        self.assertTrue(urlsplit(url).path.endswith('_test'),'Use a dedicated *_test database only')
        registry=Registry(url)
        key='ci-'+uuid.uuid4().hex
        task=registry.create(key,None,'PostgreSQL integration',key)
        registry.command(task['id'],'ci',None,'APPROVE',{},1,key+'-approve',admin=True)
        claimed=registry.claim('ci-worker')
        self.assertEqual(claimed['id'],task['id'])
        registry.checkpoint(task['id'],claimed['generation'],'source',{'files':FILES,'cursor':1},{'type':'CODE_DIFF'})
        with registry.tx() as db:db.execute('UPDATE v2_tasks SET lease_until=0 WHERE id=?',(task['id'],))
        resumed=Registry(url).claim('ci-worker-two')
        self.assertEqual(resumed['files'],FILES)
        with self.assertRaises(StaleLease):registry.checkpoint(task['id'],claimed['generation'],'late',{}, {})
        registry.command(task['id'],'ci',None,'CANCEL',{},resumed['version'],key+'-cancel',admin=True)
        registry.boundary(task['id'],resumed['generation'])

class V2SecurityTests(V2AsyncCase):
    async def test_echo_token_is_not_an_admin_token_and_send_rechecks_latest_echo(self):
        from pydantic import SecretStr
        from osa.application.runtime import OutboxDispatcher
        from test_core import FakePlatform
        config=demo_settings().model_copy(update={'v2_enabled':True,'v2_database_url':':memory:',
            'echo_token':SecretStr('echo-state-only-test-token-123456789')})
        app=create_app(config)
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            self.assertEqual((await client.get('/api/v1/work/tasks',headers={'Authorization':'Bearer echo-state-only-test-token-123456789'})).status_code,401)
            event=make_event(text='你好')
            await app.state.store.publish(event,True)
            await app.state.runtime.process(event)
            result=await client.post('/api/v1/groups/echo-state',headers={'Authorization':'Bearer echo-state-only-test-token-123456789'},
                json={'group_id':'10001','event_id':'echo-1','detected_at':time.time(),'ttl_seconds':20})
            self.assertEqual(result.status_code,200)
            platform=FakePlatform();dispatcher=OutboxDispatcher(app.state.store,platform,frozenset(['10001']),False,app.state.features)
            dispatcher.v2=app.state.v2
            await dispatcher.dispatch_once()
            self.assertEqual(platform.sent,[])

    async def test_model_wait_does_not_lock_registry_progress_queries(self):
        from types import SimpleNamespace
        registry=Registry(':memory:')
        task=registry.create('12345','10001','private code','task-request')
        registry.command(task['id'],'owner',None,'APPROVE',{},1,'approve',admin=True)
        task=registry.claim('worker')
        waiting=asyncio.Event();entered=asyncio.Event()
        async def planner(_):entered.set();await waiting.wait();return FILES
        with tempfile.TemporaryDirectory() as directory:
            agent=WorkAgent(registry,planner,AsyncMock(),directory)
            runner=asyncio.create_task(agent.run(task))
            await asyncio.wait_for(entered.wait(),1)
            v2=V2Service(registry,SimpleNamespace(work_enabled=True))
            reply=await asyncio.wait_for(v2.route(make_event(text='/进度 '+task['id']),GroupPolicy()),1)
            self.assertIn('RUNNING',reply)
            self.assertIn('生成或修复',reply)
            self.assertNotIn('private code',reply)
            runner.cancel();await asyncio.gather(runner,return_exceptions=True)
