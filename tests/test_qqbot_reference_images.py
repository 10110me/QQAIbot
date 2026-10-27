import asyncio
import base64
import io
import json
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from osa.application.qqbot_media import prepare_reference_images
from osa.application.v3.thinking import ThinkingLoop
from osa.application.v3.tools import AgentTools
from osa.domain.entities import Destination, Event, GroupPolicy, ModelResult
from osa.domain.features import Record
from osa.infrastructure.features.qqbot import QqbotToolClient
from osa.infrastructure.features.repository import MemoryFeatureRepository
from osa.infrastructure.memory_store import InMemoryStore


def png():
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(output, "PNG")
    return output.getvalue()


def message(identity="source", actor="10000", text="这图片里的人太胖了，改瘦一点", group="10001"):
    return Event(identity, identity, "test", "message.created", Destination("group", group),
                 actor, identity, text, [], time.time())


class CatalogCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_refresh_fetches_initial_catalog_once(self):
        client=QqbotToolClient('http://qqbot',cache_seconds=86400)
        client._fetch_tools=AsyncMock(return_value=[{'tool':'drawing'}])
        task=asyncio.create_task(client.refresh_loop())
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        client._fetch_tools.assert_awaited_once()
        self.assertEqual((await client.cached_catalog())['tools'],[{'tool':'drawing'}])

    async def test_failed_catalog_fetch_is_backed_off_between_messages(self):
        client=QqbotToolClient('http://qqbot')
        client._fetch_tools=AsyncMock(side_effect=RuntimeError('offline'))
        self.assertIsNone(await client.catalog_for_prompt())
        self.assertIsNone(await client.catalog_for_prompt())
        client._fetch_tools.assert_awaited_once()

    async def test_missing_catalog_is_loaded_before_thinking(self):
        client=QqbotToolClient('http://qqbot')
        client._fetch_tools=AsyncMock(return_value=[{'tool':'drawing'}])
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
            '{"final_action":"SILENT"}','test','thinking')))
        tools=SimpleNamespace(features=SimpleNamespace(qqbot_tools=client))
        await ThinkingLoop(model,tools).run([],{'actor_id':'10000','source_id':'source'})
        self.assertIn('drawing',json.dumps(model.generate.await_args.args[0]))
        client._fetch_tools.assert_awaited_once()

    async def test_explicit_refresh_replaces_catalog_before_thinking(self):
        client=QqbotToolClient('http://qqbot')
        client._fetch_tools=AsyncMock(side_effect=[
            [{'tool':'old_tool'}],[{'tool':'new_tool'}]])
        await client.list_tools()
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
            '{"final_action":"SILENT"}','test','thinking')))
        tools=SimpleNamespace(features=SimpleNamespace(qqbot_tools=client))
        await ThinkingLoop(model,tools).run([],{'actor_id':'10000','source_id':'source',
            'bot_actions_requested':True,'source_event':{'text':'nova 刷新 qqbot 工具列表'}})
        prompt=json.dumps(model.generate.await_args.args[0])
        self.assertIn('new_tool',prompt)
        self.assertNotIn('old_tool',prompt)
        self.assertEqual(client._fetch_tools.await_count,2)

    async def test_repeated_and_concurrent_requests_only_fetch_once(self):
        client = QqbotToolClient("http://qqbot", repo=MemoryFeatureRepository())
        client._fetch_tools = AsyncMock(return_value=[{"tool": "interaction.image_generation"}])
        await asyncio.gather(*[client.list_tools() for index in range(8)])
        client._fetch_tools.assert_awaited_once()
        self.assertIsNotNone(await client.cached_catalog())

    async def test_persisted_catalog_survives_new_client_without_fetch(self):
        repo = MemoryFeatureRepository()
        original = QqbotToolClient("http://qqbot", repo=repo, cache_seconds=86400)
        original._fetch_tools = AsyncMock(return_value=[{"tool": "drawing"}])
        await original.list_tools()
        restored = QqbotToolClient("http://qqbot", repo=repo, cache_seconds=86400)
        restored._fetch_tools = AsyncMock()
        self.assertEqual(await restored.list_tools(), [{"tool": "drawing"}])
        restored._fetch_tools.assert_not_awaited()

    async def test_expired_or_forced_cache_fetches_again(self):
        client = QqbotToolClient("http://qqbot")
        client._fetch_tools = AsyncMock(return_value=[{"tool": "drawing"}])
        await client.list_tools()
        client._catalog["expires_at"] = time.time() - 1
        self.assertIsNone(await client.cached_catalog())
        await client.list_tools()
        await client.list_tools(force_refresh=True)
        self.assertEqual(client._fetch_tools.await_count, 3)

    async def test_endpoint_change_does_not_reuse_other_catalog(self):
        repo = MemoryFeatureRepository()
        original = QqbotToolClient("http://qqbot", repo=repo)
        original._fetch_tools = AsyncMock(return_value=[{"tool": "drawing"}])
        await original.list_tools()
        self.assertIsNone(await QqbotToolClient("http://another", repo=repo).cached_catalog())

    async def test_valid_cache_is_injected_into_thinking_without_discovery(self):
        client = QqbotToolClient("http://qqbot")
        client._fetch_tools = AsyncMock(return_value=[{"tool": "interaction.image_generation"}])
        await client.list_tools()
        model = SimpleNamespace(generate=AsyncMock(return_value=ModelResult('{"final_action":"SILENT"}', "test", "test")))
        tools = SimpleNamespace(features=SimpleNamespace(qqbot_tools=client))
        await ThinkingLoop(model, tools).run([], {"actor_id": "10000", "source_id": "source"})
        history = model.generate.await_args.args[0]
        self.assertIn("qqbot_cached_tools", json.dumps(history))
        self.assertIn("interaction.image_generation", json.dumps(history))
        client._fetch_tools.assert_awaited_once()


class ReferenceImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemoryStore()
        await self.store.initialize(["10001", "10002"])
        await self.store.update_policy("10001", GroupPolicy(media_read=True))
        self.event = message()
        await self.store.publish(self.event, True)
        self.raw = png()
        self.encoded = base64.b64encode(self.raw).decode()
        self.repo = MemoryFeatureRepository()
        self.images = SimpleNamespace(attachments=AsyncMock(return_value=[]))
        self.features = SimpleNamespace(store=self.store, repo=self.repo, images=self.images,
                                       objects=SimpleNamespace(get=AsyncMock(return_value=self.raw)))

    async def seed_generated(self, actor="10000", group="10001", expires=None):
        await self.repo.put(Record("asset", "media", actor, group, "active",
            {"object_key": "object", "mime": "image/png"}))
        await self.repo.put(Record("result", "qqbot_image_result", actor, group, "ready",
            {"asset_id": "asset", "source_id": "source", "expires_at": expires or time.time() + 86400}))

    async def test_current_and_quoted_original_bytes_are_forwarded(self):
        self.images.attachments.return_value = [{"mime_type": "image/png", "data_base64": self.encoded}]
        quoted = replace(self.event, reply_to_id="quoted-image")
        images, sources = await prepare_reference_images(self.features, quoted, {})
        self.assertEqual(base64.b64decode(images[0]["data_base64"]), self.raw)
        self.images.attachments.assert_awaited_once_with(quoted, [])

    async def test_followup_reuses_same_user_generated_image(self):
        await self.seed_generated()
        images, sources = await prepare_reference_images(self.features, self.event, {})
        self.assertEqual(base64.b64decode(images[0]["data_base64"]), self.raw)

    async def test_edit_without_original_never_calls_text_generation(self):
        with self.assertRaisesRegex(ValueError, "未调用文生图"):
            await prepare_reference_images(self.features, self.event, {})

    async def test_fat_cat_text_generation_does_not_require_reference(self):
        images, sources = await prepare_reference_images(self.features, replace(self.event, text="画一只胖猫"), {})
        self.assertEqual(images, [])

    async def test_other_group_or_user_image_is_not_implicitly_selected(self):
        await self.seed_generated(actor="20000", group="10002")
        with self.assertRaises(ValueError):
            await prepare_reference_images(self.features, self.event, {})

    async def test_unknown_source_id_is_rejected(self):
        with self.assertRaises(PermissionError):
            await prepare_reference_images(self.features, self.event, {"image_source_ids": ["hidden"]})
        self.images.attachments.assert_not_awaited()

    async def test_expired_generated_image_is_not_reused(self):
        await self.seed_generated(expires=time.time() - 1)
        with self.assertRaises(ValueError):
            await prepare_reference_images(self.features, self.event, {})

    async def test_gateway_upload_and_base64_result_roundtrip(self):
        self.images.attachments.return_value = [{"mime_type": "image/png", "data_base64": self.encoded}]
        client = QqbotToolClient("http://qqbot")
        client.list_tools = AsyncMock(return_value=[{"tool": "interaction.image_generation"}])
        client.call = AsyncMock(return_value={"ok": True, "actions": [{"type": "send_message_with_image",
            "image_base64": self.encoded, "message": "已完成"}]})
        self.features.qqbot_tools = client
        self.features.media = SimpleNamespace(save=AsyncMock(return_value=Record("new-asset", "media", "10000", "10001", "active")))
        async def registry(name, *args):
            if name == "thinking":
                return {"owner_id": "10000", "group_id": "10001"}
            if name == "receipt":
                return "key", None
        gateway = AgentTools(SimpleNamespace(call=registry), self.store, features=self.features)
        result = await gateway.execute("qqbot.tools.call", {"tool": "interaction.image_generation",
            "arguments": {"prompt": "改瘦一点", "use_reference_images": True}},
             {"actor_id": "10000", "group_id": "10001", "source_id": "source", "source_event": self.event.to_dict(),
              "bot_actions_requested": True,
             "session_id": "session", "tool_call_id": "edit", "deadline": time.time() + 60})
        self.assertNotIn("error", result)
        self.assertEqual(result["reference_image_count"], 1)
        self.assertEqual(client.call.await_args.kwargs["images"][0]["data_base64"], self.encoded)
        self.assertEqual(self.features.media.save.await_args.args[0], self.raw)
        self.assertEqual(result["asset_ids"], ["new-asset"])
        self.assertEqual(len(await self.repo.find("qqbot_image_result")), 1)
