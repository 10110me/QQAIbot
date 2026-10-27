import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

from osa.application.v3.thinking import ThinkingLoop
from osa.domain.entities import ModelResult
from osa.infrastructure.flow_journal import FlowJournal
from osa.main import create_app
from test_core import demo_settings, make_event


class FlowTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_rounds_visible_before_completion_and_tool_rethinking_order(self):
        journal = FlowJournal('.', memory_only=True)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def generate(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                value = {'tool_call': {'tool_call_id': 'search', 'name': 'memory.search', 'arguments': {'query': 'private content'}}}
            else:
                value = {'tool_call': {'tool_call_id': 'r', 'name': 'reply', 'arguments': {'key_points': ['private answer']}}}
            return ModelResult(json.dumps(value), 'test', 'thinking-model')

        loop = ThinkingLoop(SimpleNamespace(generate=generate), SimpleNamespace(execute=AsyncMock(return_value={'memories': []})))
        loop.flow = journal
        job = asyncio.create_task(loop.run([], {'source_id': 'source', 'evidence_ids': []}, 's1'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual([r['kind'] for r in journal.query(['source'])['source']], ['thinking.start'])
            release.set()
            self.assertEqual((await job).final_action, 'REPLY')
            rows = journal.query(['source'])['source']
            self.assertEqual([r['kind'] for r in rows], ['thinking.start','thinking.end','tool.start','tool.end','thinking.start','thinking.end','action'])
            self.assertEqual(rows[-1]['action'], 'REPLY')
            self.assertEqual(rows[-2]['step'], 2)
            self.assertNotIn('private', str(rows))
        finally:
            release.set()
            await job
            journal.close()

    async def test_stages_persist_and_do_not_accept_body_or_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = FlowJournal(folder)
            await journal.emit('event', 'tool.end', tool='memory.search', arguments={'token':'secret'}, response='secret', count=3)
            journal.close()
            restarted = FlowJournal(folder)
            rows = restarted.query(['event'])['event']
            self.assertEqual(rows[0]['count'], 3)
            self.assertNotIn('secret', str(rows))
            self.assertFalse(restarted.query(['other']))
            restarted.close()

    async def test_same_timestamp_preserves_recording_order(self):
        import time
        journal = FlowJournal('.', memory_only=True)
        try:
            with patch('osa.infrastructure.flow_journal.time.time', return_value=time.time()):
                for kind in ['thinking.start', 'thinking.end', 'tool.start', 'tool.end', 'thinking.start']:
                    await journal.emit('same', kind)
            rows = journal.query(['same'])['same']
            self.assertEqual([r['kind'] for r in rows], ['thinking.start', 'thinking.end', 'tool.start', 'tool.end', 'thinking.start'])
            self.assertEqual([r['seq'] for r in rows], sorted(r['seq'] for r in rows))
        finally:
            journal.close()

    async def test_api_timeline_only_for_retained_visible_messages(self):
        app = create_app(demo_settings())
        store = app.state.store
        await store.initialize(['10001'])
        events = [replace(make_event(message_id=str(i)), text='body-'+str(i)) for i in range(3)]
        for i, event in enumerate(events):
            await store.publish(event, i != 0)
            await app.state.flow_journal.emit(event.id, 'thinking.start', session_id='private-session-'+str(i), step=1)
        await store.recall(replace(events[1], event_type='message.recalled'))
        day = datetime.fromtimestamp(events[0].occurred_at, ZoneInfo('Asia/Shanghai')).date().isoformat()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test', headers={'Authorization':'Bearer admin-test-token-12345'}) as client:
                response = await client.get('/api/v1/operations/streams/events', params={'day':day,'conversation':'group:10001'})
                self.assertEqual(response.headers['cache-control'], 'no-store')
                rows = {r['id']:r for r in response.json()['data']['items']}
                self.assertFalse(rows[events[0].id]['flow'])
                self.assertFalse(rows[events[1].id]['flow'])
                self.assertEqual(rows[events[2].id]['flow'][0]['kind'], 'thinking.start')
                self.assertNotIn('body-0', str(rows))
                self.assertNotIn('body-1', str(rows))
        finally:
            await app.state.features.learning.shutdown()
            await store.close()
            app.state.flow_journal.close()
