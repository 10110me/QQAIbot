import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from osa.domain.entities import Destination, Event, GroupPolicy, Memory, Scope, utcnow
from osa.infrastructure.memory_store import InMemoryStore
from osa.infrastructure.store import SQLStore


class MemorySearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyword_search_ranks_matches_and_keeps_group_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            stores = [InMemoryStore(), SQLStore(f"sqlite+aiosqlite:///{Path(folder) / 'memory.db'}")]
            for store in stores:
                with self.subTest(store=type(store).__name__):
                    await store.initialize(['10001', '10002'], isinstance(store, SQLStore))
                    source = Event('memory-source', 'memory-source', 'test', 'message.created',
                                   Destination('group', '10001'), '20001', 'memory-source',
                                   '我维护 Python 项目', [], utcnow().timestamp())
                    await store.publish(source, True)
                    await store.add_memory(Memory('unrelated', '喜欢听音乐', Scope.LONG_GROUP,
                                                  '20001', '10001', source.id))
                    await store.add_memory(Memory('relevant', '我维护 Python 项目，并负责测试',
                                                  Scope.LONG_GROUP, '20001', '10001', source.id))
                    await store.add_memory(Memory('other-group', '另一个群的 Python 项目秘密',
                                                  Scope.LONG_GROUP, '20001', '10002', source.id))
                    found = await store.visible_memories('20001', source.destination, 'Python 项目')
                    self.assertEqual([memory.id for memory in found], ['relevant'])
                    self.assertEqual(await store.visible_memories('20001', source.destination, '不存在的词'), [])
                    await store.close()

    async def test_global_group_memory_filters_people_and_keeps_private_scopes_private(self):
        with tempfile.TemporaryDirectory() as folder:
            for store in [InMemoryStore(), SQLStore(f"sqlite+aiosqlite:///{Path(folder) / 'global.db'}")]:
                with self.subTest(store=type(store).__name__):
                    try:
                        await store.initialize(['10001', '10002'], isinstance(store, SQLStore))
                        source = Event('source', 'source', 'test', 'message.created',
                            Destination('group', '10001'), '20001', 'source', 'Python', [], utcnow().timestamp())
                        await store.publish(source, True)
                        private = replace(source, id='private', dedupe_key='private',
                            destination=Destination('private', '20001'), message_id='private')
                        await store.publish(private, True)
                        for memory in [
                            Memory('personal', '维护 Python 项目', Scope.LONG_GROUP, '20001', '10001', source.id),
                            Memory('daily', '今天讨论了数据库', Scope.DAILY_GROUP, '20001', '10001', source.id),
                            Memory('other', '喜欢篮球运动', Scope.LONG_GROUP, '20002', '10001', source.id),
                            Memory('private', '私聊内容', Scope.PRIVATE_USER, '20001', None, private.id),
                            Memory('owner', '个人授权内容', Scope.PERSONAL_OWNER, '20001', '10001', source.id),
                            Memory('expired', '过期内容', Scope.LONG_GROUP, '20001', '10001', source.id, expires_at=1),
                        ]:
                            await store.add_memory(memory)
                        destination = Destination('group', '10002')
                        self.assertEqual(await store.visible_memories('20001', destination, '', subject_id='20001'), [])
                        store.permissions.global_group_memory = True
                        found = await store.visible_memories('20001', destination, '', subject_id='20001')
                        self.assertEqual({memory.id for memory in found}, {'personal', 'daily'})
                        self.assertEqual([memory.id for memory in await store.visible_memories(
                            '20001', destination, 'Python', subject_id='20001')], ['personal'])
                        self.assertEqual([memory.id for memory in await store.visible_memories(
                            '20001', private.destination, '')], ['private'])
                        self.assertEqual(await store.visible_memories('20002', Destination('private', '20002'), ''), [])
                        for policy in (GroupPolicy(collect_messages=False), GroupPolicy(enabled=False)):
                            await store.update_policy('10001', policy)
                            self.assertEqual(await store.visible_memories('20001', destination, '', subject_id='20001'), [])
                        await store.update_policy('10001', GroupPolicy())
                        await store.recall(replace(source, event_type='message.recalled'))
                        self.assertEqual(await store.visible_memories('20001', destination, '', subject_id='20001'), [])
                    finally:
                        await store.close()

    async def test_subject_browsing_prefers_recent_retained_memories(self):
        with tempfile.TemporaryDirectory() as folder:
            for store in [InMemoryStore(), SQLStore(f"sqlite+aiosqlite:///{Path(folder) / 'recent.db'}")]:
                with self.subTest(store=type(store).__name__):
                    try:
                        await store.initialize(['10001'], isinstance(store, SQLStore))
                        source = Event('old', 'old', 'test', 'message.created', Destination('group', '10001'),
                            '20001', 'old', '旧事', [], utcnow().timestamp() - 100)
                        newer = replace(source, id='new', dedupe_key='new', message_id='new',
                            occurred_at=utcnow().timestamp())
                        for event in (source, newer):
                            await store.publish(event, True)
                        await store.add_memory(Memory('old', '曾经学过 Python', Scope.LONG_GROUP, '20001', '10001', source.id))
                        await store.add_memory(Memory('new', '最近修好了手机', Scope.LONG_GROUP, '20001', '10001', newer.id))
                        found = await store.visible_memories('20001', source.destination, '', 1, subject_id='20001')
                        self.assertEqual([memory.id for memory in found], ['new'])
                    finally:
                        await store.close()

    async def test_shared_memory_is_checked_again_at_send_and_replay(self):
        from osa.application.resources import ResourceService
        from osa.application.runtime import AgentRuntime, OutboxDispatcher
        from osa.infrastructure.models import FakeModel
        from test_core import FakePlatform, FakeResources, make_event

        store = InMemoryStore()
        await store.initialize(['10001', '10002'])
        store.permissions.global_group_memory = True
        source = make_event(message_id='source', mention=False)
        await store.publish(source, True)
        await store.add_memory(Memory('shared', '维护 Python 项目', Scope.LONG_GROUP,
            source.actor_id, '10001', source.id))
        runtime = AgentRuntime(store, FakeModel(), ResourceService(store, FakeResources()))
        platform = FakePlatform()
        dispatcher = OutboxDispatcher(store, platform)
        self.assertIs(runtime.permissions, store.permissions)
        self.assertIs(dispatcher.permissions, store.permissions)
        for message_id, revoke in [('allowed', False), ('blocked', True)]:
            event = make_event(message_id=message_id, group='10002', text='Python')
            await store.publish(event, True)
            await runtime.process(event)
            snapshot = store.decisions[event.id]['snapshot']
            self.assertIn('shared', snapshot['memory_ids'])
            if revoke:
                await store.update_policy('10001', GroupPolicy(collect_messages=False))
            await dispatcher.dispatch_once()
            self.assertEqual(len(platform.sent), 1)
            followup = replace(make_event(message_id='followup', group='10002'), occurred_at=utcnow().timestamp() + 1)
            history = await store.context(followup, 40)
            self.assertEqual(any(message['role'] == 'assistant' for message in history), not revoke)
            if revoke:
                with self.assertRaises(PermissionError):
                    await runtime.replay(snapshot)

    async def test_global_memory_configuration_and_console_visibility(self):
        import httpx
        from osa.main import create_app
        from test_core import demo_settings, make_event

        app = create_app(demo_settings().model_copy(update={'global_group_memory': True}))
        store = app.state.store
        await store.initialize(['10001'])
        source = make_event(message_id='console-memory')
        await store.publish(source, True)
        await store.add_memory(Memory('shared', '维护 Python 项目', Scope.LONG_GROUP,
            source.actor_id, '10001', source.id))
        self.assertTrue(store.permissions.global_group_memory)
        self.assertIs(app.state.runtime.permissions, store.permissions)
        self.assertIs(app.state.dispatcher.permissions, store.permissions)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test',
                headers={'Authorization': 'Bearer admin-test-token-12345'}) as client:
            response = await client.get('/api/v1/operations/memories', params={'kind': 'personal'})
            self.assertEqual(response.status_code, 200)
            data = response.json()['data']
            self.assertTrue(data['global_group_memory'])
            self.assertEqual(data['items'][0]['visibility'], 'shared_groups')
            self.assertEqual(data['items'][0]['group_id'], '10001')
        await app.state.features.learning.shutdown()
        await store.close()
