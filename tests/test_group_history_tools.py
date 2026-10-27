import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from osa.application.v3.tools import AgentTools, history_window
from osa.domain.entities import Destination, Event, GroupPolicy, Memory, ModelResult, Scope
from osa.infrastructure.memory_store import InMemoryStore
from osa.infrastructure.store import SQLStore


GROUP = Destination("group", "10001")
OTHER_GROUP = Destination("group", "10002")
MORNING = datetime(2026, 10, 24, 9, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def message(identifier, text, occurred_at, destination=GROUP, actor="20000"):
    return Event(identifier, identifier, "test", "message.created", destination, actor,
                 identifier, text, [{"type": "text", "data": {"text": text}}], occurred_at,
                 actor_name="群友", actor_role="member")


class HistoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_resolves_quotes_without_reusing_dependent_replies(self):
        directory = tempfile.TemporaryDirectory()
        stores = [InMemoryStore(), SQLStore(f"sqlite+aiosqlite:///{Path(directory.name) / 'context.db'}")]
        try:
            for store in stores:
                with self.subTest(store=type(store).__name__):
                    await store.initialize([GROUP.id, OTHER_GROUP.id], isinstance(store, SQLStore))
                    source = message("source-context", "原始问题", MORNING)
                    followup = replace(message("followup-context", "这个呢", MORNING + 120),
                                       reply_to_id=source.message_id)
                    await store.publish(source, True)
                    await store.publish(followup, True)
                    quoted = await store.quoted_context(followup)
                    self.assertIn("原始问题", quoted["content"])
                    self.assertEqual(quoted["source_id"], source.id)
                    await store.record_decision(source, "REPLY", "test",
                        {"ttl": 180, "source_ids": [source.id]},
                        ModelResult("Nova 曾回答过", "test", "model"))
                    outbox = await store.claim_outbox()
                    await store.finish_outbox(outbox["id"], "sent", "90001")
                    context = await store.context(replace(followup, occurred_at=time.time() + 1), 10)
                    self.assertIn("Nova 曾回答过", [row["content"] for row in context])
                    bot_quote = replace(followup, reply_to_id="90001")
                    self.assertIn("Nova 曾回答过", (await store.quoted_context(bot_quote))["content"])
                    foreign = replace(bot_quote, destination=OTHER_GROUP)
                    self.assertIsNone(await store.quoted_context(foreign))
                    dependent = message("dependent-context", "个人记忆", MORNING + 30)
                    await store.publish(dependent, True)
                    await store.record_decision(dependent, "REPLY", "test",
                        {"ttl": 180, "source_ids": [dependent.id], "memory_ids": ["used-memory"]},
                        ModelResult("敏感旧回复", "test", "model"))
                    restricted = await store.claim_outbox()
                    await store.finish_outbox(restricted["id"], "sent", "90002")
                    self.assertIsNone(await store.quoted_context(replace(followup, reply_to_id="90002")))
                    context = await store.context(replace(followup, occurred_at=time.time() + 1), 10)
                    self.assertNotIn("敏感旧回复", [row["content"] for row in context])
                    group_memory = Memory("group-memory", "本群公开事实", Scope.LONG_GROUP,
                                          source.actor_id, GROUP.id, source.id)
                    await store.add_memory(group_memory)
                    public_reply = message("public-reply", "新的本群话题", MORNING + 60)
                    await store.publish(public_reply, True)
                    await store.record_decision(public_reply, "REPLY", "test",
                        {"ttl": 180, "source_ids": [public_reply.id], "memory_ids": [group_memory.id]},
                        ModelResult("Nova 刚回应了本群话题", "test", "model"))
                    public_outbox = await store.claim_outbox()
                    await store.finish_outbox(public_outbox["id"], "sent", "90003")
                    context = await store.context(replace(followup, occurred_at=time.time() + 1), 10)
                    self.assertIn("Nova 刚回应了本群话题", [row["content"] for row in context])
                    self.assertIn("Nova 刚回应了本群话题", (await store.quoted_context(
                        replace(followup, reply_to_id="90003")))["content"])
                    self.assertIsNone(await store.quoted_context(
                        replace(followup, destination=OTHER_GROUP, reply_to_id="90003")))
                    await store.delete_memory(group_memory.id)
                    self.assertIsNone(await store.quoted_context(replace(followup, reply_to_id="90003")))
                    context = await store.context(replace(followup, occurred_at=time.time() + 1), 10)
                    self.assertNotIn("Nova 刚回应了本群话题", [row["content"] for row in context])
                    private_memory = Memory("private-memory", "不应进入群上下文", Scope.PRIVATE_USER,
                                            source.actor_id, None, source.id)
                    await store.add_memory(private_memory)
                    private_reply = message("private-reply", "私聊记忆问题", MORNING + 90)
                    await store.publish(private_reply, True)
                    await store.record_decision(private_reply, "REPLY", "test",
                        {"ttl": 180, "source_ids": [private_reply.id], "memory_ids": [private_memory.id]},
                        ModelResult("Nova 使用了私聊记忆", "test", "model"))
                    private_outbox = await store.claim_outbox()
                    await store.finish_outbox(private_outbox["id"], "sent", "90004")
                    self.assertIsNone(await store.quoted_context(replace(followup, reply_to_id="90004")))
                    context = await store.context(replace(followup, occurred_at=time.time() + 1), 10)
                    self.assertNotIn("Nova 使用了私聊记忆", [row["content"] for row in context])
        finally:
            for store in stores:
                await store.close()
            directory.cleanup()

    async def test_sql_and_memory_search_only_retained_current_group_messages(self):
        directory = tempfile.TemporaryDirectory()
        stores = [InMemoryStore(), SQLStore(f"sqlite+aiosqlite:///{Path(directory.name) / 'history.db'}")]
        try:
            for store in stores:
                with self.subTest(store=type(store).__name__):
                    await store.initialize([GROUP.id, OTHER_GROUP.id], isinstance(store, SQLStore))
                    older = message("older", "上午说了粗口", MORNING)
                    newer = message("newer", "上午正常聊天", MORNING + 60)
                    recalled = message("recalled", "撤回的粗口", MORNING + 120)
                    hidden = message("hidden", "没有授权留存", MORNING + 180)
                    other = message("other", "别的群的粗口", MORNING + 240, OTHER_GROUP)
                    for event, collect in [(older, True), (newer, True), (recalled, True),
                                           (hidden, False), (other, True)]:
                        await store.publish(event, collect)
                    await store.recall(replace(recalled, event_type="message.recalled"))
                    first, cursor = await store.search_events(GROUP, MORNING - 1, MORNING + 300, "", None, 1)
                    self.assertEqual([event.id for event in first], ["newer"])
                    second, next_cursor = await store.search_events(GROUP, MORNING - 1, MORNING + 300, "",
                                                                     (float(cursor.split("|")[0]), cursor.split("|")[1]), 1)
                    self.assertEqual([event.id for event in second], ["older"])
                    self.assertIsNone(next_cursor)
                    matches, _ = await store.search_events(GROUP, MORNING - 1, MORNING + 300, "粗口", None, 30)
                    self.assertEqual([event.id for event in matches], ["older"])
        finally:
            for store in stores:
                await store.close()
            directory.cleanup()

    async def test_tool_search_and_live_roles_are_scoped(self):
        store = InMemoryStore()
        await store.initialize([GROUP.id])
        source = message("source", "禁言上午说粗口的人", MORNING + 7200, actor="10000")
        evidence = message("evidence", "你这个粗口", MORNING, actor="20000")
        await store.publish(source, True)
        await store.publish(evidence, True)
        receipts = {}

        async def registry(name, *args):
            if name == "thinking":
                return {"owner_id": "10000", "group_id": GROUP.id}
            if name == "receipt":
                return args[1], receipts.get(args[1])
            if name == "finish_receipt":
                receipts[args[0]] = args[1]

        async def platform_call(action, parameters):
            if action == "get_login_info":
                return {"user_id": "99999"}
            if action == "get_group_member_list":
                self.assertEqual(parameters["group_id"], int(GROUP.id))
                return [{"user_id": "99999", "role": "admin", "nickname": "Nova"},
                        {"user_id": "10000", "role": "owner", "nickname": "群主"},
                        {"user_id": "20000", "role": "member", "nickname": "说话的人", "card": "大肥鱼"},
                        {"user_id": "30000", "role": "member", "nickname": "小肥鱼"},
                        {"user_id": "40000", "role": "member", "nickname": "大肥鱼"}]
            raise AssertionError(action)

        platform = SimpleNamespace(ready=True, call=AsyncMock(side_effect=platform_call))
        tools = AgentTools(SimpleNamespace(call=registry), store,
                           features=SimpleNamespace(platform=platform))
        context = {"source_id": source.id, "source_event": source.to_dict(), "actor_id": source.actor_id,
                   "group_id": GROUP.id, "session_id": "session", "tool_call_id": "search",
                   "deadline": time.time() + 60}
        history = await tools.execute("message.search", {"period": "today_morning", "query": ""}, context)
        self.assertEqual([row["actor_id"] for row in history["messages"]], ["20000"])
        self.assertIn("发言时群成员", history["messages"][0]["content"])
        self.assertEqual(history["_source_ids"], ["evidence"])
        context["tool_call_id"] = "roles"
        roles = await tools.execute("group.members", {"query": "20000"}, context)
        self.assertEqual((roles["bot_role"], roles["requester_role"]), ("admin", "owner"))
        self.assertEqual(roles["members"][0]["role"], "member")
        self.assertEqual(roles["members"][0]["match_type"], "qq_exact")
        context["tool_call_id"] = "name"
        names = await tools.execute("group.members", {"query": "大肥鱼"}, context)
        self.assertEqual(names["exact_matches"], 2)
        self.assertEqual({row["user_id"] for row in names["members"]}, {"20000", "40000"})
        self.assertTrue(all(row["match_type"] == "name_exact" for row in names["members"]))
        context["tool_call_id"] = "partial"
        partial = await tools.execute("group.members", {"query": "肥鱼"}, context)
        self.assertEqual(partial["exact_matches"], 0)
        self.assertEqual(partial["matched"], 3)
        context["tool_call_id"] = "missing"
        missing = await tools.execute("group.members", {"query": "2000"}, context)
        self.assertEqual(missing["matched"], 0)
        await store.update_policy(GROUP.id, GroupPolicy(collect_messages=False))
        context["tool_call_id"] = "disabled"
        denied = await tools.execute("message.search", {"period": "today_morning"}, context)
        self.assertEqual(denied["error"], "PermissionError")

    def test_morning_window_uses_group_timezone(self):
        after, before = history_window({"period": "today_morning"}, "Asia/Shanghai", MORNING + 7200)
        self.assertEqual(after, datetime(2026, 10, 24, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        self.assertEqual(before, MORNING + 7200)
