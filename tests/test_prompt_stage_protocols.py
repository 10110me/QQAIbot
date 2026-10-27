import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from osa.application.prompt_catalog import CATALOG, validate_template
from osa.application.prompting import PromptGuard
from osa.application.v3.thinking import ThinkingLoop, ReplyWriter, parse_action
from osa.application.v3.service import registered_group_bot_ids
from osa.domain.entities import ModelResult
from osa.infrastructure.runtime_config import PersonaProfile, RuntimeConfiguration, RuntimeConfigurationStore
from osa.domain.agents import ReplyBrief
from osa.application.token_context import estimate_messages_tokens


class PromptStageTests(unittest.IsolatedAsyncioTestCase):
    def test_extra_closing_brace_is_repaired_without_accepting_extra_actions(self):
        response='{"final_action":"REPLY","reply_brief":{"reply_text":"在呢"}}}'
        self.assertEqual(parse_action(response)['reply_brief']['reply_text'], '在呢')
        for invalid in (response + '{"final_action":"SILENT"}', response + 'junk',
                        '{"final_action":"REPLY","reply_brief":'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_action(invalid)

    async def test_integrated_reply_survives_extra_closing_brace(self):
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
            '{"final_action":"REPLY","reply_brief":{"reply_text":"在呢"}}}', 'test', 'thinking')))
        outcome=await ThinkingLoop(model,SimpleNamespace(execute=AsyncMock())).run(
            [],{'reply_mode':'integrated'})
        self.assertEqual(outcome.final_action,'REPLY')
        self.assertEqual(outcome.brief.reply_text,'在呢')
        model.generate.assert_awaited_once()

    async def test_thinking_prompt_is_compact_and_tool_details_follow_chat(self):
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
            '{"final_action":"SILENT"}', 'test', 'thinking')))
        await ThinkingLoop(model,SimpleNamespace(features=None)).run(
            [{'role':'user','content':'[时间:20:00] 群友甲: 大家晚上好'}],
            {'reply_mode':'integrated','actor_id':'12345','group_id':'10001'})
        messages=model.generate.await_args.args[0]
        systems='\n'.join(item['content'] for item in messages if item['role']=='system')
        self.assertLess(estimate_messages_tokens([{'role': 'system', 'content': systems}]), 6000)
        self.assertIn('工具概览',messages[0]['content'])
        self.assertIn('确实能接上话或合适地表示反应',messages[0]['content'])
        self.assertIn('空的 request_ids 列表不授权任何委派行动',systems)
        self.assertIn('不要把其他人的请求变成你的任务',systems)
        self.assertNotIn('交给回复模型',systems)
        self.assertEqual(messages[-1]['role'],'system')
        self.assertIn('【工具参考',messages[-1]['content'])
        self.assertIn('moderation.mute(action,target_ids,duration_seconds)',messages[-1]['content'])
        self.assertLess(messages.index({'role':'user','content':'[时间:20:00] 群友甲: 大家晚上好'}),len(messages)-1)

    async def test_integrated_reply_length_guidance_follows_custom_style_without_extra_calls(self):
        for custom in (False, True):
            with self.subTest(custom=custom):
                config = RuntimeConfiguration()
                config.persona.reply_style = 'Prefer elaborate expression.'
                if custom:
                    config.prompt_slots['reply_writer'] = 'Custom voice; maximum {最大字数} characters.'
                guard = PromptGuard(RuntimeConfigurationStore('', config, memory_only=True))
                model = SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
                    '{"final_action":"REPLY","reply_brief":{"reply_text":"在呢。"}}', 'test', 'thinking')))
                outcome = await ThinkingLoop(model, SimpleNamespace(features=None), prompt_guard=guard).run(
                    [], {'reply_mode': 'integrated', 'actor_id': '12345', 'max_reply_chars': 500})
                system = model.generate.await_args.args[0][0]['content']
                self.assertIn('闲聊通常 1—2 条短句', system)
                self.assertIn('普通答疑通常 160 字以内', system)
                self.assertIn('用户明确要细节', system)
                self.assertIn('这些限制不适用于实际工具参数或任务产物', system)
                self.assertIn('500 字符仅为公开文本上限而非目标', system)
                if custom:
                    self.assertLess(system.index('Custom voice'), system.index('【回复长度】'))
                self.assertEqual(outcome.brief.reply_text, '在呢。')
                model.generate.assert_awaited_once()

    def test_reply_writer_keeps_custom_style_with_shared_length_guidance(self):
        config = RuntimeConfiguration()
        config.prompt_slots['reply_writer'] = 'Custom voice; maximum {最大字数} characters.'
        guard = PromptGuard(RuntimeConfigurationStore('', config, memory_only=True))
        prompt = guard.reply_prompt(500)
        self.assertLess(prompt.index('Custom voice'), prompt.index('【回复长度】'))
        self.assertIn('500 字符仅为公开文本上限而非目标', prompt)
        self.assertIn('用户明确要细节', prompt)

    def test_nova_voice_card_keeps_behavior_rules_separate(self):
        card=(Path(__file__).resolve().parents[1]/'docs'/'nova-persona-card.md').read_text(encoding='utf-8')
        profile=PersonaProfile(name='Nova',reply_style=card)
        config=RuntimeConfiguration(persona=profile)
        prompt=PromptGuard(RuntimeConfigurationStore('',config,memory_only=True)).thinking_persona_prompt({'actor_id':'12345'})
        self.assertIn('owner_qq_ids',prompt)
        self.assertIn('先理解对话并选择行动',prompt)
        self.assertIn('默认像 QQ 聊天框里正在接话的人',card)
        self.assertNotIn('work.submit',card)
        self.assertLess(len(card),1000)

    def test_nova_voice_is_independent_without_forcing_disagreement_or_banning_words(self):
        card = (Path(__file__).resolve().parents[1] / 'docs' / 'nova-persona-card.md').read_text(encoding='utf-8')
        config = RuntimeConfiguration(persona=PersonaProfile(name='Nova', reply_style=card))
        guard = PromptGuard(RuntimeConfigurationStore('', config, memory_only=True))
        prompt = guard.thinking_persona_prompt({'actor_id': '12345'})
        for guidance in ('不把普通聊天包装成咨询建议', '也不用每条都夸人',
                         '不必句句有信息增量', '必须有接话的理由', '不固定开头'):
            self.assertIn(guidance, prompt)
        self.assertEqual(guard.filter_output('好的，已经完成。'), '好的，已经完成。')

    async def test_cached_qqbot_catalog_is_compact_but_keeps_usage(self):
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
            '{"final_action":"SILENT"}', 'test', 'thinking')))
        qqbot=SimpleNamespace(enabled=True,catalog_for_prompt=AsyncMock(return_value={
            'tools':[{'tool':'interaction.image_generation','name':'图片生成',
                      'usage':'/生成图片 <描述>','arguments':{'prompt':'string'},
                      'returns':'图片','module':'features.interaction.image_generation'}],
            'fetched_at':1,'expires_at':2}))
        await ThinkingLoop(model,SimpleNamespace(features=SimpleNamespace(qqbot_tools=qqbot))).run(
            [],{'reply_mode':'integrated','actor_id':'12345'})
        messages=model.generate.await_args.args[0]
        catalog=next(item['content'] for item in messages if 'qqbot_cached_tools' in item['content'])
        self.assertIn('/生成图片 <描述>',catalog)
        self.assertNotIn('"module"',catalog)
        self.assertNotIn('"fetched_at"',catalog)
        self.assertEqual(messages[-1]['role'],'system')

    def guard(self):
        value=RuntimeConfiguration()
        value.persona.name='Nova'
        value.prompt_slots['agent_thinking']='旧自定义行为要求：请简短回复。'
        return PromptGuard(RuntimeConfigurationStore('',value,memory_only=True))

    def test_registered_group_bots_are_not_limited_to_music(self):
        profiles=[{'bot_id':'100000004','name':'大肥鱼','summary':'已观察到点歌 Lemon、唱首晚安喵'},
                  {'bot_id':'88888','name':'快递机器人','capabilities':['查询快递']},
                  {'bot_id':'88888','name':'重复资料'}, {'bot_id':'bad','summary':'点歌'}]
        self.assertEqual(registered_group_bot_ids(profiles),['100000004','88888'])

    async def test_general_route_keeps_group_bot_distinct_from_qqbot_catalog(self):
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
            '{"final_action":"SILENT"}','test','thinking')))
        tools=SimpleNamespace(features=None)
        loop=ThinkingLoop(model,tools,prompt_guard=self.guard())
        await loop.run([],{'source_id':'tool-request','actor_id':'12345','group_id':'10001',
                           'registered_group_bot_ids':['100000004','88888']})
        messages=model.generate.await_args.args[0]
        system=messages[0]['content']
        self.assertIn('100000004',system)
        self.assertIn('88888',system)
        self.assertIn('registered_bots',system)
        self.assertIn('bot.probe',messages[-1]['content'])
        self.assertIn('qqbot.tools.list',messages[-1]['content'])

    def test_all_templates_validate_and_stage_contracts_remain_separate(self):
        for key,spec in CATALOG.items():
            with self.subTest(slot=key):validate_template(spec['default'])
        guard=self.guard()
        for key,purpose in [('agent_decision','decision'),('agent_thinking','thinking'),
                            ('passive_memory','memory'),('memory','memory'),('work_decision','decision')]:
            messages=guard.prepare_messages([{'role':'system','content':CATALOG[key]['default']},
                {'role':'user','content':'input'}],purpose)
            self.assertTrue(messages[-2]['content'].startswith('【阶段输出协议：'+key+'】'))
            self.assertEqual(guard.prepare_messages(messages,purpose),messages)
            self.assertEqual(messages[-1]['role'],'user')

    async def test_plain_chat_output_is_repaired_then_reply_action_reaches_reply_writer(self):
        model=SimpleNamespace(generate=AsyncMock(side_effect=[
            ModelResult('在呢，叫我干嘛呀','test','thinking'),
            ModelResult(json.dumps({'tool_call':{'tool_call_id':'reply-1','name':'reply',
                'arguments':{'intent':'回应点名','key_points':['表示在听'],'evidence_ids':[],'limitations':[]}}}), 'test','thinking'),
            ModelResult('在呢，找我什么事？','test','reply')]))
        tools=SimpleNamespace(execute=AsyncMock())
        guard=self.guard()
        result=await ThinkingLoop(model,tools,prompt_guard=guard).run([
            {'role':'system','content':guard.system_prompt()},
            {'role':'user','content':'Nova你在吗？'}],{'evidence_ids':[]})
        self.assertEqual(result.final_action,'REPLY')
        tools.execute.assert_not_awaited()
        request=model.generate.await_args_list[0].args[0]
        systems='\n'.join(m['content'] for m in request if m['role']=='system')
        self.assertNotIn('【提示词模板：chat】',systems)
        self.assertIn('【阶段输出协议：agent_thinking】',systems)
        self.assertIn('旧自定义行为要求',systems)
        reply=await ReplyWriter(model,guard).render(result.brief)
        self.assertEqual(reply.text,'在呢，找我什么事？')
        self.assertEqual(model.generate.await_count,3)

    async def test_repeated_malformed_output_is_bounded_and_never_forwarded(self):
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult('直接向群里发的话','test','thinking')))
        tools=SimpleNamespace(execute=AsyncMock())
        result=await ThinkingLoop(model,tools).run([],{'evidence_ids':[]})
        self.assertEqual(result.final_action,'FAILED')
        self.assertIsNone(result.brief)
        self.assertEqual(model.generate.await_count,2)
        tools.execute.assert_not_awaited()

    async def test_reply_cannot_smuggle_unknown_evidence_or_attachments(self):
        for field in ['evidence_ids','attachment_ids']:
            model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(json.dumps({
                'tool_call':{'tool_call_id':'r','name':'reply','arguments':{'key_points':['看这里'],field:['invented-id']}}}), 'test','thinking')))
            result=await ThinkingLoop(model,SimpleNamespace(execute=AsyncMock())).run([],{'evidence_ids':[]})
            self.assertEqual(result.final_action,'FAILED')
            self.assertIsNone(result.brief)

    async def test_reply_keeps_editable_slots_and_persona_without_repeating_kernel_or_style(self):
        config=RuntimeConfiguration()
        config.persona.reply_style='独有的管理员回复风格'
        config.prompt_slots['reply_writer']='自定义润色要求，最大{最大字数}字。'
        guard=PromptGuard(RuntimeConfigurationStore('',config,memory_only=True))
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult('在呢','test','reply')))
        brief=ReplyBrief('回应',['在呢'],limitations=['任务仍在排队'])
        result=await ReplyWriter(model,guard).render(brief,800)
        messages=model.generate.await_args.args[0]
        system=messages[0]['content']
        self.assertEqual(system.count('【不可修改的系统边界】'),1)
        self.assertEqual(system.count('独有的管理员回复风格'),1)
        self.assertIn('自定义润色要求，最大800字',system)
        self.assertEqual(json.loads(messages[1]['content'])['limitations'],['任务仍在排队'])
        self.assertEqual(result.text,'在呢')

    async def test_detailed_reply_is_not_cut_to_casual_chat_guideline(self):
        text='第一步，检查输入条件。第二步，按要求完成处理并核对结果。'*4
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(text,'test','reply')))
        result=await ReplyWriter(model,self.guard()).render(ReplyBrief('按要求详细说明',['解释具体步骤']),800)
        self.assertEqual(result.text,text)
        self.assertGreater(len(result.text),50)

    async def test_game_capability_boundary_handles_initiative_and_direct_invite(self):
        guard=self.guard()
        self.assertIn('不虚构自己是人类',guard.persona_prompt())
        self.assertIn('不虚构自己是人类',guard.prepare_messages([
            {'role':'system','content':guard.persona_prompt()},
            {'role':'user','content':'开个新坑？'}], 'thinking')[0]['content'])
        self.assertTrue(guard.claims_game_participation('看你们先挑个整合包，定了我再上坑'))
        self.assertTrue(guard.claims_game_participation('我跟你们一起玩 MC'))
        self.assertFalse(guard.claims_game_participation('我不能进服，但可以帮你们讨论整合包'))
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
            '定了我再上坑','test','reply')))
        writer=ReplyWriter(model,guard)
        brief=ReplyBrief('回应',['定了我再上坑'])
        initiative=await writer.render(brief,conversation=[])
        self.assertEqual(initiative.text,'')
        direct=await writer.render(brief)
        self.assertEqual(direct.text,'我不能直接进入游戏，但可以在群里帮你们讨论玩法。')
