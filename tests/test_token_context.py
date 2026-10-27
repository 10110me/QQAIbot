import json
import unittest

import httpx

from osa.application.token_context import (
    ContextBudgetExceeded, estimate_messages_tokens, fit_messages, input_token_budget,
)
from osa.infrastructure.configured_models import ConfiguredModelProvider
from osa.infrastructure.runtime_config import (
    ModelConfig, ProviderConfig, RuntimeConfiguration, RuntimeConfigurationStore, TaskConfig,
)


class TokenContextTests(unittest.TestCase):
    def test_old_messages_are_trimmed_but_current_request_and_latest_tool_pair_remain(self):
        current = {"role": "user", "content": "<untrusted_message_context actor_id='1'>\n画一只猫\n</untrusted_message_context>"}
        quote = {"role": "user", "content": "<untrusted_quoted_message actor_id='1'>\n引用的原图\n</untrusted_quoted_message>"}
        first_call = {"role": "assistant", "content": '{"tool_call_id":"1"}'}
        first_result = {"role": "user", "content": json.dumps({"tool_result": {"items": [1]}, "tool_call_id": "1"})}
        last_call = {"role": "assistant", "content": '{"tool_call_id":"2"}'}
        last_result = {"role": "user", "content": json.dumps({"tool_result": {"image": "ready"}, "tool_call_id": "2"})}
        messages = [
            {"role": "system", "content": "系统规则"},
            {"role": "user", "content": "旧消息" * 1000},
            {"role": "assistant", "content": "旧回复" * 1000},
            quote, current,
            {"role": "user", "content": "工具缓存" * 1000},
            first_call, first_result, last_call, last_result,
        ]
        budget = estimate_messages_tokens([messages[0], quote, current, last_call, last_result]) + 5
        trimmed, dropped = fit_messages(messages, budget)
        self.assertGreater(dropped, 0)
        self.assertIn(current, trimmed)
        self.assertIn(quote, trimmed)
        self.assertIn(last_call, trimmed)
        self.assertIn(last_result, trimmed)
        self.assertEqual(first_call in trimmed, first_result in trimmed)
        self.assertLessEqual(estimate_messages_tokens(trimmed), budget)

    def test_required_content_over_budget_fails_without_silent_truncation(self):
        with self.assertRaises(ContextBudgetExceeded):
            fit_messages([{"role": "system", "content": "必须保留" * 1000},
                          {"role": "user", "content": "当前请求"}], 20)

    def test_output_reservation_reduces_input_budget(self):
        self.assertEqual(input_token_budget(4096, 512), 2713)


class TokenContextProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_request_uses_model_window_and_preserves_tool_receipt(self):
        configuration = RuntimeConfiguration(
            providers=[ProviderConfig(id="p", name="Provider", base_url="https://example.com", max_retry=0)],
            models=[ModelConfig(model_identifier="model", name="Model", api_provider="p",
                                context_window_tokens=32768, max_tokens=256)],
            task_configs={"planner": TaskConfig(model_list=["model"])},
        )
        provider = ConfiguredModelProvider(RuntimeConfigurationStore("unused", configuration, memory_only=True))
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "完成"}}]})

        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        current = {"role": "user", "content": "<untrusted_message_context actor_id='1'>\n请画图\n</untrusted_message_context>"}
        receipt = {"role": "user", "content": json.dumps({"tool_result": {"ok": True}, "tool_call_id": "new"})}
        messages = [{"role": "system", "content": "规则"},
                    {"role": "user", "content": "old" * 20000}, current,
                    {"role": "user", "content": "cached" * 20000},
                    {"role": "assistant", "content": "tool call"}, receipt]
        try:
            result = await provider.generate(messages, "thinking")
        finally:
            await provider.close()
        self.assertEqual(result.text, "完成")
        sent = requests[0]["messages"]
        self.assertIn(current, sent)
        self.assertIn(receipt, sent)
        self.assertNotIn(messages[1], sent)
        self.assertLessEqual(estimate_messages_tokens(sent), 20000)
        self.assertLessEqual(estimate_messages_tokens(sent), input_token_budget(32768, 256))
        self.assertEqual(requests[0]["max_tokens"], 256)


class VideoBudgetTests(unittest.TestCase):
    def test_video_payload_is_not_counted_as_base64_text_or_dropped(self):
        current = {'role': 'user', 'content': [
            {'type': 'text', 'text': '按时间顺序描述'},
            {'type': 'video_url', 'video_url': {'url': 'data:video/mp4;base64,' + 'A' * 500000}}]}
        estimate = estimate_messages_tokens([current])
        self.assertGreaterEqual(estimate, 16384)
        self.assertLess(estimate, 17000)
        fitted, dropped = fit_messages([{'role': 'user', 'content': 'old' * 30000}, current], 20000)
        self.assertEqual(dropped, 1)
        self.assertEqual(fitted, [current])


if __name__ == "__main__":
    unittest.main()
