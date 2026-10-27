import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from osa.application.chat_stream import ChatStreamCoordinator, SilentLearningService
from osa.application.v3.tools import AgentTools
from osa.domain.entities import GroupPolicy, Memory, Scope
from osa.domain.features import Record
from osa.infrastructure.features.repository import MemoryFeatureRepository
from osa.infrastructure.memory_store import InMemoryStore
from test_core import demo_settings, make_event


class FakeModel:
    def __init__(self, values):
        self.values = list(values)

    async def generate(self, messages, purpose="chat"):
        return type("R", (), {"text": self.values.pop(0), "provider": "test", "model": purpose})()

    async def close(self):
        pass


class RebuiltFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_batches_messages_and_silent_learning_writes_shared_candidate(self):
        stream = ChatStreamCoordinator(context_limit=3)
        first = make_event(message_id="stream-1", text="Python 3.12 的 tomllib 可以读取 TOML", mention=False)
        second = make_event(message_id="stream-2", text="这个方法在标准库里", mention=False)
        state = await stream.observe(first)
        await stream.observe(second)
        self.assertEqual(state.version, 2)
        self.assertEqual(len((await stream.snapshot(first.destination))["messages"]), 2)

        store = InMemoryStore()
        await store.initialize(["10001"])
        await store.update_policy("10001", GroupPolicy(memory_extract=True))
        await store.publish(first, True)
        await store.publish(second, True)
        repo = MemoryFeatureRepository()
        model = FakeModel([json.dumps({"knowledge":[{"content":"Python 3.12 的 tomllib 在标准库中读取 TOML","source_ids":[first.id],"confidence":0.95,"shareable":True}],"facts":[]}, ensure_ascii=False)])
        learner = SilentLearningService(repo, store, model, window_size=2)
        await learner.observe(first, await store.policy(first.destination))
        await learner.observe(second, await store.policy(second.destination))
        await asyncio.sleep(0.02)
        rows = await repo.find("knowledge_candidate", status="shared", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].group_id)
        self.assertEqual(rows[0].payload["trust_status"], "unverified")
        self.assertEqual(rows[0].payload["source_ids"], [first.id])

    async def test_learning_keeps_person_fact_group_scoped(self):
        store = InMemoryStore()
        await store.initialize(["10001"])
        await store.update_policy("10001", GroupPolicy(memory_extract=True))
        repo = MemoryFeatureRepository()
        event = make_event(message_id="fact-1", text="我喜欢修复编译器", mention=False)
        await store.publish(event, True)
        model = FakeModel([json.dumps({"knowledge":[],"facts":[{"subject_id":"12345","content":"喜欢修复编译器","source_ids":[event.id],"confidence":0.98}]})])
        learner = SilentLearningService(repo, store, model, window_size=1)
        await learner.observe(event, await store.policy(event.destination))
        await asyncio.sleep(0.02)
        memories = await store.own_memories("12345")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].scope, Scope.LONG_GROUP)
        self.assertEqual(memories[0].origin_group_id, "10001")

    async def test_deepseek_tool_persists_session_and_attachment_request(self):
        store = InMemoryStore()
        await store.initialize(["10001"])
        await store.update_policy("10001", GroupPolicy(research_external=True, sandbox=True))
        from osa.infrastructure.v3.registry import Registry
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(str(Path(directory) / "registry.db"))
            settings = demo_settings().model_copy(update={"v2_enabled": True, "v3_enabled": True,
                "deepseek_web_enabled": True, "research_enabled": True, "work_enabled": True})
            from osa.application.v2.service import V2Service
            v2 = V2Service(registry, settings)
            source = make_event(message_id="deepseek-source", text="这张图是什么角色", mention=True)
            await store.publish(source, True)
            registry.begin_thinking("session", {"actor_id": source.actor_id, "group_id": "10001", "source_id": source.id})
            browser = AsyncMock()
            browser.ask.return_value = {"id": "queued", "status": "queued"}
            tools = AgentTools(v2, store, browser=browser)
            result = await tools.execute("deepseek.consult", {"question": "图中是什么角色？", "new_conversation": True},
                {"session_id": "session", "tool_call_id": "call-1", "actor_id": source.actor_id,
                 "group_id": "10001", "source_id": source.id, "deadline": 9999999999,
                 "attachments": [{"name": "character.png", "mime_type": "image/png", "data_base64": "aGVsbG8="}]})
            self.assertEqual(result["status"], "QUEUED")
            row = registry.records("night_research", source.actor_id, "10001")[0]
            self.assertTrue(row["payload"]["interactive"])
            self.assertEqual(row["payload"]["attachments"], [])

    async def test_browser_request_contract_carries_session_and_pasteable_attachment(self):
        from osa.infrastructure.features.clients import BrowserWorkerClient
        client = BrowserWorkerClient("http://browser", "worker-token")
        client.request = AsyncMock(return_value={"id": "q1", "status": "queued"})
        await client.ask("看这张图", "q1", session_id="chat-1", new_conversation=False,
                         attachments=[{"name": "角色.png", "mime_type": "image/png", "data_base64": base64.b64encode(b"img").decode()}])
        payload = client.request.await_args.args[2]
        self.assertEqual(payload["session_id"], "chat-1")
        self.assertFalse(payload["new_conversation"])
        self.assertEqual(payload["attachments"][0]["name"], "角色.png")

