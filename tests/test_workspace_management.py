import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from osa.application.runtime import OutboxDispatcher
from osa.application.v2.service import V2Service
from osa.application.v2.worker import WorkAgent
from osa.application.v3.service import V3Service
from osa.domain.entities import GroupPolicy, ModelResult
from osa.domain.work import VersionConflict, delivery_filename
from osa.infrastructure.memory_store import InMemoryStore
from osa.infrastructure.v2.worker import worker_cycle
from osa.infrastructure.v3.registry import Registry
from osa.main import create_app
from test_core import demo_settings, make_event, FakePlatform
from test_v2 import FILES, report


class WorkspaceManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name)/'control.db')
        self.registry = Registry(self.path)

    async def asyncTearDown(self):
        self.folder.cleanup()

    def running_task(self, source=None):
        task=self.registry.create('12345','10001','compute a hash','test',source)
        self.registry.command(task['id'],'admin',None,'APPROVE',{},1,'approve',admin=True)
        return self.registry.claim('worker')

    def test_read_does_not_request_a_sqlite_write_lock(self):
        task=self.registry.create('owner',None,'test','read-lock')
        writer=sqlite3.connect(self.path,isolation_level=None)
        try:
            writer.execute('BEGIN IMMEDIATE')
            self.assertEqual(self.registry.get(task['id'])['id'],task['id'])
            self.assertEqual(len(self.registry.tasks(admin=True)),1)
            self.assertIsNone(self.registry.binding(task['id']))
        finally:
            writer.rollback();writer.close()

    def test_queued_and_cancelled_tasks_have_matching_step_labels(self):
        task=self.registry.create('owner',None,'test','queue-label')
        self.registry.command(task['id'],'admin',None,'APPROVE',{},1,'queue',admin=True)
        queued=self.registry.get(task['id'])
        self.assertEqual(queued['current_step'],'等待工作进程执行')
        self.registry.command(task['id'],'admin',None,'CANCEL',{},queued['version'],'cancel',admin=True)
        self.assertEqual(self.registry.get(task['id'])['current_step'],'任务已取消')

    async def test_real_error_is_failed_and_worker_survives_transient_lock(self):
        task=self.running_task()
        executor=SimpleNamespace(run=AsyncMock(),cancel=AsyncMock())
        await WorkAgent(self.registry,AsyncMock(side_effect=sqlite3.OperationalError('database is locked')),executor,self.folder.name).run(task)
        failed=self.registry.get(task['id'])
        self.assertEqual(failed['status'],'FAILED')
        self.assertEqual(failed['failure_kind'],'infrastructure')
        self.registry.command(task['id'],'admin',None,'RESUME',{},failed['version'],'retry',admin=True)
        self.assertEqual(self.registry.get(task['id'])['status'],'QUEUED')
        adapter=SimpleNamespace(maintenance=AsyncMock(side_effect=[sqlite3.OperationalError('locked'),None]))
        agent=SimpleNamespace(run=AsyncMock())
        await worker_cycle(self.registry,agent,adapter,False)
        await worker_cycle(self.registry,agent,adapter,False)
        self.assertEqual(adapter.maintenance.await_count,2)

    def test_legacy_exceptions_are_reclassified_without_queuing_or_losing_files(self):
        task=self.running_task()
        self.registry.checkpoint(task['id'],task['generation'],'saved',{'files':FILES},{})
        self.registry.finish(task['id'],task['generation'],'WAITING_INPUT','OperationalError: database is locked')
        self.assertEqual(self.registry.reconcile_legacy_failures(),1)
        final=self.registry.get(task['id'])
        self.assertEqual(final['status'],'FAILED');self.assertEqual(final['files'],FILES)
        self.assertIsNone(self.registry.claim('worker'))
        self.assertEqual(self.registry.reconcile_legacy_failures(),0)

    async def test_archive_api_preserves_project_and_blocks_active_or_stale_deletion(self):
        settings=demo_settings().model_copy(update={'v2_database_url':self.path,'workspace_dir':self.folder.name,'work_enabled':True})
        app=create_app(settings)
        db=app.state.v2.registry
        task=self.running_task()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test',headers={'Authorization':'Bearer admin-test-token-12345'}) as client:
            self.assertEqual((await client.delete('/api/v1/coding/tasks/'+task['id'])).status_code,409)
            self.registry.finish(task['id'],task['generation'],'FAILED','synthetic failure',failure_kind='infrastructure')
            latest=self.registry.get(task['id'])
            self.assertEqual((await client.delete('/api/v1/coding/tasks/'+task['id']+'?expected_version=1')).status_code,409)
            removed=await client.delete('/api/v1/coding/tasks/'+task['id']+'?expected_version='+str(latest['version']))
            self.assertEqual(removed.status_code,200)
            self.assertEqual((await client.get('/api/v1/coding/tasks')).json()['data'],[])
            self.assertTrue(db.get(task['id'])['archived_at'])
            with self.assertRaises(ValueError):db.command(task['id'],'admin',None,'RESUME',{},latest['version']+1,'archived-retry',admin=True)

    def test_timeline_returns_latest_events_in_chronological_order(self):
        for i in range(205):self.registry.publish('t',None,'coding.progress.updated','key'+str(i),{'number':i})
        rows=self.registry.domain_events('t',latest=True)
        self.assertEqual(len(rows),200)
        self.assertEqual([r['payload']['number'] for r in rows],list(range(5,205)))

    async def completed_delivery(self, selected=None, renames=None):
        store=InMemoryStore();await store.initialize(['10001'])
        await store.update_policy('10001',GroupPolicy(sandbox=True,min_send_interval=0))
        event=make_event();await store.publish(event,True)
        context={'actor_id':event.actor_id,'group_id':'10001','source_id':event.id,'source_event':event.to_dict(),
                 'evidence_ids':[],'dependency_snapshot':{'source_ids':[event.id]}}
        self.registry.begin_thinking('parent',context)
        task=self.running_task(event.to_dict());self.registry.attach_parent(task['id'],'parent')
        async def executed(*args):
            self.assertEqual(self.registry.get(task['id'])['status'],'VALIDATING')
            value=report();value['stdout']='SHA512=verified-execution-output'
            return value
        executor=SimpleNamespace(run=AsyncMock(side_effect=executed),cancel=AsyncMock())
        await WorkAgent(self.registry,AsyncMock(return_value=FILES),executor,self.folder.name).run(task)
        completed=self.registry.get(task['id'])
        self.assertEqual(completed['status'],'COMPLETED')
        self.assertTrue((Path(self.folder.name)/task['id']/'generation-1'/'project.zip').is_file())
        brief={'key_points':['SHA512=verified-execution-output'],'artifact_names':selected or [],'artifact_renames':renames or {}}
        model=SimpleNamespace(generate=AsyncMock(side_effect=[ModelResult(json.dumps({'final_action':'REPLY','reply_brief':brief}),'test','m'),ModelResult('SHA512=verified-execution-output','test','m')]))
        settings=demo_settings().model_copy(update={'work_enabled':True,'workspace_dir':self.folder.name})
        v2=V2Service(self.registry,settings)
        service=V3Service(settings,model,v2,store)
        self.assertTrue(await service.resume_once())
        first_prompt=model.generate.await_args_list[0].args[0]
        self.assertIn('verified-execution-output',json.dumps(first_prompt))
        platform=FakePlatform();platform.send_file=AsyncMock()
        dispatcher=OutboxDispatcher(store,platform,frozenset(['10001']))
        dispatcher.v2=v2
        self.assertTrue(await dispatcher.dispatch_once())
        self.assertTrue(platform.sent)
        return platform,store

    async def test_calculation_completion_sends_text_without_any_file(self):
        platform,store=await self.completed_delivery()
        platform.send_file.assert_not_awaited()
        snapshot=next(iter(store.decisions.values()))['snapshot']
        self.assertFalse(snapshot.get('deliver_work_file'))

    async def test_selected_zip_is_sent_under_coordinator_name(self):
        platform,_=await self.completed_delivery(['project.zip'],{'project.zip':'计算源码.zip'})
        self.assertEqual(platform.send_file.await_args.args[1],'计算源码.zip')
        self.assertTrue(platform.send_file.await_args.args[2].startswith(b'PK'))

    async def test_selected_source_file_uses_verified_bytes_and_chinese_name(self):
        platform,_=await self.completed_delivery(['main.py'],{'main.py':'计算源码.py'})
        self.assertEqual(platform.send_file.await_args.args[1],'计算源码.py')
        self.assertEqual(platform.send_file.await_args.args[2],FILES['main.py'].encode())

    async def test_path_and_type_changes_are_not_accepted_as_delivery_names(self):
        for name in ['../escape.zip','wrong.exe','\nproject.zip','/tmp/out.zip']:
            with self.subTest(name=name),self.assertRaises(ValueError):delivery_filename('project.zip',name)
