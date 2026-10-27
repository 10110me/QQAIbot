import concurrent.futures
import io
import json
import logging
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from sqlalchemy import event as sql_event, select

from osa.infrastructure.operations import ConsoleLogs
from osa.infrastructure.schema import DecisionRow
from osa.infrastructure.store import SQLStore
from osa.interfaces.api import require_admin
from osa.interfaces.operations import router
from test_core import make_event


class ConsoleLogPerformanceTests(unittest.TestCase):
    def test_reverse_reader_handles_unicode_chunk_boundaries_and_missing_newline(self):
        lines = ['较长内容' * 6000, '第二行', 'last']
        content = '\n'.join(lines).encode()
        actual = list(ConsoleLogs._reverse_lines(io.BytesIO(content), len(content)))
        self.assertEqual(actual, [line.encode() for line in reversed(lines)])

    def test_latest_page_stops_decoding_after_limit_and_keeps_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = ConsoleLogs(Path(directory), lambda: [])
            try:
                for index in range(300):
                    logs.emit(logging.LogRecord('osa.test', logging.INFO, '', 0, 'entry %s', (index,), None))
                with patch('osa.infrastructure.operations.json.loads', wraps=json.loads) as decode:
                    page = logs.query(limit=2)
                    self.assertEqual(decode.call_count, 3)
                self.assertEqual([row['message'] for row in page['items']], ['entry 299', 'entry 298'])
                older = logs.query(before=page['next'], limit=2)
                self.assertEqual([row['message'] for row in older['items']], ['entry 297', 'entry 296'])
            finally:
                logs.close()

    def test_disk_scan_does_not_hold_logging_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = ConsoleLogs(Path(directory), lambda: [])
            logs.emit(logging.LogRecord('osa.test', logging.INFO, '', 0, 'initial', (), None))
            scanning, release = threading.Event(), threading.Event()
            original = logs._reverse_lines

            def slow_lines(stream, end):
                scanning.set()
                release.wait(5)
                yield from original(stream, end)

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    with patch.object(logs, '_reverse_lines', side_effect=slow_lines):
                        query = executor.submit(logs.query)
                        try:
                            self.assertTrue(scanning.wait(2))
                            emit = executor.submit(logs.emit, logging.LogRecord('osa.test', logging.INFO, '', 0, 'during scan', (), None))
                            emit.result(timeout=1)
                        finally:
                            release.set()
                        self.assertEqual(query.result(timeout=2)['items'][0]['message'], 'initial')
                self.assertEqual(logs.query()['items'][0]['message'], 'during scan')
            finally:
                logs.close()


class ConsoleSQLPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLStore(f'sqlite+aiosqlite:///{self.directory.name}/console.db', pool_size=1)
        await self.store.initialize(['10001'], True)
        self.statements = []

        def capture(connection, cursor, statement, parameters, context, many):
            self.statements.append(statement)

        sql_event.listen(self.store.engine.sync_engine, 'before_cursor_execute', capture)

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_timeline_projects_only_anchor_not_entire_decision_snapshot(self):
        source = make_event()
        await self.store.publish(source, True)
        await self.store.record_decision(source, 'IGNORE', 'test', {'anchor_id': 'batch-anchor', 'large_private_body': 'x' * 100000}, None)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = lambda: None
        app.state.store = self.store
        day = datetime.fromtimestamp(source.occurred_at, ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.statements.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            response = await client.get('/api/v1/operations/streams/events', params={'day': day, 'conversation': source.destination.key})
        self.assertEqual(response.status_code, 200)
        body = response.json()['data']['items'][0]
        self.assertEqual(body['batch_anchor_id'], 'batch-anchor')
        self.assertEqual(body['decision']['action'], 'IGNORE')
        self.assertNotIn('large_private_body', response.text)
        decision_query = next(statement for statement in self.statements if 'FROM decision_logs' in statement)
        self.assertIn('JSON_EXTRACT', decision_query)
        self.assertNotIn(', decision_logs.snapshot', decision_query)

    async def test_snapshot_invalidation_is_batched_exact_and_does_not_hydrate_bodies(self):
        source = make_event()
        await self.store.publish(source, True)
        for index in range(205):
            current = make_event(message_id='snapshot-' + str(index))
            await self.store.publish(current, True)
            await self.store.record_decision(current, 'IGNORE', 'test', {
                'source_ids': [source.id] if index % 2 == 0 else [source.id + '-different'],
                'memory_ids': ['memory'], 'body': 'x' * 1000}, None)
        self.statements.clear()
        await self.store.recall(replace(source, event_type='message.recalled'))
        queries = [statement for statement in self.statements if 'FROM decision_logs' in statement]
        self.assertGreaterEqual(len(queries), 2)
        self.assertTrue(all('JSON_EXTRACT' in statement and 'LIMIT' in statement for statement in queries))
        async with self.store.session() as session:
            snapshots = (await session.scalars(select(DecisionRow.snapshot))).all()
        self.assertEqual(sum(not snapshot for snapshot in snapshots), 103)
        self.assertEqual(sum(snapshot.get('body') == 'x' * 1000 for snapshot in snapshots), 102)
        self.statements.clear()
        self.assertEqual(await self.store.purge_raw(source.occurred_at - 100), [])
        self.assertFalse(any('FROM decision_logs' in statement for statement in self.statements))
