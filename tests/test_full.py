import asyncio
import base64
import hashlib
import io
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import httpx

from osa.application.features.consolidation import ConsolidationService
from osa.application.features.knowledge import KnowledgeService
from osa.application.features.research import ResearchService
from osa.application.features.tasks import TaskService
from osa.domain.entities import Destination, GroupPolicy, ModelResult
from osa.domain.features import Record
from osa.domain.text import chunks
from osa.infrastructure.features.downloader import PublicResolver, public_ip, validate_url
from osa.infrastructure.features.embedding import FakeEmbedder
from osa.infrastructure.features.executor import SandboxClient
from osa.infrastructure.features.parsers import parse_document
from osa.infrastructure.features.repository import MemoryFeatureRepository
from osa.infrastructure.features.objects import validate_key
from osa.infrastructure.memory_store import InMemoryStore
from osa.infrastructure.models import FakeModel, ModelRouter
from osa.interfaces.browser import authorized_cookie, signature
from osa.main import create_app
from test_core import demo_settings, make_event


class InputSafetyTests(unittest.TestCase):
    def test_download_rejects_private_redirect_targets_and_credentials(self):
        for url in ['http://127.0.0.1/a','http://[::1]/a','http://169.254.169.254/a','http://10.1.1.1/a','http://localhost/a','http://host.local/a','file:///etc/passwd','http://user:pass@example.com/a','http://example.com:22/a','http://[::ffff:127.0.0.1]/a']:
            with self.subTest(url=url), self.assertRaises((PermissionError, ValueError)):
                validate_url(url)
        self.assertEqual(validate_url('https://example.com/document'), 'example.com')
        self.assertTrue(public_ip('8.8.8.8'))

    def test_object_keys_cannot_traverse_paths(self):
        validate_key('a'*32)
        for key in ['../../secret','/etc/passwd','hello.txt','a'*32+'/x']:
            with self.assertRaises(PermissionError):
                validate_key(key)

    def test_parser_rejects_mismatched_and_unsafe_formats(self):
        self.assertEqual(parse_document('你好'.encode(),'note.txt')[0]['text'],'你好')
        for name, content in [('payload.exe',b'x'),('evil.pdf',b'not a PDF'),('evil.txt',b'abc\x00x')]:
            with self.assertRaises(ValueError):
                parse_document(content, name)

    def test_chunks_preserve_page_and_overlap(self):
        values = chunks([{'page':3,'text':'x'*2100}], size=900, overlap=120)
        self.assertEqual([r['start'] for r in values],[0,780,1560])
        self.assertTrue(all(r['page']==3 for r in values))

    def test_browser_cookie_is_signed_expiring_and_not_admin_token(self):
        payload = str(int(time.time())+60)+'.random_nonce'
        cookie = payload+'.'+signature('secret-owner-key',payload)
        self.assertTrue(authorized_cookie('secret-owner-key',cookie))
        self.assertFalse(authorized_cookie('other',cookie))
        self.assertFalse(authorized_cookie('secret-owner-key','1.nonce.'+signature('secret-owner-key','1.nonce')))
        self.assertFalse(authorized_cookie('secret-owner-key',cookie+'x'))


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = MemoryFeatureRepository()
        self.store = InMemoryStore()
        await self.store.initialize(['10001','10002'])

    async def test_dns_mixed_public_and_private_is_denied(self):
        import socket
        loop = asyncio.get_running_loop()
        results=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        with patch.object(loop,'getaddrinfo',new=AsyncMock(return_value=results)):
            with self.assertRaises(PermissionError):
                await PublicResolver().resolve('example.com',443)

    async def test_repository_dedupe_and_one_claim(self):
        first=await self.repo.put(Record('a','job.test',None,'10001','queued',{},'stable'))
        second=await self.repo.put(Record('b','job.test',None,'10001','queued',{},'stable'))
        self.assertEqual(first.id,second.id)
        self.assertEqual((await self.repo.claim(['job.test'],time.time())).id,'a')
        self.assertIsNone(await self.repo.claim(['job.test'],time.time()))

    async def test_approval_is_one_time_and_parameters_are_bound(self):
        service=TaskService(self.repo)
        job=await service.submit('sandbox',{'code':'print(1)'},actor='12345',group='10001',approval=True)
        self.assertEqual(job.status,'awaiting_approval')
        self.assertIsNone(await self.repo.claim(['job.sandbox'],time.time()))
        approval=(await self.repo.find('approval'))[0]
        await service.approve(approval.id,True)
        self.assertEqual((await self.repo.get(job.id)).status,'queued')
        self.assertEqual((await self.repo.get(approval.id)).payload['parameters']['code'],'print(1)')
        with self.assertRaises(ValueError):
            await service.approve(approval.id,True)

    async def test_approval_restart_reconciles_approved_but_unqueued_job(self):
        service=TaskService(self.repo)
        job=await service.submit('sandbox',{'code':'print(1)'},actor='12345',group='10001',approval=True)
        approval=(await self.repo.find('approval'))[0]
        await self.repo.patch(approval.id,status='approved')
        await service.recover()
        self.assertEqual((await self.repo.get(job.id)).status,'queued')

    async def test_cancel_revokes_even_when_remote_worker_is_offline(self):
        service=TaskService(self.repo)
        service.cancel_callbacks['job.research']=AsyncMock(side_effect=ConnectionError())
        job=await service.submit('research',{'query':'x'},actor='12345',group='10001')
        cancelled=await service.cancel(job.id)
        self.assertEqual(cancelled.status,'cancelled')
        self.assertIsNone(await self.repo.claim(['job.research'],time.time()))

    async def test_handler_succeeds_and_write_failures_are_not_auto_retried(self):
        service=TaskService(self.repo)
        service.handlers['job.sandbox']=AsyncMock(side_effect=RuntimeError())
        job=await service.submit('sandbox',{},actor='12345',group='10001')
        await service.execute(await self.repo.claim(['job.sandbox'],time.time()))
        self.assertEqual((await self.repo.get(job.id)).status,'failed')
        service.handlers['job.index']=AsyncMock(side_effect=RuntimeError())
        retry=await service.submit('index',{},actor='12345',group='10001')
        await service.execute(await self.repo.claim(['job.index'],time.time()))
        self.assertEqual((await self.repo.get(retry.id)).status,'retry')

    async def test_hybrid_retrieval_cannot_cross_group_or_private_boundaries(self):
        knowledge=KnowledgeService(self.repo,FakeEmbedder())
        await knowledge.ingest('A资料',[{'page':2,'text':'A群编译器秘密'}],group='10001',owner=None,source={})
        await knowledge.ingest('B资料',[{'page':1,'text':'B群游戏规则'}],group='10002',owner=None,source={})
        await knowledge.ingest('私聊资料',[{'page':1,'text':'私人秘密'}],group=None,owner='12345',source={})
        result=await knowledge.retrieve('秘密','10002','12345')
        self.assertTrue(all(row['title']=='B资料' for row in result))
        private=await knowledge.retrieve('秘密',None,'67890')
        self.assertEqual(private,[])
        mine=await knowledge.retrieve('秘密',None,'12345')
        self.assertTrue(all(row['title']=='私聊资料' for row in mine))

    async def test_embedding_outage_falls_back_to_scoped_keyword_retrieval(self):
        class BrokenEmbedder:
            model_id = 'broken'
            async def embed(self, texts):
                raise RuntimeError('embedding upstream unavailable')
        knowledge=KnowledgeService(self.repo,None)
        await knowledge.ingest('可计算资料',[{'page':1,'text':'温和配置里的字段说明'}],group='10001',owner=None,source={})
        knowledge.embedder=BrokenEmbedder()
        result=await knowledge.retrieve('字段说明','10001','12345')
        self.assertEqual([row['title'] for row in result],['可计算资料'])

    async def test_public_knowledge_requires_verified_source_and_license(self):
        knowledge=KnowledgeService(self.repo,None)
        with self.assertRaises(PermissionError):
            await knowledge.ingest('x',[{'page':1,'text':'secret'}],group='10001',owner=None,source={},public=True)
        await knowledge.ingest('公共知识',[{'page':1,'text':'公开说明'}],group=None,owner=None,source={'verified':True,'license':'CC0'},public=True)
        self.assertEqual((await knowledge.retrieve('说明','10002','12345'))[0]['title'],'公共知识')

    async def test_document_delete_removes_chunks_and_vectors(self):
        knowledge=KnowledgeService(self.repo,FakeEmbedder())
        document=await knowledge.ingest('文档',[{'page':1,'text':'源代码'}],group='10001',owner=None,source={})
        await knowledge.delete(document.id)
        self.assertEqual(await knowledge.retrieve('源代码','10001','12345'),[])
        self.assertEqual(self.repo.vectors,{})

    async def test_every_research_calls_web_browser_even_when_search_fails(self):
        browser=AsyncMock()
        browser.ask.return_value={'id':'remote'}
        browser.status.return_value={'status':'succeeded','answer':'网站里的回答'}
        search=AsyncMock();search.search.side_effect=ConnectionError()
        service=ResearchService(self.repo,self.store,browser,search,10)
        job=await self.repo.put(Record('job','job.research','12345','10001','running',{'query':'最新消息'}))
        result=await service.run(job)
        browser.ask.assert_awaited_once()
        self.assertEqual(result['provider'],'deepseek_web')
        self.assertEqual(result['answer'],'网站里的回答')
        self.assertEqual((await self.repo.find('research'))[0].payload['answer'],'网站里的回答')

    async def test_login_wait_is_exposed_and_resumes_same_browser_request(self):
        browser=AsyncMock();browser.ask.return_value={'id':'remote'}
        async def status(task_id):
            row=await self.repo.get('job')
            if not row.payload.get('needs_user'):
                return {'status':'waiting_human','answer':'','error':'手动扫码'}
            return {'status':'succeeded','answer':'登录后回答'}
        browser.status.side_effect=status
        service=ResearchService(self.repo,self.store,browser,None,10)
        job=await self.repo.put(Record('job','job.research','12345','10001','running',{'query':'test'}))
        result=await service.run(job)
        self.assertEqual(result['answer'],'登录后回答')
        browser.ask.assert_awaited_once()

    async def test_research_budget_survives_new_service_instance(self):
        day=time.strftime('%Y-%m-%d',time.gmtime())
        await self.repo.put(Record('old','research','12345','10001','succeeded',{'day':day}))
        browser=AsyncMock()
        service=ResearchService(self.repo,self.store,browser,None,1)
        job=await self.repo.put(Record('job','job.research','12345','10001','running',{'query':'test'}))
        with self.assertRaises(PermissionError):
            await service.run(job)
        browser.ask.assert_not_awaited()

    async def test_consolidation_only_accepts_valid_self_sources(self):
        event=make_event(text='我的项目是编译器')
        await self.store.publish(event,True)
        await self.store.update_policy('10001',GroupPolicy(memory_extract=True))
        from datetime import datetime
        from zoneinfo import ZoneInfo
        date=datetime.fromtimestamp(event.occurred_at,ZoneInfo('Asia/Shanghai')).date().isoformat()
        model=AsyncMock();model.generate.return_value=ModelResult(json.dumps({'summary':'项目','facts':[
            {'subject_id':'12345','content':'编译器项目','source_ids':[event.id],'confidence':.95},
            {'subject_id':'67890','content':'冒充事实','source_ids':[event.id],'confidence':.99}]}),'mock','mock')
        service=ConsolidationService(self.repo,self.store,model)
        job=Record('job','job.consolidate',None,'10001','running',{'date':date})
        result=await service.run(job)
        self.assertEqual(result['facts'],1)
        self.assertEqual(len(await self.store.own_memories('12345')),1)
        self.assertEqual(await self.store.own_memories('67890'),[])

    async def test_model_usage_budget_is_persistent(self):
        router=ModelRouter(FakeModel(),1);router.repo=self.repo
        await router.generate([{'role':'user','content':'x'}])
        other=ModelRouter(FakeModel(),1);other.repo=self.repo
        from osa.domain.entities import UpstreamUnavailable
        with self.assertRaises(UpstreamUnavailable):
            await other.generate([{'role':'user','content':'x'}])

    async def test_unlimited_budget_ignores_previous_quota_and_keeps_usage(self):
        limited=ModelRouter(FakeModel(),1);limited.repo=self.repo
        await limited.generate([{'role':'user','content':'previous'}])
        unlimited=ModelRouter(FakeModel(),0);unlimited.repo=self.repo
        await unlimited.generate([{'role':'user','content':'new'}])
        restarted=ModelRouter(FakeModel(),0);restarted.repo=self.repo
        await restarted.generate([{'role':'user','content':'after restart'}])
        rows=await self.repo.find('model_usage',limit=10)
        self.assertEqual(len(rows),3)
        self.assertTrue(all(row.status=='succeeded' for row in rows))

    async def test_sandbox_client_preserves_response_and_cancel_endpoint(self):
        request=httpx.Response(200,json={'status':'succeeded','stdout':'ok'},request=httpx.Request('POST','http://worker/run'))
        fake=AsyncMock();fake.request=AsyncMock(return_value=request);fake.post=AsyncMock(return_value=request)
        fake.__aenter__.return_value=fake
        with patch('osa.infrastructure.features.executor.httpx.AsyncClient',return_value=fake):
            client=SandboxClient('http://worker','token')
            self.assertEqual((await client.run('python',{'code':'print(1)'},'a'*32))['stdout'],'ok')
            await client.cancel('a'*32)
            self.assertIn('/tasks/',fake.post.call_args.args[0])

    async def test_extended_api_assets_and_feature_disabled_states(self):
        app=create_app(demo_settings())
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            headers={'Authorization':'Bearer admin-test-token-12345'}
            self.assertEqual((await client.get('/console-extra.js')).status_code,200)
            self.assertEqual((await client.get('/api/v1/tasks',headers=headers)).status_code,200)
            denied=await client.post('/api/v1/browser/session',headers=headers)
            self.assertEqual(denied.status_code,409)
            self.assertEqual((await client.get('/browser/vnc.html')).status_code,401)
            denied=await client.post('/api/v1/research',headers=headers,json={'query':'最新消息'})
            self.assertEqual(denied.status_code,409)


if __name__=='__main__':
    unittest.main()

class ScopedAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviewer_cannot_read_other_groups_or_global_browser(self):
        from osa.config import AdminAccount
        config=demo_settings().model_copy(update={'allowed_groups':['10001','10002'],
            'admin_accounts':[AdminAccount(name='reviewer',token='reviewer-scope-token-123456789',role='reviewer',groups=['10001'])]})
        app=create_app(config)
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            for group in ['10001','10002']:
                event=make_event(message_id=group,group=group)
                await app.state.store.publish(event,True)
                await app.state.runtime.process(event)
            headers={'Authorization':'Bearer reviewer-scope-token-123456789'}
            values=(await client.get('/api/v1/messages',headers=headers)).json()['data']['items']
            self.assertTrue(values)
            self.assertTrue(all(row['conversation']=='group:10001' for row in values))
            self.assertEqual((await client.get('/api/v1/groups/10002/settings',headers=headers)).status_code,403)
            self.assertEqual((await client.get('/api/v1/browser/status',headers=headers)).status_code,403)
            self.assertEqual((await client.get('/api/v1/memories?actor_id=12345',headers=headers)).status_code,403)
            self.assertEqual((await client.post('/api/v1/controls/emergency-stop',headers=headers,json={'paused':True})).status_code,403)
            groups=(await client.get('/api/v1/groups',headers=headers)).json()['data']
            self.assertEqual([group['id'] for group in groups],['10001'])

class BrowserSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_logout_revokes_live_mapping_without_deleting_website_profile(self):
        from osa.interfaces.browser import active_cookie
        from pydantic import SecretStr
        config=demo_settings().model_copy(update={'deepseek_web_enabled':True,'worker_token':SecretStr('worker-session-token-1234567890')})
        app=create_app(config)
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            headers={'Authorization':'Bearer admin-test-token-12345'}
            result=await client.post('/api/v1/browser/session',headers=headers)
            self.assertEqual(result.status_code,200)
            from urllib.parse import parse_qs, urljoin, urlsplit
            desktop_url = urljoin('http://test/', result.json()['data']['url'])
            socket_path = parse_qs(urlsplit(desktop_url).query)['path'][0]
            # Match current noVNC's relative WebSocket URL construction.
            self.assertEqual(urlsplit(urljoin(desktop_url, socket_path)).path,
                             '/browser/websockify')
            self.assertIn('HttpOnly',result.headers['set-cookie'])
            cookie=client.cookies.get('osa_browser')
            self.assertTrue(active_cookie(app,cookie))
            await client.delete('/api/v1/browser/session',headers=headers)
            self.assertFalse(active_cookie(app,cookie))

class DependencyManifestTests(unittest.TestCase):
    def test_txt_runtime_dependencies_match_project_metadata(self):
        import tomllib
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        project=tomllib.loads((root/'pyproject.toml').read_text())['project']['dependencies']
        requirements=[line.strip() for line in (root/'requirements.txt').read_text().splitlines() if line.strip() and not line.startswith(('#','-'))]
        self.assertEqual(set(project),set(requirements))
