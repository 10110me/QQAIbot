import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

from osa.application.conversation_scheduler import ConversationScheduler
from osa.domain.entities import GroupPolicy, ModelResult
from osa.main import create_app
from test_core import demo_settings, make_event


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings = demo_settings().model_copy(update={
            'data_dir': self.temp.name, 'v2_database_url': ':memory:',
            'workspace_dir': self.temp.name+'/work',
            'allowed_groups': ['10001', '10002'],
        })
        self.app = create_app(settings)
        self.runtime = self.app.state.runtime
        self.store = self.app.state.store
        await self.store.initialize(settings.allowed_groups)
        for group in settings.allowed_groups:
            await self.store.update_policy(group, GroupPolicy(initiative=True,initiative_min_interval=0))
        self.clock = 100.0
        self.engine = ConversationScheduler(self.store, self.runtime, 2, clock=lambda: self.clock)
        self.runtime.process_batch = AsyncMock(wraps=self.runtime.process_batch)
        async def respond(event, messages, snapshot):
            snapshot['thinking']={'final_action':'REPLY'}
            return ModelResult('收到。','test','reply')
        self.app.state.v3.respond = AsyncMock(side_effect=respond)

    async def asyncTearDown(self):
        for task in self.engine.running:task.cancel()
        await asyncio.gather(*self.engine.running, return_exceptions=True)
        await self.app.state.features.learning.shutdown()
        await self.store.close()
        self.temp.cleanup()

    async def publish(self, number, **kwargs):
        event = make_event(message_id=str(number), text='群友聊天内容 '+str(number), mention=False, **kwargs)
        await self.store.publish(event, True)
        return event

    async def settle(self):
        await asyncio.gather(*list(self.engine.running))
        await self.engine.tick()

    async def test_two_messages_produce_one_thinking_turn_and_cooldown_accumulates_next_batch(self):
        first = await self.publish(1)
        second = await self.publish(2)
        await self.engine.tick()
        self.app.state.v3.respond.assert_not_awaited()
        self.clock += 1
        await self.engine.tick(); await self.settle()
        self.assertEqual(self.app.state.v3.respond.await_count, 1)
        snapshot = self.app.state.v3.respond.await_args.args[2]
        self.assertEqual(snapshot['batch']['message_count'], 2)
        self.assertEqual(self.store.decisions[first.id]['reason'], 'batch_included')
        self.assertEqual(self.store.decisions[second.id]['reason'], 'initiative_candidate')
        await self.publish(3); await self.publish(4)
        self.clock += 1
        await self.engine.tick()
        self.assertEqual(self.app.state.v3.respond.await_count, 1)
        self.clock += 7
        await self.engine.tick(); await self.settle()
        self.assertEqual(self.app.state.v3.respond.await_count, 2)

    async def test_single_message_has_bounded_cold_start_wait(self):
        await self.store.update_policy('10001', GroupPolicy(initiative=True, talk_frequency=.01))
        await self.publish(1); await self.engine.tick()
        self.clock += 8
        await self.engine.tick(); await self.settle()
        snapshot = self.app.state.v3.respond.await_args.args[2]
        self.assertEqual(snapshot['batch']['trigger'], 'max_wait')
        self.assertEqual(self.app.state.v3.respond.await_count, 1)

    async def test_image_wait_does_not_hold_later_direct_mention(self):
        self.app.state.features.images.pending_for=MagicMock(return_value=True)
        picture=replace(make_event(message_id='image-wait',text='',mention=False),
            segments=[{'type':'image','data':{}}])
        await self.store.publish(picture,True)
        await self.engine.tick()
        self.clock+=8
        await self.engine.tick()
        self.runtime.process_batch.assert_not_awaited()
        mention=make_event(message_id='image-question',text='nova 看看刚才的图')
        await self.store.publish(mention,True)
        await self.engine.tick();await self.settle()
        self.assertEqual(self.app.state.v3.respond.await_count,1)
        self.assertIn(picture.id,self.app.state.v3.respond.await_args.args[2]['source_ids'])

    async def test_image_wait_has_thirty_second_deadline(self):
        self.app.state.features.images.pending_for=MagicMock(return_value=True)
        picture=replace(make_event(message_id='image-timeout',text='',mention=False),
            segments=[{'type':'image','data':{}}])
        await self.store.publish(picture,True)
        await self.engine.tick()
        self.clock+=8
        await self.engine.tick()
        self.runtime.process_batch.assert_not_awaited()
        self.clock+=22
        await self.engine.tick();await self.settle()
        self.runtime.process_batch.assert_awaited_once()

    async def test_silent_thinking_finishes_batch_without_decision_retry(self):
        async def silent(event, messages, snapshot):
            snapshot['thinking']={'final_action':'SILENT'}
            return None
        self.app.state.v3.respond.side_effect = silent
        first = await self.publish(1)
        second = await self.publish(2); await self.engine.tick()
        self.clock += 1
        await self.engine.tick(); await self.settle()
        self.assertEqual(self.app.state.v3.respond.await_count, 1)
        self.assertEqual(self.store.decisions[first.id]['reason'], 'batch_included')
        self.assertEqual(self.store.decisions[second.id]['reason'], 'thinking_silent')
        self.assertTrue(all(row['status']=='done' for row in self.store.events.values()))

    async def test_new_mention_bypasses_cooldown_and_keeps_requester_as_anchor(self):
        await self.publish(1); await self.publish(2); await self.engine.tick()
        self.clock += 1
        await self.engine.tick(); await self.settle()
        mention = replace(make_event(message_id='urgent', text='你在吗'), actor_id='12346')
        other = make_event(message_id='later', text='别的群友插话', actor='12347', mention=False)
        await self.store.publish(mention, True); await self.store.publish(other, True)
        await self.engine.tick(); await self.settle()
        self.assertEqual(self.app.state.v3.respond.await_count, 2)
        self.assertEqual(self.app.state.v3.respond.await_args.args[0].id, mention.id)
        self.assertEqual(self.runtime.process_batch.await_args.args[2], 'direct')

    async def test_same_user_followup_after_name_call_stays_direct(self):
        caller = '12345'
        name_call = replace(make_event(message_id='name', text='nova'), actor_id=caller)
        followup = replace(make_event(message_id='notice', text='你读一下群公告里面有什么'), actor_id=caller)
        await self.store.publish(name_call, True)
        await self.engine.tick(); await self.settle()
        self.assertEqual(self.app.state.v3.respond.await_count, 1)
        await self.store.publish(followup, True)
        self.clock += 37
        await self.engine.tick(); await self.settle()
        self.assertEqual(self.app.state.v3.respond.await_count, 2)
        self.assertEqual(self.app.state.v3.respond.await_args.args[0].id, followup.id)
        self.assertIn(followup.id, self.runtime.process_batch.await_args.args[1])

    async def test_other_bot_mention_cannot_replace_an_explicit_work_request(self):
        request=make_event(message_id='minesweeper',text='写一个扫雷，然后发给我，使用说明用中文')
        other=replace(make_event(message_id='other-bot',text='你能不能帮我测试一个程序',mention=False),
            segments=[{'type':'at','data':{'qq':'88888'}},{'type':'text','data':{'text':'你能不能帮我测试一个程序'}}])
        await self.store.publish(request,True);await self.store.publish(other,True)
        await self.engine.tick()
        await asyncio.gather(*list(self.engine.running))
        self.assertEqual(self.app.state.v3.respond.await_args.args[0].id,request.id)
        self.assertNotIn(other.id,self.engine.turns[request.destination.key].direct_ids)
        self.assertEqual(self.store.decisions[other.id]['reason'],'batch_included')

    async def test_two_direct_requests_reach_one_planning_round_with_both_identities(self):
        first=make_event(message_id='first-request',text='先写扫雷')
        second=make_event(message_id='second-request',text='另外做一个计算程序')
        await self.store.publish(first,True);await self.store.publish(second,True)
        await self.engine.tick();await asyncio.gather(*list(self.engine.running))
        self.assertEqual(self.app.state.v3.respond.await_count,1)
        self.assertEqual(self.runtime.process_batch.await_args.args[1],{first.id,second.id})
        self.assertIn(first.id, json.dumps(self.app.state.v3.respond.await_args.args[1]))
        self.assertIn(second.id, json.dumps(self.app.state.v3.respond.await_args.args[1]))
        self.assertTrue(all(row['status']=='done' for row in self.store.events.values()))

    async def test_running_group_accepts_messages_without_blocking_other_group(self):
        gate = asyncio.Event()
        async def respond(event, messages, snapshot):
            if event.id == first.id:await gate.wait()
            snapshot['thinking']={'final_action':'REPLY'}
            return ModelResult('收到。','test','reply')
        self.app.state.v3.respond.side_effect = respond
        first = make_event(message_id='slow')
        await self.store.publish(first, True); await self.engine.tick()
        await asyncio.sleep(.01)
        await self.publish(2)
        other = make_event(group='10002', message_id='fast')
        await self.store.publish(other, True); await self.engine.tick()
        await asyncio.wait_for(asyncio.gather(*[task for task,key in self.engine.running.items() if key==other.destination.key]),1)
        self.assertIn(other.id, self.store.decisions)
        self.assertNotIn(first.id, self.store.decisions)
        self.assertEqual(len((await self.runtime.streams.snapshot(first.destination))['messages']), 2)
        gate.set(); await self.settle()

    async def test_followups_are_coalesced_and_expired_chat_never_calls_model(self):
        import time
        mention=make_event(message_id='start',text='nova')
        await self.store.publish(mention,True);await self.engine.tick();await self.settle()
        for i in range(12):await self.publish(100+i)
        await self.engine.tick();await self.settle()
        self.assertEqual(self.app.state.v3.respond.await_count,2)
        self.assertEqual(len(self.runtime.process_batch.await_args.args[0]),12)
        stale=replace(make_event(message_id='expired-chat',text='早就过去的闲聊',mention=False),occurred_at=time.time()-500)
        explicit=replace(make_event(message_id='old-task',text='写一个扫雷'),occurred_at=time.time()-500)
        await self.store.publish(stale,True);await self.store.publish(explicit,True)
        await self.engine.tick();await self.settle()
        self.assertEqual(self.store.decisions[stale.id]['reason'],'batch_expired')
        self.assertEqual(self.app.state.v3.respond.await_args.args[0].id,explicit.id)

    async def test_recalled_message_is_not_in_decision_batch(self):
        first = await self.publish(1); await self.publish(2); await self.engine.tick()
        await self.store.recall(replace(first,event_type='message.recalled'))
        self.clock += 1
        await self.engine.tick(); await self.settle()
        snapshot = self.app.state.v3.respond.await_args.args[2]
        self.assertNotIn(first.id,snapshot['source_ids'])
        self.assertNotIn(first.text,json.dumps(self.app.state.v3.respond.await_args.args[1],ensure_ascii=False))

    async def test_disabled_initiative_never_calls_model_for_regular_chat(self):
        await self.store.update_policy('10001', GroupPolicy())
        await self.publish(1); await self.engine.tick(); await self.settle()
        self.app.state.v3.respond.assert_not_awaited()

    async def test_direct_request_gets_next_free_slot_before_older_incidental_chat(self):
        self.engine.concurrency = 1
        await self.publish('old-a')
        await self.publish('old-b')
        await self.engine.tick()
        self.clock += 1
        mention = make_event(group='10002', message_id='priority', text='请先回应这个请求')
        await self.store.publish(mention, True)
        await self.engine.tick()
        await asyncio.gather(*list(self.engine.running))
        self.assertEqual(self.runtime.process_batch.await_args.args[0][0].id, mention.id)
        self.app.state.v3.respond.assert_awaited_once()

    async def test_batch_reply_creates_one_outbox_and_recalling_any_source_cancels_delivery(self):
        from osa.application.runtime import OutboxDispatcher
        from test_core import FakePlatform
        from osa.application.v3.service import V3Service
        self.app.state.v3.respond = V3Service.respond.__get__(self.app.state.v3)
        model = AsyncMock()
        model.generate.return_value = ModelResult(json.dumps({'final_action':'REPLY',
            'reply_brief':{'reply_text':'我看到你们刚才说的事情了。'}},ensure_ascii=False), 'test','thinking')
        self.app.state.v3.thinking.model = model
        first = await self.publish(1)
        second = await self.publish(2)
        await self.engine.tick(); self.clock += 1
        await self.engine.tick(); await self.settle()
        self.assertEqual(len(self.store.outboxes), 1)
        outbox = next(iter(self.store.outboxes.values()))
        self.assertEqual(outbox['event_id'], second.id)
        self.assertEqual(outbox['text'], '我看到你们刚才说的事情了。')
        self.assertIn(first.id,self.store.decisions[second.id]['snapshot']['source_ids'])
        await self.store.recall(replace(first,event_type='message.recalled'))
        platform=FakePlatform()
        dispatcher=OutboxDispatcher(self.store,platform)
        await dispatcher.dispatch_once()
        self.assertEqual(platform.sent,[])
        self.assertEqual(outbox['status'],'cancelled')

    async def test_unfinished_batch_is_recovered_from_sqlite_after_restart(self):
        from osa.infrastructure.store import SQLStore
        from osa.infrastructure.schema import EventRow
        from sqlalchemy import select
        url='sqlite+aiosqlite:///'+self.temp.name+'/scheduler.db'
        original=SQLStore(url)
        await original.initialize(['10001'],True)
        event=make_event(message_id='durable',mention=False)
        await original.publish(event,True)
        scheduler=ConversationScheduler(original,self.runtime,clock=lambda:self.clock)
        await scheduler.accept(await original.claim_events(1))
        self.assertEqual(len(scheduler.turns[event.destination.key].events),1)
        await original.close()
        recovered=SQLStore(url)
        try:
            await recovered.initialize(['10001'])
            events=await recovered.claim_events(2)
            self.assertEqual([row.id for row in events],[event.id])
            async with recovered.session() as session:
                self.assertEqual(await session.scalar(select(EventRow.status)),'processing')
        finally:await recovered.close()
