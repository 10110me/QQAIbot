import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from osa.infrastructure.v3.codex import CodexAdapter
from osa.infrastructure.v3.registry import Registry


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway_module = load_module("codex_gateway_test", ROOT / "workers/opencode/service.py")
server_module = load_module("codex_runner_test", ROOT / "workers/codex/server.py")


class CodexGatewayTests(unittest.IsolatedAsyncioTestCase):
    def test_model_config_uses_responses_and_environment_key(self):
        config = tomllib.loads(gateway_module.codex_config(
            "https://example.invalid/v1", "synthetic-model", {"X-Test": "yes"}))
        self.assertEqual(config["model"], "synthetic-model")
        self.assertEqual(config["model_providers"]["osa"]["wire_api"], "responses")
        self.assertEqual(config["model_providers"]["osa"]["env_key"], "CODEX_API_KEY")
        self.assertEqual(config["model_providers"]["osa"]["http_headers"], {"X-Test": "yes"})

    def test_chat_model_uses_local_responses_bridge(self):
        config = tomllib.loads(gateway_module.codex_config(
            "http://127.0.0.1:4096/v1", "z-ai/glm-5.3", chat_bridge=True))
        self.assertTrue(config["disable_response_storage"])
        self.assertEqual(config["model_providers"]["osa"]["base_url"], "http://127.0.0.1:4096/v1")

    async def test_gateway_uses_distinct_containers_and_codex_runner(self):
        with patch.dict(os.environ, {"OSA_CODING_BACKEND": "codex", "OSA_OPENCODE_SCOPE": "test",
            "OSA_OPENCODE_NETWORK": "test_net", "OSA_OPENCODE_PASSWORD": "test-password-not-a-real-secret",
            "OSA_CODEX_API_KEY": "synthetic-key", "OSA_CODEX_BASE_URL": "https://example.invalid/v1",
            "OSA_CODEX_MODEL": "synthetic-model"}):
            gateway = gateway_module.Gateway()
            name = gateway_module.workspace_name("test", "a" * 32, "codex")
            self.assertTrue(name.startswith("osa-cx-"))
            self.assertNotEqual(name, gateway_module.workspace_name("test", "a" * 32))
            gateway.docker = AsyncMock(side_effect=lambda *args, **kwargs: None if args[0] == "inspect" else b"")
            gateway.create = AsyncMock()
            await gateway.ensure("a" * 32)
            environment = gateway.create.await_args.args[1]
            self.assertIn('wire_api = "responses"', environment["OSA_CODEX_CONFIG_CONTENT"])
            self.assertEqual(environment["CODEX_API_KEY"], "synthetic-key")
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", environment)

    async def test_direct_key_requires_explicit_model(self):
        with patch.dict(os.environ, {"OSA_CODING_BACKEND": "codex", "OSA_OPENCODE_SCOPE": "test",
            "OSA_OPENCODE_NETWORK": "test_net", "OSA_OPENCODE_PASSWORD": "test-password-not-a-real-secret",
            "OSA_CODEX_API_KEY": "synthetic-key", "OSA_CODEX_MODEL": ""}):
            gateway = gateway_module.Gateway()
            gateway.docker = AsyncMock(side_effect=lambda *args, **kwargs: None if args[0] == "inspect" else b"")
            with self.assertRaises(HTTPException) as error:
                await gateway.ensure("a" * 32)
            self.assertEqual(error.exception.status_code, 409)

    async def test_codex_gateway_does_not_retire_opencode_containers(self):
        with patch.dict(os.environ, {"OSA_CODING_BACKEND": "codex", "OSA_OPENCODE_SCOPE": "test",
            "OSA_OPENCODE_NETWORK": "test_net", "OSA_OPENCODE_PASSWORD": "test-password-not-a-real-secret",
            "OSA_CODEX_API_KEY": "synthetic-key", "OSA_CODEX_MODEL": "synthetic-model"}):
            gateway = gateway_module.Gateway()
            old_name = gateway_module.workspace_name("test", "b" * 32, "opencode")
            gateway.docker = AsyncMock(side_effect=lambda *args, **kwargs:
                None if args[0] == "inspect" else old_name.encode() if args[0] == "ps" else b"")
            gateway.sessions_idle = AsyncMock()
            gateway.retire = AsyncMock()
            gateway.create = AsyncMock()
            await gateway.ensure("a" * 32)
            gateway.sessions_idle.assert_not_awaited()
            gateway.retire.assert_not_awaited()

    async def test_adapter_binds_codex_without_reusing_opencode_session(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Registry(str(Path(folder) / "registry.db"))
            task = registry.create("owner", None, "synthetic", "codex-test")
            registry.command(task["id"], "admin", None, "APPROVE", {}, 1, "approve", admin=True)
            task = registry.claim("worker")
            from osa.domain.agents import CodingTaskRequest, ProviderStateUnknown
            request = CodingTaskRequest(task["id"], "owner", None, task["workspace_id"],
                "synthetic", [], task["id"])
            def handle(http_request):
                if http_request.url.path == "/global/health":
                    return httpx.Response(200, json={"healthy": True, "version": "0.147.0"})
                if http_request.method == "POST" and http_request.url.path == "/session":
                    return httpx.Response(200, json={"id": "ses_test"})
                return httpx.Response(200, json={"id": "ses_test"})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                adapter = CodexAdapter(registry, "http://codex", "secret", client=client)
                await adapter.submit(request)
                self.assertEqual(registry.binding(task["id"])["provider"], "codex")
                with registry.tx() as database:
                    database.execute("UPDATE coding_bindings SET provider='opencode' WHERE task_id=?", (task["id"],))
                with self.assertRaises(ProviderStateUnknown):
                    await adapter.submit(request)


class FakeStdin:
    def write(self, data):
        self.data = data

    async def drain(self):
        return None

    def close(self):
        return None


class FakeProcess:
    def __init__(self):
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = 0
        for event in (
            {"type": "thread.started", "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53"},
            {"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "pwd"}},
            {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "pwd", "status": "completed"}},
            {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "Done"}},
            {"type": "turn.completed"},
        ):
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        return 0


class CodexRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_bridge_requires_bearer_and_keeps_selected_model(self):
        class FakeResponse:
            def model_dump(self, mode):
                return {"id": "resp_test", "model": "z-ai/glm-5.3", "output": []}

        call = AsyncMock(return_value=FakeResponse())
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "CODEX_SERVER_PASSWORD": "synthetic-password", "CODEX_SERVER_USERNAME": "codex",
            "CODEX_API_KEY": "synthetic-key", "OSA_CODEX_CHAT_BRIDGE": "true",
            "OSA_CODEX_MODEL": "z-ai/glm-5.3", "OSA_CODEX_UPSTREAM_BASE_URL": "https://example.invalid/v1",
            "OSA_CODEX_UPSTREAM_HEADERS": '{"X-Test":"yes"}'}), \
            patch.dict(sys.modules, {"litellm": SimpleNamespace(aresponses=call)}):
            home = Path(folder) / "home"
            project = Path(folder) / "project"
            project.mkdir()
            with patch.object(server_module, "HOME", home), patch.object(server_module, "PROJECT", project), \
                    patch.object(server_module, "STATE_FILE", home / "osa-state.json"):
                app = server_module.create_app()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    body = {"model": "z-ai/glm-5.3", "input": "test", "stream": False}
                    self.assertEqual((await client.post("/v1/responses", json=body)).status_code, 401)
                    result = await client.post("/v1/responses", json=body,
                        headers={"Authorization": "Bearer synthetic-key"})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json()["id"], "resp_test")
                    self.assertEqual(call.await_args.kwargs["model"], "custom_openai/z-ai/glm-5.3")
                    self.assertEqual(call.await_args.kwargs["api_base"], "https://example.invalid/v1")
                    self.assertEqual(call.await_args.kwargs["extra_headers"], {"X-Test": "yes"})

    async def test_bridge_tracks_tools_result_and_idempotent_prompt(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "CODEX_SERVER_PASSWORD": "synthetic-password", "CODEX_SERVER_USERNAME": "codex",
            "OSA_CODEX_CONFIG_CONTENT": 'model = "synthetic"\n'}):
            home = Path(folder) / "home"
            project = Path(folder) / "project"
            project.mkdir()
            with patch.object(server_module, "HOME", home), patch.object(server_module, "PROJECT", project), \
                    patch.object(server_module, "STATE_FILE", home / "osa-state.json"):
                app = server_module.create_app()
                auth = httpx.BasicAuth("codex", "synthetic-password")
                transport = httpx.ASGITransport(app=app)
                fake = FakeProcess()
                with patch.object(server_module.asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=fake):
                    async with httpx.AsyncClient(transport=transport, base_url="http://test", auth=auth) as client:
                        self.assertEqual((await client.get("/global/health")).json()["version"], "0.147.0")
                        session_id = (await client.post("/session", json={"title": "osa-test"})).json()["id"]
                        body = {"messageID": "msg_first", "parts": [{"type": "text", "text": "create a file"}]}
                        self.assertEqual((await client.post(f"/session/{session_id}/prompt_async", json=body)).status_code, 200)
                        for _ in range(100):
                            status = (await client.get("/session/status")).json()[session_id]["type"]
                            if status == "idle":
                                break
                            await asyncio.sleep(.01)
                        self.assertEqual(status, "idle")
                        messages = (await client.get(f"/session/{session_id}/message")).json()
                        self.assertEqual(messages[-1]["parts"][-1]["text"], "Done")
                        self.assertEqual(messages[-1]["parts"][0]["tool"], "bash")
                        self.assertEqual((await client.post(f"/session/{session_id}/prompt_async", json=body)).status_code, 200)
                        self.assertEqual(len((await client.get(f"/session/{session_id}/message")).json()), 2)
                        self.assertEqual((await client.get("/session", params={"search": "osa-test"})).json()[0]["id"], session_id)

    async def test_bridge_rejects_concurrent_sessions(self):
        release = asyncio.Event()
        async def paused_run(*_args):
            await release.wait()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "CODEX_SERVER_PASSWORD": "synthetic-password", "CODEX_SERVER_USERNAME": "codex"}):
            home = Path(folder) / "home"
            project = Path(folder) / "project"
            project.mkdir()
            with patch.object(server_module, "HOME", home), patch.object(server_module, "PROJECT", project), \
                    patch.object(server_module, "STATE_FILE", home / "osa-state.json"), \
                    patch.object(server_module.Workspace, "run", paused_run):
                app = server_module.create_app()
                auth = httpx.BasicAuth("codex", "synthetic-password")
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", auth=auth) as client:
                    first = (await client.post("/session", json={"title": "first"})).json()["id"]
                    second = (await client.post("/session", json={"title": "second"})).json()["id"]
                    response = await client.post(f"/session/{first}/prompt_async",
                        json={"messageID": "msg_first", "parts": [{"type": "text", "text": "work"}]})
                    self.assertEqual(response.status_code, 200)
                    response = await client.post(f"/session/{second}/prompt_async",
                        json={"messageID": "msg_test", "parts": [{"type": "text", "text": "work"}]})
                    self.assertEqual(response.status_code, 409)
                    release.set()
                    await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
