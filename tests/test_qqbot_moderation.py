import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from osa.application.qqbot_moderation import call_moderation, moderation_arguments
from osa.application.v3.tools import AgentTools
from osa.domain.entities import DeliveryUnknown, Destination, Event, GroupPolicy, UpstreamUnavailable
from osa.infrastructure.memory_store import InMemoryStore


class ModerationArgumentTests(unittest.TestCase):
    def test_structured_batch_deduplicates_and_preserves_seconds(self):
        self.assertEqual(moderation_arguments({"action": "mute", "target_ids": ["20000", 20000, "20001"],
                                             "duration_seconds": 75}),
                         {"action": "mute", "target_ids": ["20000", "20001"], "duration_seconds": 75})

    def test_legacy_commands_remain_minutes_and_unmute_is_zero(self):
        result = moderation_arguments({"message": "/禁言 @20000 @20001 5", "mentions": ["20000", "20001"]})
        self.assertEqual(result["duration_seconds"], 300)
        self.assertEqual(moderation_arguments({"message": "/unmute", "mentions": ["20000"]})["duration_seconds"], 0)

    def test_invalid_targets_and_duration_are_not_silently_truncated(self):
        for arguments in ({"target_ids": ["all"]}, {"target_ids": [True]}, {"target_ids": ["0"]},
                          {"target_ids": ["20000"] * 51}, {"target_ids": ["20000"], "duration_seconds": True},
                          {"target_ids": ["20000"], "duration_seconds": 2592001},
                          {"target_ids": ["20000"], "duration_seconds": 0},
                          {"target_ids": ["20000"], "message": "/禁言 五分钟"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                moderation_arguments({"action": "mute", **arguments})


class ModerationExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.destination = Destination("group", "10001")
        self.event = Event("source", "source", "test", "message.created", self.destination,
                           "10000", "source", "把这几个人禁言五分钟", [], time.time())
        self.store = InMemoryStore()
        await self.store.initialize(["10001"])
        await self.store.publish(self.event, True)
        self.members = [{"user_id": "99999", "role": "admin"}, {"user_id": "10000", "role": "member"},
                        {"user_id": "20000", "role": "member"}, {"user_id": "20001", "role": "member"},
                        {"user_id": "20002", "role": "admin"}, {"user_id": "20003", "role": "owner"}]
        self.failures = {}
        self.platform = SimpleNamespace(ready=True, call=AsyncMock(side_effect=self.platform_call))
        self.client = SimpleNamespace(list_tools=AsyncMock(return_value=[{"tool": "moderation.mute"}]),
                                      call=AsyncMock(side_effect=self.authorize))
        self.features = SimpleNamespace(platform=self.platform, qqbot_tools=self.client)
        self.context = {"source_id": "source", "actor_id": "10000", "group_id": "10001", "session_id": "session",
                        "tool_call_id": "batch", "deadline": time.time() + 60, "source_event": self.event.to_dict()}

    async def platform_call(self, action, params):
        if action == "get_login_info":
            return {"user_id": "99999"}
        if action == "get_group_member_list":
            return self.members
        if str(params["user_id"]) in self.failures:
            raise self.failures[str(params["user_id"])]
        return {}

    async def authorize(self, tool, arguments, **scope):
        return {"ok": True, "actions": [{"type": "set_group_ban", "group_id": scope["group_id"], "user_id": target,
                                          "duration": arguments["duration_seconds"]} for target in arguments["target_ids"]]}

    async def execute(self, targets=None, **arguments):
        return await call_moderation(self.features, self.store, self.destination, "10000", self.context,
            {"action": "mute", "target_ids": targets or ["20000", "20001"], "duration_seconds": 300, **arguments})

    def bans(self):
        return [call.args[1] for call in self.platform.call.await_args_list if call.args[0] == "set_group_ban"]

    async def test_batch_fetches_members_once_and_bans_every_target(self):
        result = await self.execute()
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["applied"], 2)
        self.assertEqual([ban["user_id"] for ban in self.bans()], [20000, 20001])
        self.assertEqual(sum(call.args[0] == "get_group_member_list" for call in self.platform.call.await_args_list), 1)
        self.assertEqual(self.platform.call.await_args_list[1].args[1], {"group_id": 10001, "no_cache": True})
        self.client.call.assert_awaited_once()

    async def test_missing_protected_and_self_targets_are_skipped_before_qqbot(self):
        result = await self.execute(["20000", "29999", "20002", "20003", "99999"])
        self.assertTrue(result["partial"])
        self.assertEqual([item["status"] for item in result["results"]], ["applied"] + ["skipped"] * 4)
        self.assertEqual(self.client.call.await_args.args[1]["target_ids"], ["20000"])
        self.assertEqual(len(self.bans()), 1)

    async def test_fifty_targets_are_all_applied_without_generic_action_limit(self):
        targets = [str(30000 + index) for index in range(50)]
        self.members.extend({"user_id": target, "role": "member"} for target in targets)
        result = await self.execute(targets)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.bans()), 50)

    async def test_all_missing_targets_do_not_call_qqbot(self):
        result = await self.execute(["28888", "29999"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "BATCH_NOT_APPLIED")
        self.client.call.assert_not_awaited()

    async def test_unknown_member_role_is_not_assumed_to_be_ordinary_member(self):
        self.members[2].pop("role")
        result = await self.execute(["20000"])
        self.assertEqual(result["results"][0]["error"], "UNKNOWN_ROLE")
        self.assertEqual(self.bans(), [])

    async def test_no_bot_admin_permission_never_reaches_qqbot_or_ban(self):
        self.members[0]["role"] = "member"
        result = await self.execute()
        self.assertEqual(result["error"], "BOT_PERMISSION_DENIED")
        self.client.call.assert_not_awaited()
        self.assertEqual(self.bans(), [])

    async def test_group_owner_can_operate_admin_but_not_owner(self):
        self.members[0]["role"] = "owner"
        result = await self.execute(["20002", "20003"])
        self.assertEqual(result["summary"]["applied"], 1)
        self.assertEqual(self.bans()[0]["user_id"], 20002)

    async def test_lookup_failure_fails_closed(self):
        self.platform.call.side_effect = UpstreamUnavailable("offline")
        result = await self.execute()
        self.assertEqual(result["error"], "MEMBER_CHECK_FAILED")
        self.client.call.assert_not_awaited()

    async def test_invalid_lookup_data_fails_closed(self):
        self.members = {}
        self.assertEqual((await self.execute())["error"], "MEMBER_CHECK_FAILED")
        self.assertEqual(self.bans(), [])

    async def test_requester_outside_group_cannot_operate(self):
        self.members = [member for member in self.members if member["user_id"] != "10000"]
        self.assertEqual((await self.execute())["error"], "REQUESTER_NOT_IN_GROUP")
        self.client.call.assert_not_awaited()

    async def test_platform_rejection_does_not_abort_other_targets_or_claim_success(self):
        self.failures["20000"] = UpstreamUnavailable("rejected")
        result = await self.execute()
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual([item["status"] for item in result["results"]], ["failed", "applied"])

    async def test_timeout_is_unknown_not_success_and_no_automatic_retry(self):
        self.failures["20000"] = DeliveryUnknown("timeout")
        result = await self.execute(["20000"])
        self.assertEqual(result["results"][0]["status"], "unknown")
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.bans()), 1)

    async def test_qqbot_denied_operator_never_applies_actions(self):
        self.client.call.side_effect = None
        self.client.call.return_value = {"ok": True, "actions": [], "results": [
            {"user_id": "20000", "error": "PERMISSION_DENIED", "detail": "仅管理员可使用此命令"}]}
        result = await self.execute(["20000"])
        self.assertEqual(result["results"][0]["error"], "PERMISSION_DENIED")
        self.assertEqual(self.bans(), [])

    async def test_wrong_group_target_or_duration_action_is_not_executed(self):
        self.client.call.side_effect = None
        for patch in ({"group_id": "10002"}, {"user_id": "20003"}, {"duration": 600}, {"duration": True}):
            with self.subTest(patch=patch):
                self.client.call.return_value = {"actions": [{"type": "set_group_ban", "group_id": "10001",
                    "user_id": "20000", "duration": 300, **patch}]}
                result = await self.execute(["20000"])
                self.assertFalse(result["ok"])
        self.assertEqual(self.bans(), [])

    async def test_revoked_policy_between_check_and_execution_prevents_ban(self):
        async def revoke(*args, **kwargs):
            await self.store.update_policy("10001", GroupPolicy(enabled=False))
            return await self.authorize(*args, **kwargs)
        self.client.call.side_effect = revoke
        result = await self.execute()
        self.assertEqual(result["summary"]["applied"], 0)
        self.assertEqual(self.bans(), [])

    async def test_offline_platform_does_not_report_applied(self):
        self.platform.ready = False
        self.assertEqual((await self.execute())["error"], "BOT_OFFLINE")
        self.platform.call.assert_not_awaited()

    async def test_unmute_applies_zero_to_all_eligible_targets(self):
        result = await self.execute(action="unmute")
        self.assertTrue(result["ok"])
        self.assertEqual([ban["duration"] for ban in self.bans()], [0, 0])

    async def test_agent_tools_routes_batch_and_receipt_replay_does_not_repeat(self):
        receipts = {}
        async def registry(name, *args):
            if name == "thinking":
                return {"owner_id": "10000", "group_id": "10001"}
            if name == "receipt":
                return "receipt", receipts.get("receipt")
            if name == "finish_receipt":
                receipts[args[0]] = args[1]
        gateway = AgentTools(SimpleNamespace(call=registry), self.store, features=self.features)
        arguments = {"tool": "moderation.mute", "arguments": {"action": "mute", "target_ids": ["20000", "20001"],
                                                                 "duration_seconds": 300}}
        result = await gateway.execute("qqbot.tools.call", arguments, self.context)
        self.assertEqual(result["summary"]["applied"], 2)
        self.assertEqual(await gateway.execute("qqbot.tools.call", arguments, self.context), result)
        self.assertEqual(len(self.bans()), 2)

    async def test_direct_moderation_tool_does_not_require_catalog_lookup(self):
        receipts = {}
        async def registry(name, *args):
            if name == "thinking":
                return {"owner_id": "10000", "group_id": "10001"}
            if name == "receipt":
                return "receipt", receipts.get("receipt")
            if name == "finish_receipt":
                receipts[args[0]] = args[1]
        gateway = AgentTools(SimpleNamespace(call=registry), self.store, features=self.features)
        result = await gateway.execute("moderation.mute", {"action": "mute", "target_ids": ["20000"],
            "duration_seconds": 300}, self.context)
        self.assertEqual(result["summary"]["applied"], 1)
        self.client.list_tools.assert_not_awaited()
        self.assertEqual(len(self.bans()), 1)

    async def test_direct_moderation_requires_request_selection_in_multi_user_batch(self):
        gateway = AgentTools(SimpleNamespace(call=AsyncMock()), self.store, features=self.features)
        context = {**self.context, "request_events": [self.event.to_dict(),
            {**self.event.to_dict(), "id": "other", "actor_id": "20000"}]}
        result = await gateway.execute("moderation.mute", {"action": "mute", "target_ids": ["20001"],
            "duration_seconds": 300}, context)
        self.assertEqual(result["error"], "REQUEST_SELECTION_REQUIRED")
        self.assertEqual(self.bans(), [])
