"""SQLite/migration integration tests, enabled after installing project dependencies."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

AVAILABLE = all(importlib.util.find_spec(name) for name in ("sqlalchemy", "aiosqlite", "alembic"))


@unittest.skipUnless(AVAILABLE, "Install project dependencies to test SQL persistence")
class PersistentAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from osa.infrastructure.store import SQLStore
        self.directory = tempfile.TemporaryDirectory()
        self.store = SQLStore(f"sqlite+aiosqlite:///{self.directory.name}/integration.db")
        await self.store.initialize(["10001", "10002"], True)

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_transactional_outbox_dedup_and_restart_recovery(self):
        from test_core import make_event, FakeResources
        from osa.application.resources import ResourceService
        from osa.application.runtime import AgentRuntime
        from osa.infrastructure.models import FakeModel
        event = make_event()
        self.assertTrue(await self.store.publish(event, True))
        self.assertFalse(await self.store.publish(event, True))
        runtime = AgentRuntime(self.store, FakeModel(), ResourceService(self.store, FakeResources()))
        await runtime.process((await self.store.claim_events(10))[0])
        item = await self.store.claim_outbox()
        self.assertIsNotNone(item)
        await self.store.initialize(["10001", "10002"])
        self.assertIsNone(await self.store.claim_outbox())
        self.assertEqual((await self.store.stats())["outbox"], {"unknown": 1})

    async def test_permission_predicates_filter_before_returning_content(self):
        from test_core import make_event
        from osa.domain.entities import Destination, GroupPolicy, Memory, Scope
        event = make_event()
        await self.store.publish(event, True)
        await self.store.add_memory(Memory("group", "群资料", Scope.LONG_GROUP, "12345", "10001", event.id))
        await self.store.add_memory(Memory("personal", "本人项目", Scope.PERSONAL_OWNER, "12345", "10001", event.id))
        dest = Destination("group", "10002")
        await self.store.update_policy("10002", GroupPolicy(cross_memory=True))
        await self.store.set_consent("12345", True)
        self.assertEqual(await self.store.visible_memories("12345", dest, "项目"), [])
        await self.store.set_consent("12345", True, "10002")
        self.assertEqual([m.id for m in await self.store.visible_memories("12345", dest, "项目")], ["personal"])
        self.assertEqual(await self.store.visible_memories("999", dest, "项目"), [])

    async def test_memory_write_preserves_oldest_and_cleans_duplicates(self):
        from osa.domain.entities import Memory, Scope
        from osa.infrastructure.schema import MemoryRow
        from test_core import make_event
        event = make_event()
        await self.store.publish(event, True)
        async with self.store.session.begin() as session:
            session.add_all([MemoryRow(id=memory_id, content=content, scope=Scope.LONG_GROUP,
                subject_id=event.actor_id, origin_group_id='10001', source_event_id=event.id,
                status='active', version=1)
                for memory_id, content in [('old-a', '喜欢 Python。'), ('old-b', '喜欢Python')]])
        saved = await self.store.add_memory(Memory('new', '喜欢 Python', Scope.LONG_GROUP,
            event.actor_id, '10001', event.id))
        self.assertEqual(saved.id, 'old-a')
        self.assertEqual((await self.store.memories_by_ids(['old-b']))[0].status, 'deleted')
        self.assertEqual(len(await self.store.own_memories(event.actor_id)), 1)

    async def test_collection_disabled_never_writes_original_text(self):
        from sqlalchemy import select
        from osa.infrastructure.schema import EventRow
        from test_core import make_event
        event = make_event(text="不落盘原文")
        await self.store.publish(event, False)
        async with self.store.session() as session:
            row = await session.scalar(select(EventRow))
            self.assertEqual(row.payload["text"], "")
        self.assertEqual((await self.store.claim_events(1))[0].text, event.text)

    async def test_single_connection_wal_and_chunked_vectors_preserve_scores(self):
        from sqlalchemy import text
        from osa.infrastructure.store import SQLStore
        from osa.infrastructure.features.sql_repository import SQLFeatureRepository
        from osa.infrastructure.features.repository import cosine
        from osa.domain.features import Record
        store = SQLStore(f"sqlite+aiosqlite:///{self.directory.name}/lowmem.db", pool_size=1)
        try:
            await store.initialize(["10001"], True)
            async with store.session() as session:
                self.assertEqual(await session.scalar(text("PRAGMA journal_mode")), "wal")
                self.assertEqual(await session.scalar(text("PRAGMA cache_size")), -2048)
            self.assertEqual(store.engine.pool.size(), 1)
            repo = SQLFeatureRepository(store)
            ids = []
            for number in range(70):
                rid = f"vector-{number}"
                ids.append(rid)
                await repo.put(Record(rid, "chunk", "1", "10001", "active", {}))
                await repo.vector_put(rid, "model", [float(number), 1.0, 0.0])
            scores = await repo.vector_scores(ids, "model", [1.0, 1.0, 0.0])
            self.assertEqual(len(scores), 70)
            self.assertAlmostEqual(scores["vector-69"], cosine([69.0, 1.0, 0.0], [1.0, 1.0, 0.0]))
        finally:
            await store.close()

    async def test_recall_and_memory_deletion_propagate(self):
        from test_core import make_event
        from osa.domain.entities import Memory, Scope
        event = make_event()
        await self.store.publish(event, True)
        await self.store.add_memory(Memory("m", "项目", Scope.LONG_GROUP, "12345", "10001", event.id))
        await self.store.recall(replace(event, event_type="message.recalled"))
        self.assertEqual(await self.store.own_memories("12345"), [])
        self.assertEqual((await self.store.memories_by_ids(["m"]))[0].content, "")

    async def test_notice_snapshot_versions_and_removal(self):
        await self.store.save_resources("10001", "notices", [{"id": "1", "body": "旧"}])
        await self.store.save_resources("10001", "notices", [{"id": "1", "body": "新"}])
        self.assertEqual((await self.store.resources("10001", "notices"))[0]["version"], 2)
        await self.store.save_resources("10001", "notices", [])
        self.assertEqual(len(await self.store.resources("10001", "notices")), 1)
        await self.store.save_resources("10001", "notices", [])
        self.assertEqual(await self.store.resources("10001", "notices"), [])


@unittest.skipUnless(AVAILABLE, "Install project dependencies to test Alembic migrations")
class MigrationTests(unittest.TestCase):
    def test_upgrade_downgrade_upgrade(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "OSA_DATABASE_URL": f"sqlite+aiosqlite:///{directory}/migration.db"}
            for arguments in [("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")]:
                result = subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=root, env=env, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
