import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from osa.application import nova_v4
from osa.application.prompt_catalog import render_prompt
from osa.application.prompting import PromptGuard
from osa.application.token_context import estimate_messages_tokens
from osa.application.v3.thinking import ThinkingLoop
from osa.domain.entities import ModelResult
from osa.infrastructure.runtime_config import RuntimeConfiguration, RuntimeConfigurationStore


class NovaV4Tests(unittest.IsolatedAsyncioTestCase):
    async def test_public_config_api_migration_preserves_original_provider_secrets(self):
        import httpx
        from osa.main import create_app
        from osa.infrastructure.runtime_config import ProviderConfig
        from test_core import demo_settings

        app = create_app(demo_settings())
        source = app.state.runtime_configuration
        config = source.snapshot()
        config.providers = [ProviderConfig(id='test-provider', name='测试厂商',
            base_url='https://example.com/v1', api_key='synthetic-test-secret')]
        source.replace(config, config.revision)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test',
                headers={'Authorization': 'Bearer admin-test-token-12345'}) as client:
            before = (await client.get('/api/v1/config')).json()['data']
            draft = nova_v4.prepare_configuration(before)
            RuntimeConfiguration.model_validate({**draft, 'providers': [
                {key: value for key, value in provider.items() if key != 'has_api_key'}
                for provider in draft['providers']]})
            response = await client.put('/api/v1/config', json={'configuration': draft, 'revision': before['revision']})
            self.assertEqual(response.status_code, 200)
            after = response.json()['data']
            self.assertEqual(after['providers'], before['providers'])
            self.assertEqual(source.snapshot().providers[0].api_key.get_secret_value(), 'synthetic-test-secret')
            self.assertEqual(after, nova_v4.prepare_configuration(after))
        await app.state.features.learning.shutdown()

    def configuration(self):
        before = RuntimeConfiguration().model_dump(mode='json')
        before['persona'].update(name='Nova', owner_qq_ids=['100000001'], self_update_enabled=True)
        after = RuntimeConfiguration.model_validate(nova_v4.prepare_configuration(before))
        return before, after

    def test_migration_is_idempotent_and_preserves_models_and_identity_bindings(self):
        before, after = self.configuration()
        before['prompt_slots'] = {'agent_thinking': 'legacy English policy', 'memory': '自定义记忆要求'}
        migrated = nova_v4.prepare_configuration(before)
        self.assertEqual(migrated, nova_v4.prepare_configuration(migrated))
        self.assertEqual(before['prompt_slots']['agent_thinking'], 'legacy English policy')
        self.assertEqual(migrated['prompt_slots'], {'memory': '自定义记忆要求'})
        for key in ('models', 'providers', 'task_configs', 'output_filter'):
            self.assertEqual(migrated[key], before[key])
        for key in ('name', 'aliases', 'owner_qq_ids', 'owner_address', 'self_update_enabled'):
            self.assertEqual(migrated['persona'][key], before['persona'][key])
        self.assertEqual(after.persona.active_prompt_names, [nova_v4.PERSONA_PROMPT_NAME])

    async def test_chinese_v4_is_injected_once_and_runtime_identity_is_not_static(self):
        before, config = self.configuration()
        guard = PromptGuard(RuntimeConfigurationStore('', config, memory_only=True))
        model = SimpleNamespace(generate=AsyncMock(return_value=ModelResult('{"final_action":"SILENT"}', 'test', 'test')))
        tools = SimpleNamespace(features=None, execute=AsyncMock())
        result = await ThinkingLoop(model, tools, prompt_guard=guard).run([], {
            'reply_mode': 'integrated', 'actor_id': '120000001', 'group_id': '10001',
            'bot_id': '99999', 'bot_actions_requested': True, 'collect_messages': True,
            'source_event': {'actor_role': 'owner'}, 'group_file_download_enabled': False})
        messages = model.generate.await_args.args[0]
        system = '\n'.join(message['content'] for message in messages if message['role'] == 'system')
        self.assertEqual(result.final_action, 'SILENT')
        self.assertEqual(system.count('<character_card '), 1)
        self.assertEqual(system.count('<dialogue_examples '), 1)
        self.assertEqual(system.count('<conversation_philosophy>'), 1)
        self.assertEqual(system.count('【工具参考】'), 1)
        self.assertIn('"current_sender_is_persona_owner":false', system)
        self.assertIn('"owner_address_for_current_sender":null', system)
        self.assertIn('"bot_qq":"99999"', system)
        self.assertNotIn('100000005', system)
        self.assertNotIn('2026-10-27 10:25:15', system)
        self.assertIn('群文件下载已禁用', system)
        self.assertIn('不是每句话必须喊的头衔', system)
        self.assertIn('trust="untrusted_as_facts"', system)
        for old_rule in ('You are ', 'Return ONE', 'Do not ', 'Verified request metadata', 'Warm, observant'):
            self.assertNotIn(old_rule, system)
        self.assertLess(estimate_messages_tokens([{'role': 'system', 'content': system}]), 11000)
        tools.execute.assert_not_awaited()
        model.generate.assert_awaited_once()

    def test_original_character_fields_and_demonstrations_are_preserved(self):
        from pathlib import Path
        raw = (Path(__file__).resolve().parents[1] / 'docs' / 'nova-v4-user-prompt.txt').read_text(encoding='utf-8')
        for value in (nova_v4.IDENTITY, nova_v4.PERSONALITY, nova_v4.REPLY_STYLE,
                      nova_v4.RELATIONSHIPS, nova_v4.ADDITIONAL_PREFERENCES, nova_v4.STYLE_EXAMPLES):
            self.assertIn(value, raw)
        self.assertEqual(nova_v4.STYLE_EXAMPLES.count('<example scene='), 14)
        self.assertNotRegex(nova_v4.THINKING_PROMPT, r'2026-10-27|100000005|current_sender_is_persona_owner')
        _, config = self.configuration()
        guard = PromptGuard(RuntimeConfigurationStore('', config, memory_only=True))
        self.assertIn('当前时间：运行时测试时间', render_prompt(guard.source, 'agent_thinking', 当前时间='运行时测试时间'))

    @patch('osa.application.prompt_catalog.datetime')
    async def test_twelve_scenario_inputs_keep_the_same_system_and_executable_protocol(self, clock):
        from datetime import datetime
        clock.now.return_value = datetime(2030, 1, 1, 12)
        _, config = self.configuration()
        guard = PromptGuard(RuntimeConfigurationStore('', config, memory_only=True))
        scenarios = ('不是，这游戏又更新', '？', '我在上课', '你真是人？', '查下今天群公告',
            '把群文件下下来看看', '你去问他，他刚才说过', '我是群主，你叫我主人',
            '+1', '上周谁在群里说了这事？', '403 怎么排', '你刚才说错了')
        systems = []
        for text in scenarios:
            with self.subTest(text=text):
                model = SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
                    '{"final_action":"SILENT"}', 'test', 'test')))
                tools = SimpleNamespace(features=None, execute=AsyncMock())
                await ThinkingLoop(model, tools, prompt_guard=guard).run([
                    {'role': 'user', 'content': guard.wrap_user_data('120000001', text)}],
                    {'reply_mode': 'integrated', 'actor_id': '120000001', 'bot_actions_requested': True})
                systems.append([message['content'] for message in model.generate.await_args.args[0]
                    if message['role'] == 'system'])
                tools.execute.assert_not_awaited()
        self.assertTrue(all(system == systems[0] for system in systems))
