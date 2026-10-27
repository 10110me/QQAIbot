import asyncio
import logging
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from osa.infrastructure.operations import Allowlist, ConsoleLogs
from osa.infrastructure.log_localization import chinese_message
from osa.infrastructure.store import SQLStore
from osa.main import create_app
from test_core import demo_settings, make_event, payload, FakePlatform


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowlist_immediate_ingress_egress_and_conflicts(self):
        app = create_app(demo_settings())
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test",
                headers={"Authorization":"Bearer admin-test-token-12345"}) as client:
            initial = (await client.get('/api/v1/operations/allowlist')).json()['data']
            await app.state.ingest(payload(group='10002',message_id='outside'))
            self.assertEqual(len(app.state.store.events),0)
            result = await client.put('/api/v1/operations/allowlist',json={'groups':['10001','10002'],'revision':initial['revision']})
            self.assertEqual(result.status_code,200)
            self.assertEqual((await client.put('/api/v1/operations/allowlist',json={'groups':[],'revision':0})).status_code,409)
            self.assertEqual((await client.put('/api/v1/operations/allowlist',json={'groups':['abc'],'revision':1})).status_code,422)
            await app.state.ingest(payload(group='10002',message_id='inside'))
            self.assertEqual(len(app.state.store.events),1)
            event = next(iter(app.state.store.events.values()))['event']
            await app.state.runtime.process(event)
            platform = FakePlatform()
            result = await client.put('/api/v1/operations/allowlist',json={'groups':[],'revision':1})
            self.assertEqual(result.status_code,200)
            app.state.dispatcher.platform = platform
            await app.state.dispatcher.dispatch_once()
            self.assertEqual(platform.sent,[])
            self.assertFalse(app.state.settings.allowed_groups)
            self.assertEqual((await client.get('/api/v1/groups')).json()['data'],[])
            await app.state.ingest(payload(group='10002',message_id='after'))
            self.assertEqual(len(app.state.store.events),1)
            self.assertEqual((await client.get('/api/v1/groups/10002/settings')).status_code,403)

    async def test_chat_flow_privacy_pagination_sql_and_memory(self):
        for sql in [False, True]:
            app = create_app(demo_settings())
            await app.state.store.initialize(['10001'],True)
            if sql:
                await app.state.store.close()
                app.state.store = SQLStore('sqlite+aiosqlite:///:memory:')
                await app.state.store.initialize(['10001'],True)
            store = app.state.store
            try:
                stamp = 1792195200.0
                day = datetime.fromtimestamp(stamp,ZoneInfo('Asia/Shanghai')).date().isoformat()
                for i in range(5):
                    event = replace(make_event(message_id=str(i)),occurred_at=stamp,text='private-message-'+str(i))
                    await store.publish(event, i != 0)
                    if i == 1:
                        await store.recall(replace(event,event_type='message.recalled'))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test',
                        headers={'Authorization':'Bearer admin-test-token-12345'}) as client:
                    sessions=(await client.get('/api/v1/operations/streams',params={'day':day})).json()['data']
                    self.assertEqual(sessions[0]['count'],5)
                    all_rows=[]; cursor=None
                    for _ in range(4):
                        params={'day':day,'conversation':'group:10001','limit':2}
                        if cursor:params['before']=cursor
                        data=(await client.get('/api/v1/operations/streams/events',params=params)).json()['data']
                        all_rows.extend(data['items']); cursor=data['next']
                        if not cursor:break
                    self.assertEqual(len(all_rows),5)
                    self.assertEqual(len({r['id'] for r in all_rows}),5)
                    self.assertNotIn('private-message-0',str(all_rows))
                    self.assertNotIn('private-message-1',str(all_rows))
                    self.assertIn('private-message-2',str(all_rows))
                    self.assertEqual((await client.get('/api/v1/operations/streams/events',params={'day':day,'conversation':'group:10001','before':'bad'})).status_code,422)
            finally:
                await store.close()

    async def test_removed_during_send_wait_is_blocked(self):
        app=create_app(demo_settings())
        store=app.state.store
        await store.initialize(['10001'])
        event=make_event()
        await store.publish(event,True)
        await app.state.runtime.process(event)
        dispatcher=app.state.dispatcher
        platform=FakePlatform(); dispatcher.platform=platform
        import time
        dispatcher._last_send[event.destination.key]=time.monotonic()
        task=asyncio.create_task(dispatcher.dispatch_once())
        await asyncio.sleep(.05)
        dispatcher.allowed_groups=frozenset()
        await task
        self.assertEqual(platform.sent,[])

    async def test_operations_require_owner(self):
        settings=demo_settings()
        settings.admin_accounts=[__import__('osa.config',fromlist=['AdminAccount']).AdminAccount(
            name='scoped',token='scoped-secret-token-12345',role='admin',groups=['10001'])]
        app=create_app(settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
            for path in ['allowlist','streams?day=2026-10-17','logs']:
                self.assertEqual((await client.get('/api/v1/operations/'+path)).status_code,401)
                self.assertEqual((await client.get('/api/v1/operations/'+path,headers={'Authorization':'Bearer scoped-secret-token-12345'})).status_code,403)


class PersistenceTests(unittest.TestCase):
    def test_chinese_logs_search_preserves_identifiers_and_redaction(self):
        logs=ConsoleLogs(Path('.'),lambda:['private-key'],memory_only=True)
        logs.emit(logging.LogRecord('osa.infrastructure.configured_models',logging.WARNING,'',0,
            'model_attempt_failed purpose=chat provider=private-key model=chat-model attempt=2 error_type=ConnectTimeout status=None',(),None))
        result=logs.query(source='模型调用',q='连接超时')['items']
        self.assertEqual(len(result),1)
        self.assertIn('模型调用失败',result[0]['display_message'])
        self.assertIn('用途：聊天回复',result[0]['display_message'])
        self.assertIn('模型：chat-model',result[0]['display_message'])
        self.assertNotIn('private-key',str(result))
        self.assertEqual(result[0]['source_label'],'模型调用')
        self.assertTrue(logs.query(q='model_attempt_failed')['items'])
        self.assertIn('目标群已从白名单移除',chinese_message('outbox_finished status=cancelled error=destination_no_longer_allowed'))
        self.assertEqual(chinese_message('unrecognized_event model=chat-model'),'unrecognized_event model=chat-model')
        logs.close()

    def test_allowlist_restart_explicit_empty_and_atomic_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            first=Allowlist(root,['10001'])
            first.save(['20002'],0)
            restarted=Allowlist(root,['10001'])
            self.assertEqual(restarted.value['groups'],['20002'])
            with self.assertRaises(ValueError):restarted.save([],0)
            restarted.save([],1)
            self.assertEqual(Allowlist(root,['10001']).value['groups'],[])

    def test_logs_redact_rotate_restart_and_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            logs=ConsoleLogs(Path(folder),lambda:['actual-private-key'])
            logs.file.maxBytes=350
            for i in range(10):
                logs.emit(logging.LogRecord('osa.runtime',logging.WARNING,'',0,
                    'failure %s api_key=unlisted Bearer bearer-secret https://host/?token=url-secret actual-private-key',(i,),None))
            logs.close()
            restarted=ConsoleLogs(Path(folder),lambda:['actual-private-key'])
            rows=restarted.query(level='WARNING',source='runtime',q='failure',limit=2)
            self.assertEqual(len(rows['items']),2)
            self.assertTrue(rows['next'])
            text=str(rows)
            for secret in ['actual-private-key','unlisted','bearer-secret','url-secret']:
                self.assertNotIn(secret,text)
            self.assertTrue(Path(folder,'logs/console.jsonl.1').exists())
            self.assertFalse(restarted.query(level='ERROR')['items'])
            restarted.close()
