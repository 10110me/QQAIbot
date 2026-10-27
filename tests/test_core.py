"""Offline checks: domain/application contracts using the volatile demo adapter."""
import ast
import asyncio
import json
import time
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from osa.application.resources import ResourceService
from osa.application.runtime import AgentRuntime, OutboxDispatcher
from osa.config import Settings
from osa.domain.entities import (
    Action, Consent, DeliveryUnknown, Destination, Event, GroupFile, GroupPolicy,
    Memory, ModelResult, Notice, Scope,
)
from osa.domain.policy import PermissionPolicy, ToolPolicy
from osa.domain.social import SocialDecisionEngine
from osa.infrastructure.memory_store import InMemoryStore
from osa.infrastructure.models import FakeModel, ModelRouter
from osa.infrastructure.onebot import NapCatResources, OneBotConnection, normalize
from osa.application.message_context import render_event
from osa.main import create_app
from osa.sdk.contracts import PluginManifest, PluginRegistry


def payload(message_id="1", group="10001", actor="12345", text="你好", mention=True):
    return {"time": time.time(), "self_id": "99999", "post_type": "message", "message_type": "group",
            "group_id": group, "user_id": actor, "message_id": message_id,
            "message": ([{"type": "at", "data": {"qq": "99999"}}] if mention else []) + [{"type": "text", "data": {"text": text}}]}


def make_event(**kwargs) -> Event:
    event = normalize(payload(**kwargs), "99999", "test")
    assert event is not None
    return event


class FakeResources:
    async def notices(self, group_id):
        return [Notice("n1", f"{group_id} 群规则")]

    async def files(self, group_id):
        return [GroupFile("f1", "资料.pdf", 200)]


class FakePlatform:
    ready = True

    def __init__(self):
        self.sent = []

    async def send(self, destination, text, reply_to):
        self.sent.append((destination, text, reply_to))
        return str(9000 + len(self.sent))


class DomainTests(unittest.TestCase):
    def test_ids_are_scoped_and_duplicate_keys_stable(self):
        event = make_event()
        self.assertEqual(event.id, make_event().id)
        self.assertNotEqual(event.id, make_event(group="10002").id)

    def test_cq_string_at_reply_and_escape(self):
        data = payload()
        data["message"] = "[CQ:reply,id=4][CQ:at,qq=99999]你好&#91;世界&#93;"
        event = normalize(data, "99999", "test")
        self.assertTrue(event.mentioned)
        self.assertEqual(event.reply_to_id, "4")
        self.assertEqual(event.text, "你好[世界]")

    def test_bot_self_and_unknown_account_events_rejected(self):
        self.assertIsNone(normalize(payload(actor="99999"), "99999", "test"))
        self.assertIsNone(normalize(payload(), "88888", "test"))

    def test_segment_remote_urls_are_not_retained(self):
        data = payload()
        data["message"].append({"type": "image", "data": {"url": "http://127.0.0.1/secret", "file": "secret"}})
        self.assertNotIn("secret", json.dumps(normalize(data, "99999", "test").to_dict()))

    def test_social_rules_allow_silence(self):
        engine = SocialDecisionEngine()
        self.assertEqual(engine.decide(make_event(mention=False), GroupPolicy(), False).action, Action.IGNORE)
        self.assertEqual(engine.decide(make_event(), GroupPolicy(), False).action, Action.REPLY)
        self.assertEqual(engine.decide(make_event(mention=False), GroupPolicy(), True).action, Action.REPLY)
        self.assertEqual(engine.decide(make_event(), GroupPolicy(enabled=False), True).action, Action.IGNORE)

    def test_unaddressed_one_to_one_chat_is_not_novas_turn(self):
        engine = SocialDecisionEngine()
        for text in ('我需要你', '你再看看题目呢'):
            event = make_event(text=text, mention=False)
            self.assertEqual(engine.decide(event, GroupPolicy(), False).reason, 'ambiguous_second_person')
            self.assertEqual(engine.decide(event, GroupPolicy(), True).action, Action.REPLY)
        quoted = replace(make_event(text='这样写怎么样', mention=False), reply_to_id='another-person')
        self.assertEqual(engine.decide(quoted, GroupPolicy(), False).reason, 'reply_to_other_member')
        self.assertEqual(engine.decide(quoted, GroupPolicy(), True).action, Action.REPLY)
        open_topic = make_event(text='大家觉得这个提示词怎么样', mention=False)
        self.assertEqual(engine.decide(open_topic, GroupPolicy(), False).reason, 'humans_conversation')

    def test_at_other_member_is_preserved_and_never_direct(self):
        data = payload(mention=False, text="你是不是学 C++")
        data["message"] = [{"type": "at", "data": {"qq": "100000006"}},
                            {"type": "text", "data": {"text": " 你是不是学 C++"}}]
        event = normalize(data, "99999", "test")
        self.assertIsNotNone(event)
        self.assertFalse(event.mentioned)
        self.assertEqual(event.segments[0]["data"]["qq"], "100000006")
        decision = SocialDecisionEngine().decide(event, GroupPolicy(), False, called_by_name=True)
        self.assertEqual(decision.reason, "mention_other_user")
        self.assertIn("@100000006", render_event(event))

    def test_complex_segments_keep_safe_preview_and_ids(self):
        data = payload(mention=False, text="看这个")
        data["message"] = [{"type": "reply", "data": {"id": "88"}},
                            {"type": "forward", "data": {"id": "99"}},
                            {"type": "json", "data": {"data": '{\"prompt\":\"卡片内容\"}'}},
                            {"type": "text", "data": {"text": "看这个"}}]
        event = normalize(data, "99999", "test")
        self.assertEqual(event.reply_to_id, "88")
        rendered = render_event(event, include_id=True)
        self.assertIn("引用消息:88", rendered)
        self.assertIn("合并转发", rendered)
        self.assertIn("卡片内容", rendered)

    def test_retrieve_and_disclose_are_distinct(self):
        memory = Memory("m", "项目", Scope.PERSONAL_OWNER, "12345", "10001", "s")
        policy = PermissionPolicy()
        dest, group, consent = Destination("group", "10002"), GroupPolicy(cross_memory=True), Consent(True)
        self.assertTrue(policy.can_retrieve(memory, "12345", dest, consent, group, 0))
        self.assertFalse(policy.can_disclose(memory, "12345", dest, consent, group, 0))
        self.assertTrue(policy.can_disclose(memory, "12345", dest, Consent(True, frozenset({"10002"})), group, 0))
        self.assertFalse(policy.can_disclose(memory, "67890", dest, Consent(True, frozenset({"10002"})), group, 0))

    def test_private_and_group_scopes_do_not_cross_boundaries(self):
        policy = PermissionPolicy()
        for scope in [Scope.LONG_GROUP, Scope.DAILY_GROUP, Scope.GROUP_RESOURCE, Scope.PRIVATE_USER]:
            with self.subTest(scope=scope):
                memory = Memory("m", "秘密", scope, "12345", "10001", "s")
                self.assertFalse(policy.can_disclose(memory, "12345", Destination("group", "10002"), Consent(True, frozenset({"10002"})), GroupPolicy(cross_memory=True), 0))

    def test_inactive_and_expired_memory_are_rejected(self):
        for state in ["deleted", "candidate", "expired", "disputed"]:
            memory = Memory("m", "事实", Scope.LONG_GROUP, "12345", "10001", "s", state)
            self.assertFalse(PermissionPolicy().can_disclose(memory, "12345", Destination("group", "10001"), Consent(), GroupPolicy(), 0))
        memory = Memory("m", "事实", Scope.LONG_GROUP, "12345", "10001", "s", expires_at=1)
        self.assertFalse(PermissionPolicy().can_disclose(memory, "12345", Destination("group", "10001"), Consent(), GroupPolicy(), 2))

    def test_high_risk_tools_are_denied(self):
        for capability in ["python.execute", "shell.execute", "group.notices.write", "group.files.delete"]:
            with self.assertRaises(PermissionError):
                ToolPolicy().require(capability, Destination("group", "10001"), GroupPolicy(notice_read=True, file_metadata_read=True))

    def test_quiet_hours_cross_midnight(self):
        from datetime import datetime, UTC
        policy = GroupPolicy(quiet_start=22, quiet_end=7, timezone="UTC")
        gate = PermissionPolicy()
        self.assertFalse(gate.can_send(policy, datetime(2026, 1, 1, 23, tzinfo=UTC).timestamp(), False))
        self.assertTrue(gate.can_send(policy, datetime(2026, 1, 1, 12, tzinfo=UTC).timestamp(), False))

    def test_layer_dependency_direction(self):
        root = Path(__file__).resolve().parents[1] / "src" / "osa"
        for layer in ["domain", "application"]:
            for filename in (root / layer).rglob("*.py"):
                for node in ast.walk(ast.parse(filename.read_text())):
                    names = [node.module or ""] if isinstance(node, ast.ImportFrom) else ([a.name for a in node.names] if isinstance(node, ast.Import) else [])
                    self.assertFalse(any(name.startswith(("osa.infrastructure", "osa.interfaces", "sqlalchemy", "httpx", "fastapi")) for name in names), filename)

    def test_plugin_approval_is_not_manifest_permission(self):
        registry = PluginRegistry()
        registry.discover(PluginManifest(id="example.notice-reader", name="Notice reader", version="0.1.0", plugin_type="tool", permissions=[{"capability": "group.notices.read", "resource": "group_scoped"}]))
        dest = Destination("group", "10001")
        with self.assertRaises(PermissionError):
            registry.context("example.notice-reader", dest)
        registry.approve("example.notice-reader", frozenset({"group.notices.read"}))
        context = registry.context("example.notice-reader", dest)
        context.require("group.notices.read", dest)
        with self.assertRaises(PermissionError):
            context.require("group.notices.read", Destination("group", "10002"))
        with self.assertRaises(PermissionError):
            registry.approve("example.notice-reader", frozenset({"shell.execute"}))


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemoryStore()
        await self.store.initialize(["10001", "10002"])
        for group in ["10001", "10002"]:
            await self.store.update_policy(group, GroupPolicy(min_send_interval=0))
        self.resources = ResourceService(self.store, FakeResources())
        self.runtime = AgentRuntime(self.store, FakeModel(), self.resources)
        self.platform = FakePlatform()
        self.dispatcher = OutboxDispatcher(self.store, self.platform)

    async def process(self, event):
        self.assertTrue(await self.store.publish(event, True))
        for value in await self.store.claim_events(10):
            await self.runtime.process(value)

    async def test_duplicate_event_one_actual_send(self):
        event = make_event()
        await self.process(event)
        self.assertFalse(await self.store.publish(event, True))
        await self.dispatcher.dispatch_once()
        self.assertFalse(await self.dispatcher.dispatch_once())
        self.assertEqual(len(self.platform.sent), 1)

    async def test_human_conversation_has_no_outbox(self):
        await self.process(make_event(mention=False))
        self.assertEqual((await self.store.stats())["outbox"], {})

    async def test_removed_destination_never_sends_old_pending_reply(self):
        await self.process(make_event())
        dispatcher = OutboxDispatcher(self.store, self.platform, allowed_groups=frozenset())
        await dispatcher.dispatch_once()
        self.assertEqual(self.platform.sent, [])
        self.assertEqual((await self.store.stats())["outbox"], {"cancelled": 1})

    async def test_group_memory_dependent_bot_output_is_reused_in_same_group(self):
        source = make_event(message_id="s", mention=False)
        await self.store.publish(source, True)
        await self.store.add_memory(Memory("m", "记忆事实", Scope.LONG_GROUP, "12345", "10001", source.id))
        await self.process(make_event(message_id="2"))
        await self.dispatcher.dispatch_once()
        context = await self.store.context(make_event(message_id="3"), 40)
        self.assertTrue(any(message["role"] == "assistant" for message in context))

    async def test_exact_reply_to_bot_is_conversation_scoped(self):
        await self.process(make_event())
        await self.dispatcher.dispatch_once()
        event = replace(make_event(message_id="2", mention=False), reply_to_id="9001")
        self.assertTrue(await self.store.reply_is_bot(event))
        self.assertFalse(await self.store.reply_is_bot(replace(event, destination=Destination("group", "10002"))))

    async def test_three_cross_group_consent_gates_and_actor_boundary(self):
        source = make_event(message_id="source", mention=False)
        await self.store.publish(source, True)
        for memory in [Memory("a", "A群秘密", Scope.LONG_GROUP, "12345", "10001", source.id), Memory("p", "私聊秘密", Scope.PRIVATE_USER, "12345", None, source.id), Memory("owner", "共享项目", Scope.PERSONAL_OWNER, "12345", "10001", source.id)]:
            await self.store.add_memory(memory)
        dest = Destination("group", "10002")
        self.assertEqual(await self.store.visible_memories("12345", dest, "秘密"), [])
        await self.store.update_policy("10002", GroupPolicy(cross_memory=True, min_send_interval=0))
        await self.store.set_consent("12345", True)
        self.assertEqual(await self.store.visible_memories("12345", dest, "项目"), [])
        await self.store.set_consent("12345", True, "10002")
        self.assertEqual([m.id for m in await self.store.visible_memories("12345", dest, "项目")], ["owner"])
        self.assertEqual(await self.store.visible_memories("67890", dest, "项目"), [])

    async def test_revocation_after_generation_blocks_delivery_and_replay(self):
        source = make_event(message_id="s", mention=False)
        await self.store.publish(source, True)
        await self.store.add_memory(Memory("m", "共享项目", Scope.PERSONAL_OWNER, "12345", "10001", source.id))
        await self.store.set_consent("12345", True, "10002")
        await self.store.update_policy("10002", GroupPolicy(cross_memory=True, min_send_interval=0))
        event = make_event(message_id="2", group="10002")
        await self.process(event)
        snapshot = self.store.decisions[event.id]["snapshot"]
        await self.store.set_consent("12345", False)
        await self.dispatcher.dispatch_once()
        self.assertEqual(self.platform.sent, [])
        with self.assertRaises(PermissionError):
            await self.runtime.replay(snapshot)

    async def test_ambiguous_external_send_is_never_retried(self):
        class UnknownPlatform(FakePlatform):
            async def send(self, destination, text, reply_to):
                await super().send(destination, text, reply_to)
                raise DeliveryUnknown()
        await self.process(make_event())
        peer = UnknownPlatform()
        dispatcher = OutboxDispatcher(self.store, peer)
        await dispatcher.dispatch_once()
        self.assertFalse(await dispatcher.dispatch_once())
        self.assertEqual(len(peer.sent), 1)
        self.assertEqual((await self.store.stats())["outbox"], {"unknown": 1})

    async def test_recall_erases_source_and_derived_memory(self):
        event = make_event(text="/记忆 记住 我的项目是编译器")
        await self.process(event)
        self.assertEqual(len(await self.store.own_memories("12345")), 1)
        await self.store.recall(replace(event, event_type="message.recalled"))
        self.assertEqual(await self.store.own_memories("12345"), [])
        self.assertEqual(self.store.events[event.id]["event"].text, "")
        await self.dispatcher.dispatch_once()
        self.assertEqual(self.platform.sent, [])

    async def test_out_of_order_recall_blocks_late_source(self):
        event = make_event()
        recall = replace(event, id="recalled", dedupe_key="recalled", event_type="message.recalled")
        await self.store.publish(recall, True)
        await self.process(event)
        self.assertEqual((await self.store.stats())["outbox"], {})

    async def test_replay_never_creates_outbox_or_sends(self):
        event = make_event()
        await self.process(event)
        before = await self.store.stats()
        result = await self.runtime.replay(self.store.decisions[event.id]["snapshot"])
        self.assertFalse(result["sent"])
        self.assertEqual(await self.store.stats(), before)
        self.assertEqual(self.platform.sent, [])

    async def test_emergency_stop_and_expiry_are_checked_before_send(self):
        event = make_event()
        await self.process(event)
        await self.store.set_paused(True)
        await self.dispatcher.dispatch_once()
        self.assertEqual(self.platform.sent, [])
        await self.store.set_paused(False)
        event = make_event(message_id="2")
        await self.process(event)
        self.store.outboxes[event.id]["expires_at"] = 0
        await self.dispatcher.dispatch_once()
        self.assertEqual(self.platform.sent, [])

    async def test_corrected_memory_invalidates_claimed_candidate(self):
        source = make_event(message_id="s", mention=False)
        await self.store.publish(source, True)
        await self.store.add_memory(Memory("m", "旧项目", Scope.LONG_GROUP, "12345", "10001", source.id))
        await self.process(make_event(message_id="2"))
        await self.store.correct_memory("m", "12345", "新项目", source.id)
        await self.dispatcher.dispatch_once()
        self.assertEqual(self.platform.sent, [])
        self.assertEqual(self.store.memories["m"].version, 2)

    async def test_deletion_checks_owner_and_invalidates_snapshot(self):
        source = make_event(message_id="s", mention=False)
        await self.store.publish(source, True)
        await self.store.add_memory(Memory("m", "个人事实", Scope.LONG_GROUP, "12345", "10001", source.id))
        event = make_event(message_id="2")
        await self.process(event)
        self.assertFalse(await self.store.delete_memory("m", "67890"))
        self.assertTrue(await self.store.delete_memory("m", "12345"))
        self.assertEqual(self.store.decisions[event.id]["snapshot"], {})
        self.assertEqual(self.store.memories["m"].content, "")

    async def test_collection_disabled_does_not_retain_human_text(self):
        await self.store.update_policy("10001", GroupPolicy(collect_messages=False))
        event = make_event(text="不保存这条原文")
        await self.store.publish(event, False)
        await self.runtime.process((await self.store.claim_events(10))[0])
        self.assertEqual(self.store.events[event.id]["event"].text, "")
        self.assertNotIn("messages", self.store.decisions[event.id]["snapshot"])
        self.assertEqual(self.store.decisions[event.id]["snapshot"]["event"]["text"], "")

    async def test_resource_reads_require_policy_and_keep_group_scope(self):
        with self.assertRaises(PermissionError):
            await self.resources.sync("10001", "notices")
        await self.store.update_policy("10001", GroupPolicy(notice_read=True))
        await self.resources.sync("10001", "notices")
        self.assertEqual(len(await self.store.resources("10001", "notices")), 1)
        self.assertEqual(await self.store.resources("10002", "notices"), [])

    async def test_notice_version_changes_and_missing_requires_two_snapshots(self):
        await self.store.save_resources("10001", "notices", [{"id": "n", "body": "旧公告"}])
        await self.store.save_resources("10001", "notices", [{"id": "n", "body": "新公告"}])
        self.assertEqual((await self.store.resources("10001", "notices"))[0]["version"], 2)
        await self.store.save_resources("10001", "notices", [])
        self.assertEqual(len(await self.store.resources("10001", "notices")), 1)
        await self.store.save_resources("10001", "notices", [])
        self.assertEqual(await self.store.resources("10001", "notices"), [])

    async def test_daily_card_is_idempotent_and_recall_invalidates(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        event = make_event(mention=False)
        await self.process(event)
        day = datetime.fromtimestamp(event.occurred_at, ZoneInfo("Asia/Shanghai")).date().isoformat()
        first = await self.store.daily("10001", day, "Asia/Shanghai")
        second = await self.store.daily("10001", day, "Asia/Shanghai")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["source_ids"], [event.id])
        await self.store.recall(replace(event, event_type="message.recalled"))
        self.assertEqual((await self.store.daily("10001", day, "Asia/Shanghai"))["source_ids"], [])

    async def test_model_router_budget_is_hard_limit(self):
        router = ModelRouter(FakeModel(), daily_limit=1)
        await router.generate([{"role": "user", "content": "a"}])
        from osa.domain.entities import UpstreamUnavailable
        with self.assertRaises(UpstreamUnavailable):
            await router.generate([{"role": "user", "content": "b"}])

    async def test_model_router_zero_budget_is_unlimited(self):
        router = ModelRouter(FakeModel(), daily_limit=0)
        first = await router.generate([{"role": "user", "content": "a"}])
        second = await router.generate([{"role": "user", "content": "b"}])
        self.assertIn("收到", first.text)
        self.assertIn("收到", second.text)

    async def test_model_router_failure_circuit_does_not_block_other_purposes(self):
        from osa.domain.entities import UpstreamUnavailable

        class FailingVision:
            async def generate(self, messages, purpose):
                if purpose == "vision":
                    raise ValueError("invalid image")
                return await FakeModel().generate(messages, purpose)

        router = ModelRouter(FailingVision())
        for _ in range(3):
            with self.assertRaises(UpstreamUnavailable):
                await router.generate([], "vision")
        response = await router.generate([], "thinking")
        self.assertEqual(response.provider, "fake")

    async def test_configured_provider_uses_its_own_failover_without_router_circuit(self):
        from osa.domain.entities import UpstreamUnavailable

        class FailingConfigured:
            handles_retries = True

            async def generate(self, messages, purpose):
                raise UpstreamUnavailable("all models unavailable")

        router = ModelRouter(FailingConfigured())
        for _ in range(4):
            with self.assertRaises(UpstreamUnavailable):
                await router.generate([], "thinking")
        self.assertEqual(router._open_until, {})

    async def test_read_only_file_traversal_limits(self):
        class Client:
            async def call(self, action, params):
                return {"files": [], "folders": [{"folder_id": str(len(str(params.get('folder_id', '')))) + str(params.get('folder_id', ''))}]}
        from osa.domain.entities import UpstreamUnavailable
        with self.assertRaises(UpstreamUnavailable):
            await NapCatResources(Client(), max_depth=1).files("10001")


def demo_settings():
    return Settings(_env_file=None, admin_token="admin-test-token-12345", napcat_token="napcat-test-token-67890", database_url="memory://", demo_mode=True, allowed_groups=["10001"], bot_id="99999", decision_mode='hybrid')


class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_api_auth_webui_and_policy_validation(self):
        app = create_app(demo_settings())
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.get("/")).status_code, 200)
            self.assertEqual((await client.get("/api/v1/groups")).status_code, 401)
            headers = {"Authorization": "Bearer admin-test-token-12345"}
            self.assertEqual((await client.get("/api/v1/groups", headers=headers)).status_code, 200)
            bad = await client.patch("/api/v1/groups/10001/settings", headers=headers, json={"min_send_interval": -1, "unexpected": "secret"})
            self.assertEqual(bad.status_code, 422)
            self.assertNotIn("secret", bad.text)
            self.assertEqual((await client.get("/health/ready")).status_code, 200)

    async def websocket_task(self, app, token):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "path": "/onebot/v11/ws", "raw_path": b"/onebot/v11/ws", "query_string": b"",
                 "scheme": "ws", "root_path": "", "client": ("test", 1), "server": ("test", 80),
                 "subprotocols": [], "headers": [(b"authorization", ("Bearer " + token).encode()), (b"x-self-id", b"99999")]}
        task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
        return task, incoming, outgoing

    async def test_reverse_websocket_asgi_duplicate_and_echo_ack(self):
        app = create_app(demo_settings())
        async with app.router.lifespan_context(app):
            task, incoming, outgoing = await self.websocket_task(app, "napcat-test-token-67890")
            try:
                accepted = await asyncio.wait_for(outgoing.get(), 2)
                self.assertEqual(accepted["type"], "websocket.accept")
                event = payload()
                await incoming.put({"type": "websocket.receive", "text": json.dumps(event)})
                await incoming.put({"type": "websocket.receive", "text": json.dumps(event)})
                frame = await asyncio.wait_for(outgoing.get(), 3)
                action = json.loads(frame["text"])
                self.assertEqual(action["action"], "send_group_msg")
                await incoming.put({"type": "websocket.receive", "text": json.dumps({"echo": action["echo"], "status": "ok", "retcode": 0, "data": {"message_id": 9001}})})
                for _ in range(40):
                    values = await app.state.store.messages(None, None, None, None, 10)
                    if values and values[0]["status"] == "sent":
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(values), 1)
                self.assertEqual(values[0]["status"], "sent")
            finally:
                await incoming.put({"type": "websocket.disconnect", "code": 1000})
                await asyncio.wait_for(task, 2)

    async def test_wrong_websocket_token_rejected(self):
        app = create_app(demo_settings())
        async with app.router.lifespan_context(app):
            task, incoming, outgoing = await self.websocket_task(app, "wrong")
            frame = await asyncio.wait_for(outgoing.get(), 2)
            self.assertEqual(frame["type"], "websocket.close")
            self.assertEqual(frame["code"], 1008)
            await asyncio.wait_for(task, 2)


if __name__ == "__main__":
    unittest.main()
