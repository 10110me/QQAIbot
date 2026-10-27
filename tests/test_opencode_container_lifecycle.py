import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


path = Path(os.environ.get("GATEWAY_SERVICE_FILE", Path(__file__).resolve().parents[1] / "workers/opencode/service.py"))
spec = importlib.util.spec_from_file_location("gateway_lifecycle", path)
gateway_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway_module)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"OSA_OPENCODE_SCOPE": "test", "OSA_OPENCODE_NETWORK": "test_net",
            "OSA_OPENCODE_PASSWORD": "test-password-not-a-real-secret", "OSA_OPENCODE_MODEL_CONFIG_URL": "",
            "OSA_WORK_MODEL_API_KEY": "synthetic-key", "OSA_OPENCODE_BACKUP_VOLUME": "test_appdata"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.gateway = gateway_module.Gateway()
        self.name = gateway_module.workspace_name("test", "a" * 32)
        self.other = gateway_module.workspace_name("test", "b" * 32)
        self.gateway.docker = AsyncMock(return_value=b"")
        self.gateway.sessions_idle = AsyncMock(return_value=True)
        self.gateway.accessed[self.name] = 0

    def test_coding_model_has_room_for_reasoning_and_tool_calls(self):
        config = gateway_module.coding_config('https://example.invalid', 'synthetic-model', 'synthetic-key')
        self.assertEqual(json.loads(config)['provider']['osa']['models']['synthetic-model']['limit']['output'], 32768)

    async def cleanup(self, state):
        self.gateway.docker.side_effect = lambda *args, **kwargs: (self.name + "|" + state).encode() if args[0] == "ps" else b""
        with patch.object(gateway_module.time, "monotonic", return_value=500):
            await self.gateway.cleanup_once()

    def removed(self):
        return [call.args for call in self.gateway.docker.await_args_list if call.args[0] == "rm"]

    async def test_stopped_container_is_removed_without_removing_volumes(self):
        await self.cleanup("exited")
        self.assertEqual(self.removed(), [("rm", self.name)])
        self.assertFalse(any("volume" in call.args or "--force" in call.args or "-v" in call.args for call in self.gateway.docker.await_args_list))
        self.assertNotIn(self.name, self.gateway.accessed)

    async def test_idle_running_container_is_stopped_and_removed(self):
        await self.cleanup("running")
        self.gateway.sessions_idle.assert_awaited_once_with(self.name)
        self.gateway.docker.assert_any_await("stop", "--time", "10", self.name)
        self.assertEqual(self.removed(), [("rm", self.name)])

    async def test_busy_session_is_never_removed(self):
        self.gateway.sessions_idle.return_value = False
        await self.cleanup("running")
        self.assertEqual(self.removed(), [])

    async def test_unknown_session_status_is_never_removed(self):
        self.gateway.sessions_idle.side_effect = httpx.ConnectError("offline")
        await self.cleanup("running")
        self.assertEqual(self.removed(), [])

    async def test_recently_accessed_running_container_is_not_probed_or_removed(self):
        self.gateway.accessed[self.name] = 499
        await self.cleanup("running")
        self.gateway.sessions_idle.assert_not_awaited()
        self.assertEqual(self.removed(), [])

    async def test_fresh_created_container_has_grace_period(self):
        self.gateway.accessed.clear()
        await self.cleanup("created")
        self.assertEqual(self.removed(), [])

    async def test_abandoned_created_container_is_removed(self):
        await self.cleanup("created")
        self.assertEqual(self.removed(), [("rm", self.name)])

    async def test_other_project_and_paused_container_are_not_removed(self):
        foreign = gateway_module.workspace_name("different-project", "a" * 32)
        self.gateway.docker.return_value = (foreign + "|exited\n" + self.name + "|paused").encode()
        await self.gateway.cleanup_once()
        self.assertEqual(self.removed(), [])

    async def test_retire_rejects_foreign_name_before_docker(self):
        with self.assertRaises(ValueError):
            await self.gateway.retire(gateway_module.workspace_name("foreign", "a" * 32), running=True)
        self.gateway.docker.assert_not_awaited()

    async def test_cleanup_failure_does_not_skip_other_containers(self):
        def docker(*args, **kwargs):
            if args[0] == "ps":
                return (self.name + "|exited\n" + self.other + "|exited").encode()
            if args == ("rm", self.name):
                raise RuntimeError("cannot remove")
            return b""
        self.gateway.docker.side_effect = docker
        await self.gateway.cleanup_once()
        self.gateway.docker.assert_any_await("rm", self.other)

    async def test_ensure_recreates_stopped_container_with_same_workspace_name(self):
        self.gateway.docker.side_effect = lambda *args, **kwargs: b"false" if args[0] == "inspect" else b""
        self.gateway.create = AsyncMock()
        self.assertEqual(await self.gateway.ensure("a" * 32), self.name)
        self.assertEqual(self.removed(), [("rm", self.name)])
        self.assertEqual(self.gateway.create.await_args.args[0], self.name)
        self.gateway.docker.assert_any_await("start", self.name)

    async def test_ensure_missing_container_creates_same_workspace_and_preserves_session_volume_name(self):
        self.gateway.docker.side_effect = lambda *args, **kwargs: None if args[0] == "inspect" else b""
        process = AsyncMock()
        process.returncode = 0
        with patch.object(gateway_module.asyncio, "create_subprocess_exec", return_value=process) as create:
            await self.gateway.ensure("a" * 32)
        args = create.call_args.args
        self.assertIn("type=volume,source=" + self.name + "-code,target=/project", args)
        self.assertIn("type=volume,source=" + self.name + "-home,target=/home/agent", args)

    async def test_switching_workspace_removes_idle_previous_container(self):
        self.gateway.docker.side_effect = lambda *args, **kwargs: self.other.encode() if args[0] == "ps" else b"true"
        await self.gateway.ensure("a" * 32)
        self.gateway.docker.assert_any_await("stop", "--time", "10", self.other)
        self.gateway.docker.assert_any_await("rm", self.other)

    async def test_switching_workspace_does_not_interrupt_busy_previous_container(self):
        self.gateway.docker.return_value = self.other.encode()
        self.gateway.sessions_idle.return_value = False
        with self.assertRaises(gateway_module.HTTPException) as raised:
            await self.gateway.ensure("a" * 32)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.removed(), [])

    async def test_backup_discovers_persistent_volumes_when_no_containers_exist(self):
        self.gateway.stop_children = AsyncMock()
        foreign = gateway_module.workspace_name("foreign", "b" * 32)
        volumes = [self.name + "-home", self.name + "-code", foreign + "-code", self.name + "-invalid"]
        self.gateway.docker.side_effect = lambda *args, **kwargs: "\n".join(volumes).encode() if args[0] == "volume" else b""
        await self.gateway.backup()
        archives = [call for call in self.gateway.docker.await_args_list if call.args[0] == "run"]
        self.assertEqual(len(archives), 2)
        for index, suffix in enumerate(("home", "code")):
            self.assertIn("type=volume,source=" + self.name + "-" + suffix + ",target=/source,readonly", archives[index].args)

    async def test_shutdown_removes_containers_but_not_volumes(self):
        self.gateway.docker.side_effect = lambda *args, **kwargs: (self.name + "|running\n" + self.other + "|exited").encode() if args[0] == "ps" else b""
        await self.gateway.stop_children()
        self.assertEqual(self.removed(), [("rm", self.name), ("rm", self.other)])

    async def test_real_status_parser_rejects_malformed_or_busy_response(self):
        async def handler(request):
            return httpx.Response(200, json={"ses_test": {"type": "busy"}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(gateway_module.httpx, "AsyncClient", return_value=client):
            self.assertFalse(await gateway_module.Gateway.sessions_idle(self.gateway, self.name))


if __name__ == "__main__":
    unittest.main()
