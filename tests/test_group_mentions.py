import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from osa.application.v3.thinking import ThinkingLoop
from osa.domain.entities import Destination
from osa.infrastructure.onebot import OneBotConnection
from test_persona_growth import response, tool


class GroupMentionTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return {'actor_id': '12345', 'group_id': '10001', 'source_id': 'source',
                'reply_mode': 'integrated', 'bot_actions_requested': True}

    async def test_real_mention_uses_a_verified_group_receipt_without_keyword_flags(self):
        model = SimpleNamespace(generate=AsyncMock(side_effect=[
            response({'tool_call': tool('group.members', 'lookup', query='', role='owner')}),
            response({'final_action': 'REPLY', 'reply_brief': {'reply_text': '找你一下。', 'mention_ids': ['55667788']}}),
        ]))
        tools = SimpleNamespace(features=None, execute=AsyncMock(return_value={
            'members': [{'user_id': '55667788', 'name': '实际群主', 'role': 'owner'}],
            '_mention_targets': [{'id': '55667788', 'role': 'owner'}]}))
        context = self.context()
        result = await ThinkingLoop(model, tools).run([], context)
        self.assertEqual(result.final_action, 'REPLY')
        self.assertEqual(result.brief.mention_ids, ['55667788'])
        self.assertEqual(context['dependency_snapshot']['verified_mention_ids'], ['55667788'])
        tools.execute.assert_awaited_once()
        self.assertEqual(model.generate.await_count, 2)

    async def test_unverified_member_cannot_be_mentioned(self):
        model = SimpleNamespace(generate=AsyncMock(side_effect=[
            response({'tool_call': tool('group.members', 'lookup', query='群主')}),
            response({'final_action': 'REPLY', 'reply_brief': {'reply_text': '找你一下。', 'mention_ids': ['66778899']}}),
        ]))
        tools = SimpleNamespace(features=None, execute=AsyncMock(return_value={
            '_mention_targets': [{'id': '55667788', 'role': 'owner'}]}))
        result = await ThinkingLoop(model, tools).run([], self.context())
        self.assertEqual(result.final_action, 'FAILED')
        self.assertIsNone(result.brief)

    async def test_onebot_emits_real_at_segments_and_rejects_private_or_all_mentions(self):
        platform = OneBotConnection()
        platform.call = AsyncMock(return_value={'message_id': 9001})
        result = await platform.send_segments(Destination('group', '10001'), '找你一下。', '',
                                             [{'type': 'at', 'qq': '55667788'}])
        self.assertEqual(result, '9001')
        sent = platform.call.await_args.args[1]['message']
        self.assertIn({'type': 'at', 'data': {'qq': '55667788'}}, sent)
        for destination, target in ((Destination('private', '12345'), '55667788'),
                                    (Destination('group', '10001'), 'all')):
            with self.subTest(destination=destination, target=target), self.assertRaises(PermissionError):
                await platform.send_segments(destination, '', '', [{'type': 'at', 'qq': target}])
