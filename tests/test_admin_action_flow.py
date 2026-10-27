import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from osa.application.chat_stream import BotSkillLearningService
from osa.application.runtime import AgentRuntime
from osa.domain.agents import DecisionResult
from osa.domain.entities import Destination, Event, GroupPolicy
from osa.domain.features import Record
from osa.infrastructure.features.repository import MemoryFeatureRepository
from osa.infrastructure.memory_store import InMemoryStore


def make_message(identity, actor="10000", text="禁言那个要 jm 网站的人", role="admin", mention=False):
    return Event(identity, identity, "test", "message.created", Destination("group", "10001"),
                 actor, identity, text, [{"type": "at", "data": {"qq": "99999"}}] if mention else [],
                 time.time(), actor_role=role, mentioned=mention)


class AdminActionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = MemoryFeatureRepository()
        self.store = InMemoryStore()
        await self.store.initialize(["10001"])
        self.skills = BotSkillLearningService(self.repo, self.store, "99999", enabled=True)

    async def seed_flow(self, version=2):
        await self.repo.put(Record(self.skills._flow_id("10001"), "bot_skill_flow", None,
                                  "10001", "active", {"bot_id": "77777", "actor_id": "10002",
                                  "guard_version": version, "expires_at": time.time() + 600}))

    async def test_admin_management_request_is_not_held(self):
        await self.seed_flow()
        self.assertFalse(await self.skills.should_hold(make_message("admin")))
        ordinary = make_message("ordinary", actor="10002", text="聊天", role="member")
        self.assertTrue(await self.skills.should_hold(ordinary))
        self.assertTrue(await self.skills.should_hold(replace(make_message("member"), actor_role="member")))

    async def test_release_does_not_reactivate_the_flow(self):
        await self.seed_flow()
        await self.skills.release_flow("10001")
        self.assertIsNone(await self.skills._flow("10001"))

    async def test_old_unvalidated_lock_is_expired(self):
        await self.seed_flow(version=1)
        self.assertFalse(await self.skills.should_hold(make_message("chat", text="你好", role="member")))
        self.assertEqual((await self.repo.get(self.skills._flow_id("10001"))).status, "expired")

    async def test_unregistered_account_reply_cannot_lock_group(self):
        response = make_message("reply", actor="77777", text="1. 选项", role="member")
        await self.skills._activate_flow(response, {"actor_id": "10002"}, "observation")
        self.assertIsNone(await self.skills._flow("10001"))

    async def test_registered_bot_without_choices_cannot_lock_group(self):
        admin = make_message("register")
        await self.skills.register(admin, "77777")
        response = make_message("reply", actor="77777", text="收到图片", role="member")
        await self.skills._activate_flow(response, {"actor_id": "10002"}, "observation")
        self.assertIsNone(await self.skills._flow("10001"))

    async def test_registered_bot_menu_still_holds_ordinary_chat(self):
        await self.skills.register(make_message("register"), "77777")
        response = make_message("reply", actor="77777", text="1. 歌名\n2. 其他歌", role="member")
        await self.skills._activate_flow(response, {"actor_id": "10002"}, "observation")
        self.assertTrue(await self.skills.should_hold(make_message("chat", text="你好", role="member")))

    async def test_admin_request_stays_batch_anchor_after_new_chat(self):
        runtime = AgentRuntime(self.store, None, None, features=SimpleNamespace(bot_skills=self.skills))
        runtime._process = AsyncMock(return_value={"action": "TEST"})
        request = replace(make_message("request"), mentioned=False,
                          segments=[{"type": "at", "data": {"qq": "77777"}}])
        later = make_message("later", actor="10002", text="别的话题", role="member")
        for event in (request, later):
            await self.store.publish(event, True)
        result = await runtime.process_batch([request, later])
        self.assertEqual(result["action"], "TEST")
        self.assertEqual(runtime._process.await_args.args[0].id, request.id)
        batch = runtime._process.await_args.args[1]
        self.assertTrue(batch["direct"])
        self.assertIn(request.id, [item["id"] for item in batch["request_events"]])

    async def test_admin_request_reaches_thinking_as_direct_request(self):
        await self.seed_flow()
        features = SimpleNamespace(bot_skills=self.skills, initiative_eligible=AsyncMock(return_value=False),
            rank_memories=AsyncMock(return_value=[]), evidence=AsyncMock(return_value={}))
        resources = SimpleNamespace(evidence=AsyncMock(return_value={}))
        runtime = AgentRuntime(self.store, None, resources, features=features)
        runtime.v2 = SimpleNamespace(allowed=AsyncMock(return_value=True), route=AsyncMock(return_value=None),
            evidence=AsyncMock(return_value={"items": [], "refs": []}))
        runtime.v3 = SimpleNamespace(respond=AsyncMock(side_effect=RuntimeError("thinking reached")))
        request = replace(make_message("request"), mentioned=False,
                          segments=[{"type": "at", "data": {"qq": "77777"}}])
        await self.store.publish(request, True)
        with self.assertRaisesRegex(RuntimeError, "thinking reached"):
            await runtime._process(request)
        self.assertTrue(runtime.v3.respond.await_args.args[2]["direct_request"])
        self.assertIsNone(await self.skills._flow("10001"))

    async def test_admin_request_does_not_bypass_pause(self):
        await self.store.set_paused(True)
        features = SimpleNamespace(bot_skills=self.skills, initiative_eligible=AsyncMock(return_value=False))
        runtime = AgentRuntime(self.store, None, None, features=features)
        runtime.v2 = SimpleNamespace(allowed=AsyncMock(return_value=True))
        runtime.v3 = SimpleNamespace(respond=AsyncMock())
        await runtime._process(make_message("request"))
        runtime.v3.respond.assert_not_awaited()

    async def test_member_mention_cancels_flow_for_later_messages(self):
        await self.seed_flow()
        request = make_message("mention", actor="10003", text="在吗", role="member", mention=True)
        self.assertFalse(await self.skills.should_hold(request))
        flow = await self.repo.get(self.skills._flow_id("10001"))
        self.assertEqual(flow.status, "cancelled")
        self.assertEqual(flow.payload["release_reason"], "direct_mention")
        self.assertFalse(await self.skills.should_hold(make_message("later", text="闲聊", role="member")))

    async def test_mentioning_other_member_does_not_release_flow(self):
        await self.seed_flow()
        request = replace(make_message("other", text="你好", role="member"),
                          segments=[{"type": "at", "data": {"qq": "88888"}}])
        self.assertTrue(await self.skills.should_hold(request))
        self.assertIsNotNone(await self.skills._flow("10001"))

    async def test_mention_input_is_not_forwarded_to_external_bot(self):
        await self.seed_flow()
        self.skills.platform = SimpleNamespace(ready=True, call=AsyncMock())
        request = make_message("mention", actor="10002", text="2", role="member", mention=True)
        await self.skills.observe(request, GroupPolicy())
        self.skills.platform.call.assert_not_awaited()
        self.assertIsNone(await self.skills._flow("10001"))

    async def test_cancelled_probe_and_pending_reply_cannot_relock_group(self):
        await self.skills.register(make_message("register"), "77777")
        await self.seed_flow()
        self.skills._pending["group:10001"] = [{"event_id": "input", "message_id": "input",
            "actor_id": "10002", "text": "/help", "targets": ["77777"],
            "command": "/help", "time": time.time()}]
        await self.repo.put(Record("probe", "bot_skill_probe", "10002", "10001", "sent",
                                  {"bot_id": "77777", "command": "/help", "sent_at": time.time()}))
        await self.skills.observe(make_message("mention", text="在吗", role="member", mention=True), GroupPolicy())
        await self.skills.observe(make_message("bot-reply", actor="77777", text="1. 歌曲", role="member"), GroupPolicy())
        self.assertIsNone(await self.skills._flow("10001"))
        self.assertEqual((await self.repo.get("probe")).status, "cancelled")

    async def test_member_mention_does_not_bypass_pause(self):
        await self.seed_flow()
        await self.store.set_paused(True)
        features = SimpleNamespace(bot_skills=self.skills, initiative_eligible=AsyncMock(return_value=False))
        runtime = AgentRuntime(self.store, None, None, features=features)
        runtime.v2 = SimpleNamespace(allowed=AsyncMock(return_value=True))
        runtime.v3 = SimpleNamespace(respond=AsyncMock())
        await runtime._process(make_message("mention", text="你好", role="member", mention=True))
        runtime.v3.respond.assert_not_awaited()
