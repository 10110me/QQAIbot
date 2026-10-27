import json,tempfile,time,unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from osa.domain.entities import GroupPolicy,ModelResult
from osa.domain.features import Record
from osa.main import create_app
from osa.infrastructure.store import SQLStore
from test_core import demo_settings,make_event,FakePlatform


class InitiativeDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(demo_settings().model_copy(update={'data_dir':self.temp.name,
            'v2_database_url':str(Path(self.temp.name)/'registry.db'),'v2_enabled':True}))
        self.store=self.app.state.store;self.f=self.app.state.features
        await self.store.initialize(['10001'])
        await self.store.update_policy('10001',GroupPolicy(initiative=True,min_send_interval=0))
        self.platform=FakePlatform();self.app.state.dispatcher.platform=self.platform
        self.app.state.runtime.model.generate=AsyncMock(side_effect=self.generate)
        self.reply_text='这关看着挺有意思'
        self.latest=[]

    async def asyncTearDown(self):
        await self.f.images.shutdown();await self.f.learning.shutdown();await self.store.close()
        self.temp.cleanup()

    async def generate(self,messages,purpose='chat'):
        if purpose=='thinking':
            self.latest=messages
            data={'final_action':'REPLY','reply_brief':{'reply_text':self.reply_text}}
        else:raise AssertionError('Unexpected extra model call: '+purpose)
        return ModelResult(json.dumps(data,ensure_ascii=False),'test',purpose)

    async def process(self,age=60):
        source=replace(make_event(message_id='topic',mention=False,text='这个游戏挺有意思'),occurred_at=time.time()-age)
        newer=make_event(message_id='newer',mention=False,text='这关看着挺有意思')
        for event in [source,newer]:
            await self.store.publish(event,True);await self.f.observe(event)
        await self.app.state.runtime.process(source)
        return source

    async def test_new_message_and_60_second_generation_age_do_not_drop_reply(self):
        event=await self.process()
        self.assertEqual(len(self.store.outboxes),1)
        item=next(iter(self.store.outboxes.values()))
        self.assertGreater(item['expires_at'],time.time())
        self.assertLessEqual(item['expires_at'],time.time()+30)
        self.assertTrue(any('【本轮寻址】' in m['content'] for m in self.latest if m['role']=='system'))
        await self.app.state.dispatcher.dispatch_once()
        self.assertEqual(item['status'],'sent')
        self.assertEqual(len(self.platform.sent),1)
        purposes=[c.args[1] for c in self.app.state.runtime.model.generate.await_args_list]
        self.assertEqual(purposes,['thinking'])

    async def test_truly_expired_reply_is_dropped_with_specific_reason(self):
        await self.process(200)
        await self.app.state.dispatcher.dispatch_once()
        item=next(iter(self.store.outboxes.values()))
        self.assertEqual(item['status'],'cancelled');self.assertEqual(item['error'],'reply_expired')
        self.assertFalse(self.platform.sent)

    async def test_reply_writer_can_decline_obsolete_topic_without_extra_review(self):
        self.reply_text='[无需接话]'
        event=await self.process()
        self.assertFalse(self.store.outboxes)
        self.assertEqual(self.store.decisions[event.id]['reason'],'initiative_no_longer_relevant')

    async def test_game_join_claim_is_not_sent_as_initiative(self):
        self.reply_text='看你们先挑个整合包，定了我再上坑'
        event=await self.process()
        self.assertFalse(self.store.outboxes)
        self.assertEqual(self.store.decisions[event.id]['reason'],'initiative_no_longer_relevant')

    async def test_failed_thinking_stays_silent_for_direct_request(self):
        async def fail_thinking(messages,purpose='chat'):
            if purpose=='thinking':raise RuntimeError('model unavailable')
            raise AssertionError('Unexpected model call: '+purpose)
        self.app.state.runtime.model.generate.side_effect=fail_thinking
        event=make_event(message_id='failed-direct',text='帮我看看')
        await self.store.publish(event,True)
        await self.app.state.runtime.process(event)
        self.assertFalse(self.store.outboxes)
        self.assertEqual(self.store.decisions[event.id]['reason'],'thinking_failed')

    async def test_unsolicited_group_image_does_not_request_a_reply(self):
        event=replace(make_event(message_id='shared-picture',mention=False,text='看看这张图'),
            segments=[{'type':'text','data':{'text':'看看这张图'}},{'type':'image','data':{}}])
        await self.store.publish(event,True)
        await self.app.state.runtime.process(event)
        self.assertFalse(self.store.outboxes)
        self.assertEqual(self.store.decisions[event.id]['reason'],'unsolicited_image')
        self.app.state.runtime.model.generate.assert_not_awaited()

    async def test_unsolicited_group_forward_does_not_request_a_reply(self):
        event=replace(make_event(message_id='shared-chat',mention=False,text=''),
            segments=[{'type':'forward','data':{'id':'7700505355262134060'}}])
        await self.store.publish(event,True)
        await self.app.state.runtime.process_batch([event],trigger='max_wait')
        self.assertFalse(self.store.outboxes)
        self.assertEqual(self.store.decisions[event.id]['reason'],'unsolicited_forward')
        self.app.state.runtime.model.generate.assert_not_awaited()

    async def test_forward_addressed_to_bot_remains_actionable(self):
        event=replace(make_event(message_id='asked-about-chat',text='看看这段聊天'),
            segments=[{'type':'at','data':{'qq':'99999'}},
                      {'type':'text','data':{'text':'看看这段聊天'}},
                      {'type':'forward','data':{'id':'7700505355262134060'}}])
        await self.store.publish(event,True)
        await self.app.state.runtime.process_batch([event],direct_ids={event.id},trigger='direct')
        self.assertEqual(self.store.decisions[event.id]['reason'],'direct_mention')
        self.app.state.runtime.model.generate.assert_awaited()

    async def test_disabling_initiative_after_generation_still_revokes_send(self):
        await self.process()
        await self.store.update_policy('10001',GroupPolicy(initiative=False,min_send_interval=0))
        await self.app.state.dispatcher.dispatch_once()
        item=next(iter(self.store.outboxes.values()))
        self.assertEqual(item['status'],'cancelled');self.assertEqual(item['error'],'evidence_or_permissions_changed')
        self.assertFalse(self.platform.sent)

    async def test_file_download_pause_blocks_tool_inputs_and_background_index(self):
        settings=self.app.state.settings;settings.group_file_download_enabled=False
        event=replace(make_event(),segments=[{'type':'file','data':{'file_id':'broken','name':'要求.txt'}}])
        await self.store.publish(event,True)
        self.f.platform.call=AsyncMock(side_effect=AssertionError('Paused downloader must not call NapCat'))
        self.f.downloader.fetch=AsyncMock(side_effect=AssertionError('Paused downloader must not download'))
        await self.f.work_inputs.observe(event)
        self.assertEqual(await self.f.repo.find('work_input'),[])
        result=await self.f.run_index(Record('index','job.index',event.actor_id,'10001','queued',{'file_id':'broken'}))
        self.assertEqual(result['status'],'disabled')
        ctx={'actor_id':event.actor_id,'group_id':'10001','source_id':event.id,'source_event':event.to_dict()}
        await self.app.state.v2.call('begin_thinking','files-disabled',ctx)
        result=await self.app.state.v3.thinking.tools.execute('group.file.download',{'file_id':'broken'},
            {**ctx,'session_id':'files-disabled','tool_call_id':'download','deadline':time.time()+60})
        self.assertEqual(result['error'],'GROUP_FILE_DOWNLOAD_DISABLED');self.assertFalse(result['retryable'])
        self.f.platform.call.assert_not_awaited();self.f.downloader.fetch.assert_not_awaited()

    async def test_sql_outbox_honors_same_short_delivery_deadline(self):
        sql=SQLStore('sqlite+aiosqlite:///'+str(Path(self.temp.name)/'sql.db'))
        try:
            await sql.initialize(['10001'],True)
            event=make_event();await sql.publish(event,True)
            deadline=time.time()+20
            await sql.record_decision(event,'REPLY','initiative_candidate',{'ttl':180,'send_deadline':deadline},ModelResult('接话','test','test'))
            self.assertEqual((await sql.claim_outbox())['expires_at'],deadline)
        finally:await sql.close()
