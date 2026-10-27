import json
import tempfile
import httpx
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from osa.application.runtime import OutboxDispatcher
from osa.application.v3.thinking import ThinkingLoop, ReplyWriter
from osa.domain.entities import GroupPolicy, ModelResult
from osa.main import create_app
from test_core import FakePlatform, demo_settings, make_event
from test_v2 import V2AsyncCase


def action(value):
    return ModelResult(json.dumps(value, ensure_ascii=False), 'test', 'planner')


class ReplyLatencyTests(V2AsyncCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings = demo_settings().model_copy(update={
            'data_dir': self.temp.name, 'workspace_dir': self.temp.name + '/work',
            'v2_database_url': self.temp.name + '/registry.db',
            'decision_mode': 'local', 'reply_mode': 'integrated',
        })
        self.app = create_app(settings)
        self.store = self.app.state.store
        await self.store.initialize(['10001'])
        await self.store.update_policy('10001', GroupPolicy(initiative=True))
        self.provider = SimpleNamespace(generate=AsyncMock(return_value=action({
            'final_action': 'REPLY', 'reply_brief': {'reply_text': '在呢，什么事？'},
        })))
        self.app.state.runtime.model.provider = self.provider

    async def asyncTearDown(self):
        await self.app.state.features.learning.shutdown()
        await self.app.state.store.close()
        self.temp.cleanup()

    async def process(self, event):
        await self.store.publish(event, True)
        await self.app.state.runtime.process(event)

    async def test_direct_reply_uses_one_model_and_recall_still_blocks_delivery(self):
        event = make_event(text='你在吗')
        await self.process(event)
        self.assertEqual([call.args[1] for call in self.provider.generate.await_args_list], ['thinking'])
        outbox = next(iter(self.store.outboxes.values()))
        self.assertEqual(outbox['text'], '在呢，什么事？')
        await self.store.recall(replace(event, event_type='message.recalled'))
        platform = FakePlatform()
        dispatcher = OutboxDispatcher(self.store, platform)
        await dispatcher.dispatch_once()
        self.assertEqual(platform.sent, [])
        self.assertEqual(outbox['status'], 'cancelled')

    async def test_direct_reply_past_old_deadline_is_sent_with_source_quote(self):
        event = make_event(text='这条处理慢一点也没关系')
        await self.process(event)
        outbox = next(iter(self.store.outboxes.values()))
        outbox['expires_at'] = 0
        platform = FakePlatform()
        dispatcher = OutboxDispatcher(self.store, platform)
        await dispatcher.dispatch_once()
        self.assertEqual(outbox['status'], 'sent')
        self.assertEqual(platform.sent[0][2], event.message_id)

    async def test_eligible_initiative_can_reply_or_remain_silent_in_one_model_call(self):
        self.app.state.features.initiative_eligible = AsyncMock(return_value=True)
        for number, terminal in enumerate([
            {'final_action': 'REPLY', 'reply_brief': {'reply_text': '听起来很有趣。'}},
            {'final_action': 'SILENT'},
        ]):
            self.provider.generate.return_value = action(terminal)
            self.provider.generate.reset_mock()
            await self.process(make_event(
                text='今天讨论的话题', message_id=str(number), mention=False))
            self.assertEqual([call.args[1] for call in self.provider.generate.await_args_list], ['thinking'])
        self.assertEqual(len(self.store.outboxes), 1)

    async def test_other_member_told_to_work_is_observation_not_nova_request(self):
        first = make_event(text='这个活交给你吧', actor='12345',
                           message_id='human-first', mention=False)
        second = make_event(text='你去干活', actor='12346',
                            message_id='human-second', mention=False)
        await self.store.publish(first, True)
        self.app.state.features.initiative_eligible = AsyncMock(return_value=True)
        self.provider.generate.return_value = action({'final_action': 'SILENT'})
        await self.process(second)
        self.assertFalse(self.store.outboxes)
        self.provider.generate.assert_awaited_once()
        self.assertEqual(self.store.decisions[second.id]['reason'], 'thinking_silent')
        policy = str(self.provider.generate.await_args.args[0])
        self.assertIn('group_observation', policy)
        self.assertNotIn('Programming deliverable', policy)

    async def test_initiative_reply_is_cancelled_when_humans_continue_the_exchange(self):
        self.app.state.features.initiative_eligible = AsyncMock(return_value=True)
        source = make_event(text='这个题目怎么解', message_id='human-question', mention=False)
        await self.process(source)
        followup = replace(make_event(text='我知道啊', message_id='human-followup', mention=False,
                                      actor='12346'), reply_to_id=source.message_id)
        await self.store.publish(followup, True)
        platform = FakePlatform()
        dispatcher = OutboxDispatcher(self.store, platform)
        await dispatcher.dispatch_once()
        self.assertEqual(platform.sent, [])
        self.assertEqual(self.store.outboxes[source.id]['error'], 'conversation_moved_on')

    async def test_member_mention_and_short_continuation_do_not_trigger_a_model(self):
        self.app.state.features.initiative_eligible = AsyncMock(return_value=True)
        previous = replace(make_event(text='', message_id='mention-member', mention=False),
                           segments=[{'type': 'at', 'data': {'qq': '12346'}}])
        current = replace(make_event(text='可爱捏', message_id='member-continuation', mention=False),
                          occurred_at=previous.occurred_at + 2)
        await self.store.publish(previous, True)
        await self.process(current)
        self.provider.generate.assert_not_awaited()
        self.assertEqual(self.store.decisions[current.id]['reason'], 'member_directed_continuation')
        direct = replace(make_event(text='Nova 在吗', message_id='explicit-change'),
                         occurred_at=current.occurred_at + 1)
        await self.process(direct)
        self.provider.generate.assert_awaited_once()

    async def test_group_admin_gets_neutral_address_and_old_replies_are_only_history(self):
        configuration = self.app.state.runtime_configuration.snapshot()
        configuration.persona.owner_qq_ids = ['100000001']
        self.app.state.runtime_configuration.replace(configuration, configuration.revision)
        first = make_event(message_id='old-bot-reply')
        await self.process(first)
        previous = next(iter(self.store.outboxes.values()))
        previous.update(status='sent', sent_at=first.occurred_at + .1, text='在呢，主人')
        current = replace(make_event(text='Nova 你好', actor='100000002', message_id='admin-not-owner'),
                          actor_role='admin', actor_name='fixture-owner', occurred_at=first.occurred_at + 1)
        self.provider.generate.return_value = action({'final_action': 'REPLY',
            'reply_brief': {'reply_text': '在呢，主人有什么吩咐？'}})
        self.provider.generate.reset_mock()
        await self.process(current)
        self.provider.generate.assert_awaited_once()
        messages = self.provider.generate.await_args.args[0]
        self.assertFalse(any(message['role'] == 'assistant' for message in messages))
        self.assertTrue(any('你过去的回复' in message['content'] for message in messages))
        self.assertIn('"sender_is_persona_owner":false', messages[0]['content'])
        self.assertEqual(self.store.outboxes[current.id]['text'], '在呢，你有什么吩咐？')

    async def test_owner_binding_is_rechecked_when_queued_reply_is_sent(self):
        configuration = self.app.state.runtime_configuration.snapshot()
        configuration.persona.owner_qq_ids = ['100000001']
        self.app.state.runtime_configuration.replace(configuration, configuration.revision)
        self.provider.generate.return_value = action({'final_action': 'REPLY',
            'reply_brief': {'reply_text': '主人，在呢。'}})
        event = make_event(actor='100000001', message_id='owner-before-change')
        await self.process(event)
        self.assertEqual(self.store.outboxes[event.id]['text'], '主人，在呢。')
        configuration = self.app.state.runtime_configuration.snapshot()
        configuration.persona.owner_qq_ids = []
        self.app.state.runtime_configuration.replace(configuration, configuration.revision)
        platform = FakePlatform()
        dispatcher = OutboxDispatcher(self.store, platform, prompt_guard=self.app.state.prompt_guard)
        await dispatcher.dispatch_once()
        self.assertEqual(platform.sent[0][1], '在呢。')

    async def test_self_growth_persists_without_extra_model_call_and_is_manageable_in_console(self):
        configuration = self.app.state.runtime_configuration.snapshot()
        configuration.persona.self_update_enabled = True
        self.app.state.runtime_configuration.replace(configuration, configuration.revision)
        revision = self.app.state.runtime_configuration.snapshot().revision
        self.provider.generate.return_value = action({'final_action': 'REPLY',
            'reply_brief': {'reply_text': '嗯，我更想直接说出来。'},
            'persona_note': {'text': '更愿意直接表达关心'}})
        await self.process(make_event(message_id='growth-first', text='Nova 你可以坦率一点'))
        self.provider.generate.assert_awaited_once()
        self.assertEqual(self.app.state.runtime_configuration.snapshot().revision, revision)
        self.provider.generate.reset_mock()
        self.provider.generate.return_value = action({'final_action': 'REPLY', 'reply_brief': {'reply_text': '在呢。'}})
        await self.process(make_event(message_id='growth-second', text='Nova 在吗'))
        self.assertIn('更愿意直接表达关心', self.provider.generate.await_args.args[0][0]['content'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
            self.assertEqual((await client.get('/api/v1/config/persona/growth')).status_code, 401)
            headers = {'Authorization': 'Bearer admin-test-token-12345'}
            result = await client.get('/api/v1/config/persona/growth', headers=headers)
            note = result.json()['data']['items'][0]
            self.assertEqual(note['text'], '更愿意直接表达关心')
            toggled = await client.put('/api/v1/config/persona/growth/' + note['id'], headers=headers, json={'active': False})
            self.assertEqual(toggled.status_code, 200)
            self.assertFalse(toggled.json()['data']['active'])
        self.assertFalse(self.app.state.runtime_configuration.persona_growth.snapshot('group:10001'))

    async def test_uncertain_answer_is_not_forced_into_web_search_by_keywords(self):
        self.provider.generate.return_value = action({'final_action': 'REPLY',
            'reply_brief': {'reply_text': '没有具体角色名，暂时不能确认你在说哪几个。'}})
        event = make_event(message_id='lookup-needs-context', text='Nova 查一下这几个角色是什么定位和强度')
        await self.process(event)
        self.assertEqual(self.provider.generate.await_count, 1)
        self.assertEqual(next(iter(self.store.outboxes.values()))['text'],
                         '没有具体角色名，暂时不能确认你在说哪几个。')

    async def test_past_agreement_question_records_an_actual_memory_tool_in_chat_flow(self):
        self.provider.generate.side_effect = [
            action({'tool_call': {'tool_call_id': 'recall', 'name': 'memory.search',
                                 'arguments': {'query': '约定的称呼'}}}),
            action({'final_action': 'REPLY', 'reply_brief': {'reply_text': '没查到那次约定的记忆，暂时不能确认。'}}),
        ]
        event = make_event(message_id='memory-tool-in-flow', text='Nova 你还记得我们之前约定的称呼吗')
        await self.process(event)
        stages = self.app.state.flow_journal.query([event.id])[event.id]
        self.assertTrue(any(row['kind'] == 'tool.start' and row.get('tool') == 'memory.search' for row in stages))
        self.assertTrue(any(row['kind'] == 'tool.end' and row.get('tool') == 'memory.search' for row in stages))
        self.assertEqual(self.provider.generate.await_count, 2)

    async def test_role_lookup_and_verified_mention_reach_outbox_as_real_segments(self):
        async def members(action_name, arguments):
            if action_name == 'get_login_info':
                return {'user_id': 99999}
            return [
                {'user_id': 99999, 'nickname': 'Nova', 'role': 'member'},
                {'user_id': 12345, 'nickname': '请求者', 'role': 'member'},
                {'user_id': 55667788, 'nickname': '实际群主', 'role': 'owner'},
                {'user_id': 66778899, 'nickname': '群主', 'role': 'member'},
            ]

        self.app.state.v3.thinking.tools.platform = SimpleNamespace(ready=True, call=AsyncMock(side_effect=members))
        self.provider.generate.side_effect = [
            action({'tool_call': {'tool_call_id': 'owner', 'name': 'group.members', 'arguments': {'query': '', 'role': 'owner'}}}),
            action({'final_action': 'REPLY', 'reply_brief': {'reply_text': '找你一下。', 'mention_ids': ['55667788']}}),
        ]
        event = make_event(message_id='mention-group-owner', text='Nova 你艾特一下群主')
        await self.process(event)
        outgoing = SimpleNamespace(ready=True, send=AsyncMock(), send_segments=AsyncMock(return_value='9001'))
        dispatcher = OutboxDispatcher(self.store, outgoing, prompt_guard=self.app.state.prompt_guard)
        await dispatcher.dispatch_once()
        self.assertEqual(outgoing.send_segments.await_args.args[3], [{'type': 'at', 'qq': '55667788'}])
        outgoing.send.assert_not_awaited()
        self.assertEqual(self.store.outboxes[event.id]['status'], 'sent')
        await dispatcher.dispatch_once()
        outgoing.send_segments.assert_awaited_once()

    async def test_one_unrelated_new_message_does_not_cancel_initiative_reply(self):
        self.app.state.features.initiative_eligible = AsyncMock(return_value=True)
        source = make_event(text='群里的活动真不错', message_id='open-topic', mention=False)
        await self.process(source)
        unrelated = make_event(text='明天会下雨吗', message_id='other-topic', mention=False,
                               actor='12346')
        await self.store.publish(unrelated, True)
        platform = FakePlatform()
        dispatcher = OutboxDispatcher(self.store, platform)
        await dispatcher.dispatch_once()
        self.assertEqual(len(platform.sent), 1)

    async def test_hard_gates_and_disabled_initiative_still_spend_no_model_calls(self):
        await self.store.update_policy('10001', GroupPolicy(initiative=False))
        await self.process(make_event(mention=False))
        await self.store.update_policy('10001', GroupPolicy(enabled=False))
        await self.process(make_event(message_id='disabled'))
        self.provider.generate.assert_not_awaited()
        self.assertEqual(self.store.outboxes, {})

    async def test_missing_final_text_is_repaired_by_thinking_without_reply_model(self):
        self.provider.generate.side_effect = [
            action({'final_action': 'REPLY', 'reply_brief': {'key_points': ['在呢']}}),
            action({'final_action': 'REPLY', 'reply_brief': {'reply_text': '在呢。'}}),
        ]
        await self.process(make_event())
        self.assertEqual([call.args[1] for call in self.provider.generate.await_args_list], ['thinking', 'thinking'])
        self.assertEqual(next(iter(self.store.outboxes.values()))['text'], '在呢。')

    async def test_preparation_uses_local_ranking_and_loads_task_indexes_on_demand(self):
        features = self.app.state.features
        features.evidence = AsyncMock(return_value={})
        features.rank_memories = AsyncMock(return_value=[])
        features.images.context = AsyncMock(return_value=[])
        self.app.state.v2.shared_work = AsyncMock()
        await self.process(make_event())
        self.assertFalse(features.evidence.await_args.kwargs['use_embeddings'])
        self.assertFalse(features.rank_memories.await_args.kwargs['use_embeddings'])
        self.assertTrue(all(call.kwargs['wait_seconds'] == 0 for call in features.images.context.await_args_list))
        self.assertTrue(all(not call.args[1] for call in features.images.context.await_args_list))
        self.app.state.v2.shared_work.assert_not_awaited()

    async def test_quote_is_expanded_and_later_messages_do_not_rewrite_current_turn(self):
        earlier=make_event(message_id='earlier-context',text='说的是蓝色那张图',mention=False)
        await self.store.publish(earlier,True)
        current=replace(make_event(message_id='current-context',text='这个怎么样'),
                        reply_to_id=earlier.message_id,occurred_at=earlier.occurred_at+1)
        later=replace(make_event(message_id='later-context',text='换个话题谈篮球',mention=False),
                      occurred_at=earlier.occurred_at+2)
        await self.store.publish(later,True)
        await self.process(current)
        messages=self.provider.generate.await_args.args[0]
        contents='\n'.join(row['content'] for row in messages)
        self.assertIn('当前消息明确引用的原文',contents)
        self.assertIn('说的是蓝色那张图',contents)
        self.assertNotIn('换个话题谈篮球',contents)

    async def test_integrated_protocol_and_kernel_are_present_once_after_router_preparation(self):
        await self.process(make_event())
        messages = self.provider.generate.await_args.args[0]
        guard = self.app.state.v3.thinking.prompt_guard
        self.assertEqual(guard.prepare_messages(messages, 'thinking'), messages)
        systems = '\n'.join(row['content'] for row in messages if row['role'] == 'system')
        self.assertEqual(systems.count('【不可修改的系统边界】'), 1)
        self.assertEqual(systems.count('【阶段输出协议：agent_integrated】'), 1)
        self.assertNotIn('【阶段输出协议：agent_thinking】', systems)

    async def test_valid_tool_receipt_can_be_replied_to_without_a_rewrite_model(self):
        model = SimpleNamespace(generate=AsyncMock(side_effect=[
            action({'tool_call': {'tool_call_id': 'lookup', 'name': 'memory.search', 'arguments': {'query': '约定'}}}),
            action({'final_action': 'REPLY', 'reply_brief': {
                'reply_text': '约好周末再讨论。', 'evidence_ids': ['lookup'], 'attachment_ids': ['real-image'],
            }}),
        ]))
        tools = SimpleNamespace(execute=AsyncMock(return_value={
            'text': '周末讨论', '_attachment_ids': ['real-image'],
        }))
        outcome = await ThinkingLoop(model, tools).run([], {'reply_mode': 'integrated'})
        self.assertEqual(outcome.final_action, 'REPLY')
        reply = await ReplyWriter(model).render(outcome.brief)
        self.assertEqual(reply.text, '约好周末再讨论。')
        self.assertEqual(reply.attachment_ids, ['real-image'])
        self.assertEqual(model.generate.await_count, 2)
        tools.execute.assert_awaited_once()

    async def test_final_text_cannot_bypass_evidence_attachment_or_staged_boundaries(self):
        for extra, context in [
            ({'evidence_ids': ['forged']}, {'reply_mode': 'integrated'}),
            ({'attachment_ids': ['forged']}, {'reply_mode': 'integrated'}),
            ({'artifact_names': ['forged.txt']}, {'reply_mode': 'integrated'}),
            ({}, {}),
            ({'reply_text': 123}, {'reply_mode': 'integrated'}),
            ({'reply_text': 'x' * 801}, {'reply_mode': 'integrated'}),
        ]:
            with self.subTest(extra=extra, context=context):
                model = SimpleNamespace(generate=AsyncMock(return_value=action({
                    'final_action': 'REPLY', 'reply_brief': {'reply_text': '声称已经执行', **extra},
                })))
                outcome = await ThinkingLoop(model, SimpleNamespace(execute=AsyncMock())).run([], context)
                self.assertEqual(outcome.final_action, 'FAILED')
                self.assertIsNone(outcome.brief)

    async def test_direct_text_is_filtered_before_persistent_delivery(self):
        self.provider.generate.return_value = action({'final_action': 'REPLY', 'reply_brief': {
            'reply_text': '<think>内部推理</think>在呢 [CQ:at,qq=12345]',
        }})
        await self.process(make_event())
        text = next(iter(self.store.outboxes.values()))['text']
        self.assertNotIn('内部推理', text)
        self.assertNotIn('[CQ:', text)

    async def test_persisted_final_reply_is_reused_without_another_generation(self):
        event = make_event()
        await self.process(event)
        await self.app.state.runtime.process(event)
        self.provider.generate.assert_awaited_once()
        self.assertEqual(len(self.store.outboxes), 1)

    async def test_completed_work_resumes_integrated_reply_with_verified_artifact(self):
        from osa.application.v2.worker import WorkAgent
        from test_v2 import FILES, report

        self.app.state.settings.work_enabled = True
        await self.store.update_policy('10001', GroupPolicy(sandbox=True))
        self.provider.generate.side_effect = [
            action({'tool_call': {'tool_call_id': 'make', 'name': 'work.submit',
                                 'arguments': {'goal': '生成加法程序并交付源码'}}}),
            action({'final_action': 'REPLY', 'reply_brief': {'reply_text': '任务已安排。'}}),
            action({'final_action': 'REPLY', 'reply_brief': {
                'reply_text': '程序已完成，源码见附件。', 'artifact_names': ['project.zip'],
            }}),
        ]
        await self.process(make_event(text='编写加法程序并发送源码'))
        claimed = self.app.state.v2.registry.claim('test-worker')
        self.assertIsNotNone(claimed)
        executor = SimpleNamespace(run=AsyncMock(return_value=report()))
        await WorkAgent(self.app.state.v2.registry, AsyncMock(return_value=FILES),
                        executor, self.app.state.settings.workspace_dir).run(claimed)
        self.assertTrue(await self.app.state.v3.resume_once())
        self.assertEqual([call.args[1] for call in self.provider.generate.await_args_list],
                         ['thinking', 'thinking', 'thinking'])
        delivered = [row for row in self.store.outboxes.values() if row['text'] == '程序已完成，源码见附件。']
        self.assertEqual(len(delivered), 1)
        snapshot = self.store.decisions[delivered[0]['decision_id']]['snapshot']
        self.assertTrue(snapshot['deliver_work_file'])
        self.assertEqual(snapshot['work_task_id'], claimed['id'])

