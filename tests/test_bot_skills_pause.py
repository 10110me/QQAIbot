import unittest
from dataclasses import replace

import httpx

from osa.domain.entities import GroupPolicy
from osa.domain.features import Record
from osa.main import create_app
from test_core import demo_settings, make_event


class BotSkillsPauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_learning_keeps_history_and_blocks_activation(self):
        app = create_app(demo_settings())
        features = app.state.features
        await app.state.store.initialize(['10001'])
        skill = Record('old-skill', 'bot_skill_observation', None, '10001',
                       'candidate', {'bot_id': '77777', 'trigger_command': '/help'})
        await features.repo.put(skill)
        request = replace(make_event(text='/help', mention=False),
                          segments=[{'type': 'at', 'data': {'qq': '77777'}}])
        reply = make_event(message_id='bot-reply', actor='77777',
                           text='1. 点歌\n2. 帮助', mention=False)
        for event in (request, reply):
            await features.bot_skills.observe(event, GroupPolicy())
        self.assertFalse(features.bot_skills.enabled)
        self.assertEqual(features.bot_skills._pending, {})
        self.assertEqual(len(await features.repo.find('bot_skill_observation')), 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test',
                                     headers={'Authorization': 'Bearer admin-test-token-12345'}) as client:
            status = await client.get('/api/v1/bot-skills/status')
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()['data']['enabled'])
            result = await client.post('/api/v1/bot-skills/old-skill/decision', json={'allowed': True})
            self.assertEqual(result.status_code, 409)
        self.assertEqual((await features.repo.get('old-skill')).status, 'candidate')
