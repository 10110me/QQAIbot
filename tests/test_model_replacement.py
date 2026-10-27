import json
import tempfile
import unittest
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from osa.config import AdminAccount
from osa.infrastructure.configured_models import ConfiguredModelProvider
from osa.infrastructure.runtime_config import ModelConfig, RuntimeConfiguration, RuntimeConfigurationStore
from osa.interfaces.api import require_admin
from osa.interfaces.configuration import router
import test_model_identifier_edit as editing


class ModelReplacementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = editing.ModelIdentifierEditTests.asyncSetUp
    asyncTearDown = editing.ModelIdentifierEditTests.asyncTearDown

    async def replace_model(self, **changes):
        body = {'source_model': 'vendor/old-model', 'target_model': 'backup',
                'revision': self.store.snapshot().revision, **changes}
        return await self.client.post('/api/v1/config/tasks/replace-model', json=body)

    async def test_all_routes_replaced_atomically_without_changing_models_or_parameters(self):
        before = self.store.snapshot()
        response = await self.replace_model()
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.store.snapshot()
        self.assertEqual(set(response.json()['data']['updated_tasks']), set(before.task_configs))
        self.assertEqual(saved.revision, before.revision + 1)
        self.assertEqual(saved.models, before.models)
        self.assertEqual(saved.providers, before.providers)
        for name, task in saved.task_configs.items():
            self.assertEqual(task.model_list, ['backup'])
            self.assertEqual(task.model_dump(exclude={'model_list'}), before.task_configs[name].model_dump(exclude={'model_list'}))

    async def test_replacement_keeps_order_and_unrelated_routes(self):
        value = self.store.snapshot()
        value.models.append(ModelConfig(model_identifier='third', name='第三个模型', api_provider='provider'))
        value.task_configs['work'].model_list = ['vendor/old-model', 'third', 'backup']
        value.task_configs['planner'].model_list = ['third', 'vendor/old-model', 'backup']
        value.task_configs['embedding'].model_list = []
        self.store.replace(value)
        response = await self.replace_model()
        self.assertEqual(response.status_code, 200)
        saved = self.store.snapshot()
        self.assertEqual(saved.task_configs['work'].model_list, ['backup', 'third'])
        self.assertEqual(saved.task_configs['planner'].model_list, ['third', 'backup'])
        self.assertEqual(saved.task_configs['embedding'].model_list, [])
        self.assertNotIn('embedding', response.json()['data']['updated_tasks'])

    async def test_invalid_and_stale_requests_never_partially_change_configuration(self):
        before = self.store.snapshot()
        for changes, status in [({'revision': before.revision - 1}, 409),
                                ({'target_model': 'missing'}, 404), ({'source_model': 'missing'}, 404),
                                ({'target_model': 'vendor/old-model'}, 422), ({'target_model': ''}, 422)]:
            with self.subTest(changes=changes):
                self.assertEqual((await self.replace_model(**changes)).status_code, status)
                self.assertEqual(self.store.snapshot(), before)

    async def test_repeating_completed_replacement_is_a_noop(self):
        await self.replace_model()
        before = self.store.snapshot()
        response = await self.replace_model()
        self.assertEqual(response.json()['data']['updated_count'], 0)
        self.assertEqual(self.store.snapshot(), before)

    async def test_owner_authorization_and_revision_are_required(self):
        app = FastAPI()
        app.state.runtime_configuration = self.store
        app.state.settings = SimpleNamespace(admin_token=SecretStr('owner-secret-token-12345'),
            admin_accounts=[AdminAccount(name='scoped', token='scoped-secret-token-12345', role='admin', groups=['10001'])])
        app.include_router(router)
        body = {'source_model': 'vendor/old-model', 'target_model': 'backup', 'revision': self.store.snapshot().revision}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            self.assertEqual((await client.post('/api/v1/config/tasks/replace-model', json=body)).status_code, 401)
            self.assertEqual((await client.post('/api/v1/config/tasks/replace-model', json=body,
                headers={'Authorization': 'Bearer scoped-secret-token-12345'})).status_code, 403)
        body.pop('revision')
        self.assertEqual((await self.client.post('/api/v1/config/tasks/replace-model', json=body)).status_code, 422)

    async def test_saved_replacement_survives_restart_and_next_model_call_uses_target(self):
        with tempfile.TemporaryDirectory() as directory:
            persistent = RuntimeConfigurationStore(directory, self.value)
            self.app.state.runtime_configuration = persistent
            self.assertEqual((await self.replace_model()).status_code, 200)
            reloaded = RuntimeConfigurationStore(directory, RuntimeConfiguration())
            self.assertEqual(reloaded.snapshot().task_configs['work'].model_list, ['backup'])
            provider = ConfiguredModelProvider(reloaded)
            seen = []

            async def handler(request):
                seen.append(json.loads(request.content)['model'])
                return httpx.Response(200, json={'choices': [{'message': {'content': '回答'}}]})

            await provider.client.aclose()
            provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                await provider.generate([{'role': 'user', 'content': 'test'}], 'reply')
                self.assertEqual(seen, ['backup'])
            finally:
                await provider.close()
