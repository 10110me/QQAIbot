import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

from osa.config import AdminAccount
from osa.domain.entities import UpstreamUnavailable
from osa.infrastructure.onebot import OneBotConnection
from osa.main import create_app
from tests.test_core import demo_settings


class ConsoleGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_names_share_one_request_and_survive_disconnect(self):
        platform = OneBotConnection()
        platform.websocket = object()
        platform.call = AsyncMock(return_value=[
            {"group_id": 10001, "group_name": " 测试交流群 "},
            {"group_id": 10002, "group_name": None},
            None,
        ])
        results = await asyncio.gather(*(platform.group_names() for _ in range(4)))
        self.assertEqual(results, [{"10001": "测试交流群"}] * 4)
        platform.call.assert_awaited_once_with("get_group_list", {})
        results[0].clear()
        platform.websocket = None
        self.assertEqual(await platform.group_names(), {"10001": "测试交流群"})

    async def test_failed_refresh_keeps_cached_names_and_backs_off(self):
        platform = OneBotConnection()
        platform.websocket = object()
        platform._group_names = {"10001": "旧群名"}
        for error in [UpstreamUnavailable("offline"), TimeoutError()]:
            platform._group_names_checked = 0
            platform.call = AsyncMock(side_effect=error)
            self.assertEqual(await platform.group_names(), {"10001": "旧群名"})
            self.assertEqual(await platform.group_names(), {"10001": "旧群名"})
            platform.call.assert_awaited_once()

    async def test_group_names_respect_allowlist_and_account_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = demo_settings().model_copy(update={
                "data_dir": directory, "v2_enabled": False, "v3_enabled": False,
                "allowed_groups": ["10001", "10002"],
                "admin_accounts": [AdminAccount(name="viewer", token="viewer-test-token",
                                                 role="viewer", groups=["10001"])],
            })
            app = create_app(settings)
            store = app.state.store
            await store.initialize(["10001", "10002", "10003"])
            app.state.platform._group_names = {
                "10001": "开发群", "10002": "闲聊群", "10003": "未授权的群",
            }
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                            base_url="http://test") as client:
                    self.assertEqual((await client.get("/api/v1/groups")).status_code, 401)
                    owner = await client.get("/api/v1/groups", headers={
                        "Authorization": "Bearer admin-test-token-12345"})
                    self.assertEqual({r["id"]: r["name"] for r in owner.json()["data"]},
                                     {"10001": "开发群", "10002": "闲聊群"})
                    viewer = await client.get("/api/v1/groups", headers={
                        "Authorization": "Bearer viewer-test-token"})
                    self.assertEqual([(r["id"], r["name"]) for r in viewer.json()["data"]],
                                     [("10001", "开发群")])
                    original_groups=store.groups
                    store.groups=AsyncMock(return_value=[{'id':'10001','settings':{'enabled':True}}])
                    compatible=await client.get('/api/v1/groups',headers={
                        'Authorization':'Bearer viewer-test-token'})
                    self.assertEqual(compatible.json()['data'][0]['settings']['talk_frequency'],.5)
                    self.assertEqual(compatible.json()['data'][0]['settings']['batch_max_wait_seconds'],8)
                    store.groups=original_groups
                    app.state.platform._group_names.clear()
                    fallback = await client.get("/api/v1/groups", headers={
                        "Authorization": "Bearer viewer-test-token"})
                    self.assertEqual(fallback.json()["data"][0]["name"], "")
            finally:
                await store.close()
