import asyncio
import unittest

import httpx

from osa.infrastructure.configured_models import ConfiguredModelProvider
from osa.infrastructure.runtime_config import (
    ModelConfig, ProviderConfig, RuntimeConfiguration, RuntimeConfigurationStore, TaskConfig,
)


class APIConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_endpoint_allows_ten_simultaneous_requests(self):
        configuration = RuntimeConfiguration(
            providers=[ProviderConfig(id='first', name='first', base_url='https://example.com/v1', api_key='key', max_retry=0),
                       ProviderConfig(id='second', name='second', base_url='https://example.com/v1/', api_key='key', max_retry=0)],
            models=[ModelConfig(model_identifier='first-model', name='first', api_provider='first'),
                    ModelConfig(model_identifier='second-model', name='second', api_provider='second')],
            task_configs={'replyer': TaskConfig(model_list=['first-model'])},
        )
        first_store = RuntimeConfigurationStore('unused', configuration, memory_only=True)
        second_configuration = configuration.model_copy(deep=True)
        second_configuration.task_configs['replyer'] = TaskConfig(model_list=['second-model'])
        second_store = RuntimeConfigurationStore('unused', second_configuration, memory_only=True)
        providers = [ConfiguredModelProvider(first_store), ConfiguredModelProvider(second_store)]
        reached_limit = asyncio.Event()
        release = asyncio.Event()
        active = peak = 0

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 10:
                reached_limit.set()
            try:
                await release.wait()
                return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
            finally:
                active -= 1

        for provider in providers:
            await provider.client.aclose()
            provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        jobs = [asyncio.create_task(providers[index % 2].generate([{'role': 'user', 'content': 'hello'}], 'reply'))
                for index in range(12)]
        try:
            await asyncio.wait_for(reached_limit.wait(), 3)
            await asyncio.sleep(.05)
            self.assertEqual(peak, 10)
            release.set()
            self.assertEqual(len(await asyncio.gather(*jobs)), 12)
            self.assertEqual(peak, 10)
        finally:
            release.set()
            await asyncio.gather(*jobs, return_exceptions=True)
            for provider in providers:
                await provider.close()
