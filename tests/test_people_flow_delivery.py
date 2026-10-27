import json
import logging
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from osa.application.people import PeopleService
from osa.application.reply_delivery import split_reply
from osa.application.chat_stream import SilentLearningService
from osa.application.v3.thinking import ThinkingLoop
from osa.domain.entities import GroupPolicy, Memory, ModelResult, Scope
from osa.infrastructure.features.repository import MemoryFeatureRepository
from osa.infrastructure.memory_store import InMemoryStore
from osa.infrastructure.operations import ConsoleLogs
from osa.infrastructure.store import SQLStore
from osa.infrastructure.v3.opencode import OpenCodeAdapter
from osa.main import create_app
from test_core import demo_settings, make_event, payload
from osa.infrastructure.onebot import normalize


LONG_REPLY='先把群里刚发的需求文件下载到共享目录，工作空间才能读取里面的具体要求。写完之后我再把通过检查的文件发回来。'


class PeopleFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_names_alias_evidence_group_scope_and_recall(self):
        store, repo = InMemoryStore(), MemoryFeatureRepository()
        await store.initialize(['10001','10002'])
        await store.update_policy('10001', GroupPolicy(memory_extract=True))
        people=PeopleService(repo,store);people.bot_id='99999'
        raw=payload();raw['sender']={'card':' 群名片 ','nickname':'QQ昵称'}
        first=normalize(raw,'99999','test')
        self.assertEqual((first.actor_name,first.actor_nickname),('群名片','QQ昵称'))
        await store.publish(first,True);await people.observe(first,GroupPolicy())
        rename=replace(make_event(message_id='rename'),actor_name='新名片')
        await store.publish(rename,True);await people.observe(rename,GroupPolicy())
        quote=make_event(message_id='alias',actor='23456',text='以后叫你小名就行',mention=False)
        quote=replace(quote,segments=[{'type':'at','data':{'qq':first.actor_id}}])
        await store.publish(quote,True)
        await people.learn_alias(first.destination,{'subject_id':first.actor_id,'alias':'小名','source_ids':[quote.id]},[quote])
        members, sources=await people.members(rename)
        self.assertEqual(members[0]['name'],'新名片');self.assertEqual(members[0]['aliases'],['小名'])
        self.assertIn(quote.id,sources)
        self.assertEqual((await people.members(replace(rename,destination=first.destination.__class__('group','10002'))))[0][0]['aliases'],[])
        await store.recall(replace(quote,event_type='message.recalled'))
        self.assertFalse((await people.members(rename))[0][0]['aliases'])
        await people.learn_alias(first.destination,{'subject_id':'99999','alias':'小名','source_ids':[quote.id]},[quote])
        self.assertEqual(len(await repo.find('person_alias')),1)

    async def test_ambiguous_alias_not_saved_and_self_statement_keeps_all_evidence(self):
        store,repo=InMemoryStore(),MemoryFeatureRepository();await store.initialize(['10001'])
        learning=SilentLearningService(repo,store,SimpleNamespace())
        one=make_event(message_id='p1',text='我在NAS上运行Python机器人',mention=False)
        two=make_event(message_id='p2',text='项目的用户界面也由我维护',mention=False)
        for event in [one,two]:await store.publish(event,True)
        await learning.people.learn_alias(one.destination,{'subject_id':'999','alias':'小名','source_ids':[one.id]},[one])
        self.assertFalse(await repo.find('person_alias'))
        content='该成员在自己的 NAS 上运行 Python 机器人，并负责维护这个机器人的用户界面。'
        await learning._save_fact(one.destination,{'subject_id':one.actor_id,'content':content,'source_ids':[one.id,two.id],'confidence':.95},{one.id,two.id})
        saved=next(iter(store.memories.values()));self.assertEqual(saved.content,content)
        self.assertEqual((await repo.find('memory_evidence'))[0].payload['source_ids'],[one.id,two.id])

    async def test_split_replies_are_durable_ordered_and_abort_remaining_after_unknown(self):
        for sql in [False,True]:
            with self.subTest(sql=sql),tempfile.TemporaryDirectory() as folder:
                store=SQLStore('sqlite+aiosqlite:///'+(Path(folder)/'db.sqlite').as_posix()) if sql else InMemoryStore()
                await store.initialize(['10001'],True)
                event=make_event();await store.publish(event,True)
                result=ModelResult(LONG_REPLY*32,'real-provider','real-model',input_tokens=10,output_tokens=20,attachment_ids=['sticker'])
                snapshot={'ttl':180,'source_ids':[event.id]}
                await store.record_decision(event,'REPLY','test',snapshot,result)
                await store.record_decision(event,'REPLY','test',snapshot,result)
                parts=[]
                while (row:=await store.claim_outbox()):
                    parts.append(row);await store.finish_outbox(row['id'],'sent',str(len(parts)))
                self.assertGreater(len(parts),1)
                self.assertEqual(parts[0]['reply_to'],event.message_id)
                self.assertTrue(all(not row['reply_to'] for row in parts[1:]))
                self.assertEqual(''.join(row['text'] for row in parts),LONG_REPLY*32)
                self.assertEqual(sum(row['input_tokens'] for row in parts),10)
                self.assertEqual(sum(bool(row['attachment_ids']) for row in parts),1)
                self.assertEqual(parts[-1]['attachment_ids'],['sticker'])
                second=make_event(message_id='second');await store.publish(second,True)
                await store.record_decision(second,'REPLY','test',snapshot,result)
                first=await store.claim_outbox();await store.finish_outbox(first['id'],'unknown')
                self.assertIsNone(await store.claim_outbox())
                await store.close()

    def test_code_urls_decimal_and_kaomoji_are_not_broken(self):
        self.assertEqual(split_reply(LONG_REPLY),[LONG_REPLY])
        self.assertEqual(split_reply('```python\n'+LONG_REPLY+'\n```'),['```python\n'+LONG_REPLY+'\n```'])
        text='先查看 https://example.com/foo?a=3.12 里面的资料。之后我会整理结果，发到群里面给你看。'
        self.assertTrue(any('https://example.com/foo?a=3.12' in p for p in split_reply(text)))
        self.assertLessEqual(len(split_reply(LONG_REPLY*32)),4)

    async def test_stream_names_tool_details_and_memory_source_are_real_and_owner_only(self):
        app=create_app(demo_settings());store=app.state.store
        await store.initialize(['10001'])
        event=replace(make_event(text='我负责维护一个Python项目的UI'),actor_name='小林')
        await store.publish(event,True)
        await store.add_memory(Memory('mem',event.text,Scope.LONG_GROUP,event.actor_id,'10001',event.id))
        app.state.platform._group_names={'10001':'开发交流群'}
        log=app.state.console_logs
        logger=logging.getLogger('osa');old_level=logger.level;logger.setLevel(logging.INFO);logger.addHandler(log)
        replies=[{'tool_call':{'tool_call_id':'consult','name':'deepseek.consult','arguments':{'question':'帮我确认这个图片的角色'}}},
                 {'tool_call':{'tool_call_id':'done','name':'reply','arguments':{'key_points':['正在查']}}}]
        model=SimpleNamespace(generate=AsyncMock(side_effect=[ModelResult(json.dumps(r,ensure_ascii=False),'test','model') for r in replies]))
        loop=ThinkingLoop(model,SimpleNamespace(execute=AsyncMock(return_value={'task_id':'consultation-1','status':'queued'})))
        loop.flow=app.state.flow_journal
        try:
            await loop.run([],{'source_id':event.id,'collect_messages':True},'test-tool-details')
            day=datetime.fromtimestamp(event.occurred_at,ZoneInfo('Asia/Shanghai')).date().isoformat()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test',headers={'Authorization':'Bearer admin-test-token-12345'}) as client:
                names=(await client.get('/api/v1/operations/streams',params={'day':day})).json()['data']
                self.assertEqual(names[0]['name'],'开发交流群')
                rows=(await client.get('/api/v1/operations/streams/events',params={'day':day,'conversation':'group:10001'})).json()['data']['items']
                self.assertEqual(rows[0]['actor_name'],'小林')
                tool=next(e for e in rows[0]['flow'] if e['kind']=='tool.end')
                detail=(await client.get('/api/v1/operations/logs/model-calls/'+tool['call_id'])).json()['data']
                self.assertEqual(detail['tool_name'],'deepseek.consult')
                self.assertIn('图片的角色',detail['messages'][0]['content'])
                self.assertIn('consultation-1',detail['response'])
                memory=(await client.get('/api/v1/operations/memories')).json()['data']['items'][0]
                self.assertEqual(memory['sources'][0]['text'],event.text)
                self.assertEqual(memory['actor_name'],'小林')
                await store.recall(replace(event,event_type='message.recalled'))
                self.assertEqual((await client.get('/api/v1/operations/logs/model-calls/'+tool['call_id'])).status_code,404)
        finally:
            logger.removeHandler(log);logger.setLevel(old_level)
            await app.state.features.learning.shutdown();await store.close();app.state.flow_journal.close();log.close()

    async def test_opencode_cold_start_retries_health_only(self):
        adapter=OpenCodeAdapter(SimpleNamespace(),'http://test','test-password')
        req=httpx.Request('GET','http://test/global/health')
        cold=httpx.HTTPStatusError('starting',request=req,response=httpx.Response(503,json={'detail':'CODING_SERVER_STARTING'},request=req))
        adapter.request=AsyncMock(side_effect=[cold,cold,{'healthy':True,'version':'1.2.27'}])
        with patch('osa.infrastructure.v3.opencode.asyncio.sleep',new_callable=AsyncMock):
            await adapter.health('task')
        self.assertEqual(adapter.request.await_count,3)
        self.assertTrue(all(call.args[1:]==('GET','/global/health') for call in adapter.request.await_args_list))
        adapter.request=AsyncMock(side_effect=cold)
        from osa.domain.work import WorkModelUnavailable
        with patch('osa.infrastructure.v3.opencode.time.monotonic',side_effect=[0,181]):
            with self.assertRaises(WorkModelUnavailable):await adapter.health('task')

    async def test_migration_keeps_existing_messages_and_reviews(self):
        import asyncio
        import os
        from alembic.config import Config
        from alembic import command
        from sqlalchemy import text
        with tempfile.TemporaryDirectory() as folder:
            url='sqlite+aiosqlite:///'+(Path(folder)/'migrate.sqlite').as_posix()
            config=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
            config.set_main_option('script_location',str(Path(__file__).resolve().parents[1]/'migrations'))
            with patch.dict(os.environ, {'OSA_DATABASE_URL':url}):
                await asyncio.to_thread(command.upgrade,config,'0005')
                store=SQLStore(url);await store.initialize(['10001'])
                original=make_event(message_id='old');await store.publish(original,True)
                await store.record_decision(original,'REPLY','test',{'ttl':180},ModelResult('旧消息','p','m'))
                row=await store.claim_outbox();await store.finish_outbox(row['id'],'sent','9001')
                async with store.session.begin() as session:
                    await session.execute(text("INSERT INTO message_reviews(id,outbox_id,tags,note,created_at) VALUES ('review',:id,'[]','保留评价',1)"),{'id':row['id']})
                await store.close()
                await asyncio.to_thread(command.upgrade,config,'head')
                store=SQLStore(url)
                try:
                    async with store.session() as session:
                        self.assertEqual((await session.execute(text('SELECT text FROM outbox_messages'))).scalar(),'旧消息')
                        self.assertEqual((await session.execute(text('SELECT outbox_id FROM message_reviews'))).scalar(),row['id'])
                    event=make_event(message_id='new');await store.publish(event,True)
                    await store.record_decision(event,'REPLY','test',{'ttl':180},ModelResult(LONG_REPLY*32,'p','m'))
                    async with store.session() as session:
                        count=(await session.execute(text('SELECT COUNT(*) FROM outbox_messages WHERE event_id=:id'),{'id':event.id})).scalar()
                        self.assertGreater(count,1)
                finally:
                    await store.close()
