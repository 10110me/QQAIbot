import json
import tempfile
import unittest
from urllib.parse import quote

import httpx
from fastapi import FastAPI

from osa.infrastructure.configured_models import ConfiguredModelProvider
from osa.infrastructure.runtime_config import (
    TASK_NAMES, ModelConfig, ProviderConfig, RuntimeConfiguration, RuntimeConfigurationStore, TaskConfig,
)
from osa.interfaces.api import require_admin
from osa.interfaces.configuration import router


class ModelIdentifierEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.value = RuntimeConfiguration(
            providers=[ProviderConfig(id="provider", name="Provider", base_url="https://example.com/v1")],
            models=[ModelConfig(model_identifier="vendor/old-model", name="显示名称", api_provider="provider",
                                capabilities=["chat", "image"], force_stream_mode=False),
                    ModelConfig(model_identifier="backup", name="备用模型", api_provider="provider")],
            task_configs={name: TaskConfig(model_list=["backup", "vendor/old-model"], max_tokens=1200,
                                           selection_strategy="sequential") for name in TASK_NAMES})
        self.store = RuntimeConfigurationStore("unused", self.value, memory_only=True)
        self.app = FastAPI()
        self.app.state.runtime_configuration = self.store
        self.app.dependency_overrides[require_admin] = lambda: None
        self.app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def edit(self, old="vendor/old-model", new="vendor/new-model", **changes):
        model = self.store.snapshot().models[0].model_dump(mode="json")
        model.update(model_identifier=new, revision=self.store.snapshot().revision, **changes)
        return await self.client.put("/api/v1/config/models/" + quote(old, safe=""), json=model)

    async def test_renaming_updates_all_existing_routes_without_duplicate_model(self):
        response = await self.edit()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["model_identifier"], "vendor/new-model")
        saved = self.store.snapshot()
        self.assertEqual([model.model_identifier for model in saved.models], ["vendor/new-model", "backup"])
        self.assertEqual(saved.models[0].name, "显示名称")
        self.assertEqual(saved.models[0].capabilities, ["chat", "image"])
        for name, task in saved.task_configs.items():
            with self.subTest(task=name):
                self.assertEqual(task.model_list, ["backup", "vendor/new-model"])
                self.assertEqual(task.max_tokens, 1200)
                self.assertEqual(task.selection_strategy, "sequential")

    async def test_display_name_change_does_not_change_request_id(self):
        response = await self.edit(new="vendor/old-model", name="新的显示名称", max_retry=5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.snapshot().models[0].name, "新的显示名称")
        self.assertEqual(self.store.snapshot().models[0].max_retry, 5)
        self.assertEqual(self.store.snapshot().task_configs["work"].model_list, ["backup", "vendor/old-model"])

    async def test_collision_does_not_overwrite_existing_model_or_routes(self):
        previous = self.store.snapshot()
        response = await self.edit(new="backup")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "MODEL_IDENTIFIER_EXISTS")
        self.assertEqual(self.store.snapshot(), previous)

    async def test_stale_revision_does_not_partially_rename(self):
        previous = self.store.snapshot()
        body = previous.models[0].model_dump(mode="json")
        body.update(model_identifier="new-id", revision=previous.revision - 1)
        response = await self.client.put("/api/v1/config/models/vendor%2Fold-model", json=body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.snapshot(), previous)

    async def test_missing_original_model_cannot_create_a_rename(self):
        previous = self.store.snapshot()
        self.assertEqual((await self.edit(old="missing-model")).status_code, 404)
        self.assertEqual(self.store.snapshot(), previous)

    async def test_new_model_creation_still_works(self):
        response = await self.edit(old="new-model", new="new-model")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.store.snapshot().models), 3)
        self.assertEqual(self.store.snapshot().task_configs["replyer"].model_list, ["backup", "vendor/old-model"])

    async def test_invalid_provider_or_empty_id_does_not_change_configuration(self):
        previous = self.store.snapshot()
        self.assertEqual((await self.edit(api_provider="missing")).status_code, 422)
        self.assertEqual((await self.edit(new="")).status_code, 422)
        self.assertEqual(self.store.snapshot(), previous)

    async def test_live_provider_uses_new_request_id_on_next_request(self):
        value = self.store.snapshot()
        value.task_configs["replyer"].model_list = ["vendor/old-model"]
        self.store.replace(value)
        provider = ConfiguredModelProvider(self.store)
        seen = []
        async def handler(request):
            seen.append(json.loads(request.content)["model"])
            return httpx.Response(200, json={"choices": [{"message": {"content": "回答"}}]})
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await provider.generate([{"role": "user", "content": "test"}], "reply")
            self.assertEqual((await self.edit(name="新的显示名称")).status_code, 200)
            await provider.generate([{"role": "user", "content": "test"}], "reply")
            self.assertEqual(seen, ["vendor/old-model", "vendor/new-model"])
        finally:
            await provider.close()

    async def test_rename_survives_configuration_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            persistent = RuntimeConfigurationStore(folder, self.value)
            self.app.state.runtime_configuration = persistent
            response = await self.edit(max_retry=4)
            self.assertEqual(response.status_code, 200)
            reloaded = RuntimeConfigurationStore(folder, RuntimeConfiguration()).snapshot()
            self.assertEqual(reloaded.models[0].model_identifier, "vendor/new-model")
            self.assertEqual(reloaded.models[0].max_retry, 4)
            self.assertEqual(reloaded.task_configs["work"].model_list, ["backup", "vendor/new-model"])
