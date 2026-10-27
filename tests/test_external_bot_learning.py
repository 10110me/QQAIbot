import asyncio
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from osa.application.chat_stream import BotSkillLearningService
from osa.application.v3.service import V3Service
from osa.domain.agents import ThinkResult
from osa.domain.entities import GroupPolicy
from osa.domain.features import Record
from osa.infrastructure.features.repository import MemoryFeatureRepository
from test_core import make_event


class FakePlatform:
    ready = True

    def __init__(self):
        self.calls = []

    async def call(self, action, params):
        self.calls.append((action, params))
        return {"message_id": "probe-message"}


class ExternalBotLearningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from osa.infrastructure.memory_store import InMemoryStore

        self.repo = MemoryFeatureRepository()
        self.store = InMemoryStore()
        await self.store.initialize(["10001"])
        self.platform = FakePlatform()
        self.service = BotSkillLearningService(
            self.repo, self.store, "99999", enabled=True,
            admin_ids={"10000"}, platform=self.platform)

    async def test_admin_phrase_registers_mentioned_bot_but_regular_member_cannot(self):
        admin = replace(make_event(actor="10000", text="这个账号是个机器人"),
                        segments=[{"type": "at", "data": {"qq": "77777"}},
                                  {"type": "text", "data": {"text": "这个账号是个机器人"}}])
        await self.service.observe(admin, GroupPolicy())
        profile = await self.repo.get(self.service._profile_id("10001", "77777"))
        self.assertIsNotNone(profile)
        self.assertEqual(profile.payload["bot_id"], "77777")

        regular = replace(make_event(actor="10001", text="这是机器人"),
                          segments=[{"type": "at", "data": {"qq": "88888"}},
                                    {"type": "text", "data": {"text": "这是机器人"}}])
        await self.service.observe(regular, GroupPolicy())
        self.assertIsNone(await self.repo.get(self.service._profile_id("10001", "88888")))

    async def test_admin_list_is_persistent_and_used_for_registration(self):
        await self.service.set_admin_ids(["20000"])
        self.assertEqual(await self.service.admin_ids_configured(), ["10000", "20000"])
        admin = replace(make_event(actor="20000", text="这是机器人"),
                        segments=[{"type": "at", "data": {"qq": "77777"}},
                                  {"type": "text", "data": {"text": "这是机器人"}}])
        await self.service.observe(admin, GroupPolicy())
        self.assertIsNotNone(await self.repo.get(self.service._profile_id("10001", "77777")))

    async def test_human_chat_is_not_learned_or_shown_as_registered_bot_workflow(self):
        admin = replace(make_event(actor="10000"), actor_role="owner")
        await self.service.register(admin, "77777")
        request = replace(make_event(actor="10001", message_id="human-request", text="你在吗", mention=False),
                          segments=[{"type": "at", "data": {"qq": "88888"}},
                                    {"type": "text", "data": {"text": "你在吗"}}])
        await self.service.observe(request, GroupPolicy())
        reply = make_event(actor="88888", message_id="human-reply", text="在", mention=False)
        await self.service.observe(reply, GroupPolicy())
        self.assertEqual(await self.repo.find("bot_skill_observation"), [])
        await self.repo.put(Record("old-human-workflow", "bot_skill_workflow", None, "10001", "active",
                                   {"bot_id": "88888", "summary": "普通聊天", "steps": []}))
        context = await self.service.prompt_context(request.destination)
        self.assertEqual(context["learned_workflows"], [])

    async def test_registration_reuses_previously_observed_slash_command(self):
        event = replace(make_event(actor="10000"), actor_role="owner")
        await self.repo.put(Record("old-observation", "bot_skill_observation", None, "10001", "candidate",
                                   {"bot_id": "77777", "trigger_command": "/今日老婆",
                                    "response_text": "你的今日老婆是香草"}))
        profile = await self.service.register(event, "77777", "抽老婆机器人")
        self.assertEqual(profile.payload["capabilities"], ["/今日老婆"])
        self.assertIn("你的今日老婆", profile.payload["summary"])
        self.assertEqual(profile.payload["bot_name"], "抽老婆机器人")

    async def test_prompt_context_bounds_registered_workflows(self):
        event = replace(make_event(actor="10000"), actor_role="owner")
        await self.service.register(event, "77777")
        for index in range(20):
            await self.repo.put(Record(str(index), "bot_skill_workflow", None, "10001", "active",
                                       {"bot_id": "77777", "summary": "点歌", "steps": [
                                           {"input": "旧步骤", "output": "x" * 1000},
                                           {"input": "输入序号", "output": "y" * 1000}]}))
        context = await self.service.prompt_context(event.destination)
        self.assertEqual(len(context["learned_workflows"]), 8)
        self.assertEqual(len(context["learned_workflows"][0]["steps"][-1]["output"]), 240)

    async def test_direct_request_passes_all_registered_bots_to_thinking(self):
        admin=replace(make_event(actor="10000"),actor_role="owner")
        profile=await self.service.register(admin,"77777","点歌机器人")
        await self.repo.patch(profile.id,payload={**profile.payload,"summary":"会唱歌和点歌"})
        await self.service.register(admin,"88888","快递查询机器人")
        settings=SimpleNamespace(decision_mode='local',decision_confidence_threshold=.8,
            thinking_max_steps=3,thinking_max_seconds=10,max_reply_chars=800,
            reply_mode='integrated',group_file_download_enabled=True,bot_id='99999')
        v2=SimpleNamespace(registry=None,call=AsyncMock(return_value={
            'payload':{'context':{'dependency_snapshot':{}}}}))
        features=SimpleNamespace(bot_skill_context=self.service.prompt_context)
        service=V3Service(settings,SimpleNamespace(),v2,self.store,features=features)
        service.thinking.run=AsyncMock(return_value=ThinkResult('song-session','SILENT'))
        event=make_event(text='帮我查询快递')
        result=await service.respond(event,[],{'direct_request':True,'source_ids':[event.id]})
        self.assertIsNone(result)
        context=service.thinking.run.await_args.args[1]
        self.assertEqual(set(context['registered_group_bot_ids']),{'77777','88888'})
        self.assertTrue(context['bot_actions_requested'])

    async def test_probe_is_sent_and_survives_into_observation(self):
        event = replace(make_event(actor="10000", text="登记"), actor_role="owner")
        await self.service.register(event, "77777", "点歌机器人")
        result = await self.service.probe(event, "77777", "/help")
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.platform.calls[0][0], "send_group_msg")
        self.assertEqual(self.platform.calls[0][1]["message"][0]["data"]["qq"], "77777")
        bot_reply = replace(make_event(actor="77777", message_id="reply", text="1. 点歌\n2. 帮助", mention=False), actor_name="点歌机器人")
        await self.service.observe(bot_reply, GroupPolicy())
        observations = await self.repo.find("bot_skill_observation")
        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0].payload["probe"])

    async def test_numeric_followup_is_linked_to_the_same_bot_workflow(self):
        admin = replace(make_event(actor="10000", text="这是机器人"), actor_role="admin",
                        segments=[{"type": "at", "data": {"qq": "77777"}},
                                  {"type": "text", "data": {"text": "这是机器人"}}])
        await self.service.observe(admin, GroupPolicy())
        request = replace(make_event(actor="10001", message_id="request", text="/help", mention=False),
                          segments=[{"type": "at", "data": {"qq": "77777"}},
                                    {"type": "text", "data": {"text": "/help"}}])
        await self.service.observe(request, GroupPolicy())
        first = replace(make_event(actor="77777", message_id="first", text="1. 点歌\n2. 帮助", mention=False), actor_name="点歌机器人")
        await self.service.observe(first, GroupPolicy())
        selection = replace(make_event(actor="10001", message_id="selection", text="2", mention=False))
        await self.service.observe(selection, GroupPolicy())
        second = replace(make_event(actor="77777", message_id="second", text="帮助：输入 /help 查看菜单", mention=False), actor_name="点歌机器人")
        await self.service.observe(second, GroupPolicy())
        workflows = await self.repo.find("bot_skill_workflow")
        self.assertTrue(workflows)
        self.assertIn("2", workflows[-1].payload["summary"])
        context = await self.service.prompt_context(request.destination)
        self.assertEqual(context["registered_bots"][0]["bot_id"], "77777")
        self.assertTrue(context["learned_workflows"])
        self.assertEqual(self.platform.calls, [])

    async def test_ordinary_bot_command_is_observed_not_replayed(self):
        admin = replace(make_event(actor="10000"), actor_role="owner")
        await self.service.register(admin, "77777")
        event = make_event(text="点歌 晴天", mention=False)
        result = await self.service.probe(event, "77777", "点歌 晴天")
        self.assertEqual(result["error"], "BOT_ACTION_NOT_REQUESTED")
        self.assertEqual(self.platform.calls, [])

    async def test_explicit_request_is_sent_once_across_concurrency_and_restart(self):
        event = replace(make_event(actor="10000", text="帮我点歌"), actor_role="owner")
        await self.service.register(event, "77777")
        results = await asyncio.gather(*[
            self.service.probe(event, "77777", "点歌 晴天", requested=True) for _ in range(3)])
        self.assertEqual(len(self.platform.calls), 1)
        self.assertEqual(sum(bool(result.get("deduplicated")) for result in results), 2)
        restarted = BotSkillLearningService(self.repo, self.store, "99999", enabled=True, platform=self.platform)
        self.assertTrue((await restarted.probe(event, "77777", "点歌 晴天", requested=True))["deduplicated"])
        self.assertEqual(len(self.platform.calls), 1)

    async def test_human_help_command_is_not_replayed(self):
        admin = replace(make_event(actor="10000"), actor_role="owner")
        await self.service.register(admin, "77777")
        event = make_event(text="/help", mention=False)
        self.assertEqual((await self.service.probe(event, "77777", "/help"))["error"], "BOT_ACTION_NOT_REQUESTED")
        self.assertEqual(self.platform.calls, [])

    async def test_bot_reply_mention_is_not_a_new_user_request(self):
        admin = replace(make_event(actor="10000"), actor_role="owner")
        await self.service.register(admin, "77777")
        source = make_event(actor="77777", text="正在处理，请稍候", mention=True)
        result = await self.service.probe(source, "77777", "点歌 晴天", requested=True)
        self.assertEqual(result["error"], "BOT_ACTION_NOT_REQUESTED")
        self.assertEqual(self.platform.calls, [])

    async def test_nova_managed_numeric_followup_is_forwarded_only_once(self):
        event = replace(make_event(actor="10000", text="帮我点歌"), actor_role="owner")
        await self.service.register(event, "77777")
        await self.service.probe(event, "77777", "点歌 晴天", requested=True)
        menu = make_event(actor="77777", message_id="menu", text="1. 晴天\n2. 其他歌", mention=False)
        await self.service.observe(menu, GroupPolicy())
        flow = await self.service._flow("10001")
        self.assertTrue(flow.payload["nova_managed"])
        selection = make_event(actor="10000", message_id="selection", text="1", mention=False)
        await self.service.observe(selection, GroupPolicy())
        await self.service.observe(selection, GroupPolicy())
        self.assertEqual(len(self.platform.calls), 2)

    async def test_tool_does_not_treat_initiative_as_bot_action_authorization(self):
        from osa.application.v3.tools import AgentTools

        admin = replace(make_event(actor="10000"), actor_role="owner")
        await self.service.register(admin, "77777")
        source = make_event(text="点歌 晴天", mention=False)
        await self.store.publish(source, True)

        async def call(name, *args):
            if name == "thinking":
                return {"owner_id": source.actor_id, "group_id": "10001"}
            if name == "receipt":
                return "receipt", None

        tools = AgentTools(SimpleNamespace(call=AsyncMock(side_effect=call)), self.store,
                           features=SimpleNamespace(bot_skills=self.service))
        context = {"actor_id": source.actor_id, "group_id": "10001", "source_id": source.id,
                   "source_event": source.to_dict(), "session_id": "thinking", "tool_call_id": "first",
                   "bot_actions_requested": False, "deadline": time.time() + 60}
        result = await asyncio.wait_for(tools.execute("bot.probe", {"bot_id": "77777", "command": "点歌 晴天"}, context), 3)
        self.assertEqual(result["error"], "BOT_ACTION_NOT_REQUESTED")
        self.assertEqual(self.platform.calls, [])
        context["bot_actions_requested"] = True
        result = await asyncio.wait_for(tools.execute("bot.probe", {"bot_id": "77777", "command": "点歌 晴天"}, context), 3)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(self.platform.calls), 1)

