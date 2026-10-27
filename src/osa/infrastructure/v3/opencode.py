"""Version-locked OpenCode HTTP adapter, with durable ambiguity markers.

The endpoint must be an operator-provided isolated OpenCode server. This module
does not install tools on the Social host or grant OpenCode database/QQ access.
"""
import asyncio
import hashlib
import json
import re
import time
from pathlib import PurePosixPath
from dataclasses import asdict
from urllib.parse import quote

import httpx

from osa.domain.agents import AgentCapabilities, ProviderStateUnknown, UnsupportedCapability
from osa.domain.work import WorkModelUnavailable, clean_text, validate_files

PINNED_OPENCODE_VERSION = "1.2.27"
PINNED_CODEX_VERSION = "0.147.0"


class OpenCodeAdapter:
    capabilities = AgentCapabilities(can_cancel=True)

    def __init__(self, registry, endpoint, password, version=PINNED_OPENCODE_VERSION, username="opencode", client=None, managed_gateway=False, provider="opencode"):
        supported = {"opencode": PINNED_OPENCODE_VERSION, "codex": PINNED_CODEX_VERSION}
        if not password or supported.get(provider) != version:
            raise ValueError("Configure authentication and the supported pinned OpenCode version")
        self.registry, self.endpoint, self.password, self.username = registry, endpoint, password, username
        self.version, self.client = version, client
        self.managed_gateway = managed_gateway
        self.provider = provider

    async def db(self, name, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.registry, name), *args, **kwargs)

    async def request(self, tid, method, path, **kwargs):
        task = await self.db("get", tid)
        # A gateway may route one isolated server per workspace. User text is
        # never accepted as URL/path/configuration input.
        endpoint = self.endpoint.replace("{workspace_id}", task["workspace_id"])
        if self.client:
            result = await self.client.request(method, endpoint.rstrip("/")+path,
                auth=httpx.BasicAuth(self.username, self.password), **kwargs)
        else:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                result = await client.request(method, endpoint.rstrip("/")+path,
                    auth=httpx.BasicAuth(self.username, self.password), **kwargs)
        result.raise_for_status()
        if len(result.content) > 2_000_000:
            raise ValueError("OpenCode response too large")
        return result.json() if result.content else None

    async def health(self, tid):
        # A fresh OpenCode home performs SQLite migrations before listening.
        # Only this idempotent probe is retried; never replay a prompt POST.
        from osa.domain.work import WorkModelUnavailable
        deadline = time.monotonic() + 180
        while True:
            try:
                result = await self.request(tid, "GET", "/global/health")
                break
            except httpx.HTTPError as exc:
                retryable = isinstance(exc, (httpx.ConnectError, httpx.TimeoutException))
                if isinstance(exc, httpx.HTTPStatusError):
                    try:
                        retryable = exc.response.status_code == 503 and exc.response.json().get('detail') == 'CODING_SERVER_STARTING'
                    except ValueError:
                        pass
                if not retryable or time.monotonic() >= deadline:
                    raise WorkModelUnavailable('OpenCode 工作空间启动或连接失败，请查看服务状态；未重复提交编程指令') from exc
                await asyncio.sleep(2)
        if not result.get("healthy") or result.get("version") != self.version:
            raise ValueError("OpenCode server version mismatch")

    async def submit(self, request):
        # request.idempotency_key is a local task ID, issued by Coordinator.
        tid = request.idempotency_key
        task = await self.db("get", tid)
        if task["owner_id"] != request.requester_id or task["group_id"] != request.source_group_id or task["workspace_id"] != request.workspace_id:
            raise PermissionError("OpenCode request scope mismatch")
        await self.health(tid)
        binding = await self.db("binding", tid)
        if binding and binding["provider"] != self.provider:
            raise ProviderStateUnknown("Existing task belongs to another coding provider; create a new task")
        if binding and binding["session_id"]:
            # Read reconciliation first. Never create another remote session.
            await self.request(tid, "GET", "/session/"+quote(binding["session_id"], safe=""))
            return tid
        title = "osa-"+tid
        if binding:
            sessions = await self.request(tid, "GET", "/session", params={"search": title, "limit": 100})
            matches = [s for s in sessions if s.get("title") == title]
            if len(matches) != 1:
                raise ProviderStateUnknown("Session creation outcome unknown; operator reconciliation required")
            sid = matches[0]["id"]
        else:
            await self.db("bind", tid, task["generation"], self.provider, None, "CREATING", {"title": title})
            try:
                session = await self.request(tid, "POST", "/session", json={"title": title,
                    "permission": [{"permission": "*", "pattern": "*", "action": "allow"}]})
                sid = session["id"]
            except (httpx.HTTPError, KeyError) as exc:
                raise ProviderStateUnknown("Remote session creation needs reconciliation") from exc
        await self.db("bind", tid, task["generation"], self.provider, sid, "READY", {"title": title, "plan_version": 0})
        return tid

    async def send_instruction(self, task_id, command):
        task = await self.db("get", task_id)
        binding = await self.db("binding", task_id)
        if not binding or not binding["session_id"]:
            raise ValueError("No provider session")
        command_id = str(command["command_id"])
        path = "/session/"+quote(binding["session_id"], safe="")
        previous = binding["payload"]
        if previous.get("command_id") == command_id:
            message_id = previous["message_id"]
            messages = await self.request(task_id, "GET", path+"/message", params={"limit": 100})
            if any(m.get("info", {}).get("id") == message_id for m in messages):
                return message_id
            raise ProviderStateUnknown("Prompt acceptance unknown; do not blindly resubmit")
        # Keep the official ascending-ID shape so message ordering is preserved.
        # Persist this generated ID before sending; retries use that exact value.
        digest = hashlib.sha256((task_id+":"+command_id).encode()).hexdigest()
        timestamp = (int(time.time()*1000)*4096+int(digest[-3:], 16)) & ((1 << 48)-1)
        message_id = "msg_"+f"{timestamp:012x}"+digest[:14]
        state = {**previous, "command_id": command_id, "message_id": message_id, "plan_version": task["plan_version"]}
        await self.db("bind", task_id, task["generation"], self.provider, binding["session_id"], "SUBMITTING", state)
        try:
            await self.request(task_id, "POST", path+"/prompt_async", json={"messageID": message_id,
                "parts": [{"type": "text", "text": str(command["text"])[:30000]}]})
        except httpx.HTTPError as exc:
            raise ProviderStateUnknown("Prompt acceptance unknown; reconcile by message ID") from exc
        await self.db("bind", task_id, task["generation"], self.provider, binding["session_id"], "RUNNING", state)
        return message_id

    async def get_status(self, task_id):
        binding = await self.db("binding", task_id)
        if not binding or not binding["session_id"]:
            return {"status": "UNKNOWN", "capabilities": asdict(self.capabilities)}
        await self.health(task_id)
        # Verify that the session still exists even if status omits idle sessions.
        await self.request(task_id, "GET", "/session/"+quote(binding["session_id"], safe=""))
        statuses = await self.request(task_id, "GET", "/session/status")
        state = statuses.get(binding["session_id"], {"type": "idle"}).get("type", "unknown")
        return {"status": state.upper(), "provider_session_id": binding["session_id"], "capabilities": asdict(self.capabilities),
                "note": "IDLE 仅表示代理停止，不表示项目已通过测试"}

    async def pause(self, task_id):
        raise UnsupportedCapability("OpenCode adapter has no native safe pause; Coordinator pauses at an idle boundary")

    async def resume(self, task_id):
        raise UnsupportedCapability("Coordinator restores the persisted session and submits the next instruction")

    async def cancel(self, task_id):
        binding = await self.db("binding", task_id)
        if binding and binding["session_id"]:
            await self.request(task_id, "POST", "/session/"+quote(binding["session_id"], safe="")+"/abort")

    async def stream_events(self, task_id):
        # Polling is deliberately advertised as supports_stream=False.
        yield {"type": "adapter.status", "task_id": task_id, **await self.get_status(task_id)}

    @staticmethod
    def provider_error(messages):
        for message in reversed(messages):
            info = message.get("info", {}) if isinstance(message, dict) else {}
            if info.get("error"):
                return clean_text(json.dumps(info["error"], ensure_ascii=False), 1200)
            for part in reversed(message.get("parts", [])):
                if not isinstance(part, dict) or part.get("type") != "tool":
                    continue
                state = part.get("state", {})
                if isinstance(state, dict) and str(state.get("status", "")).lower() in {"error", "failed"}:
                    detail = state.get("error") or state.get("message") or state.get("output") or state
                    return clean_text(json.dumps(detail, ensure_ascii=False) if isinstance(detail, (dict, list)) else str(detail), 1200)
        return ""

    async def project_files(self, task_id):
        """Read the isolated OpenCode project after the agent finishes.

        OpenCode edits the workspace directly through its own file tools. The
        old adapter incorrectly required the final assistant message to repeat
        every file as JSON, which made a successful coding session look like a
        protocol failure. Read the verified workspace instead and let the
        platform sandbox perform the authoritative tests.
        """
        async def nodes(path):
            value = await self.request(task_id, "GET", "/file", params={"path": path})
            return value if isinstance(value, list) else value.get("files", [])
        files = {}
        pending = ["."]
        visited = set()
        while pending:
            directory = pending.pop()
            if directory in visited:
                continue
            visited.add(directory)
            if len(visited) > 160:
                raise ValueError("OpenCode directory listing exceeds limit")
            for item in await nodes(directory):
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or item.get("name") or "").replace("\\", "/")
                kind = str(item.get("type") or item.get("kind") or "")
                if not path or path in {".", ".."} or path.startswith("/") or ".." in path.split("/"):
                    continue
                if any(part.startswith(".") or part == "__pycache__" for part in path.split("/")) or ":" in path:
                    continue
                if kind in {"directory", "dir", "folder"} or item.get("isDirectory"):
                    pending.append(path)
                    continue
                if PurePosixPath(path).suffix not in {".py", ".html", ".js", ".css", ".json", ".md", ".txt"}:
                    continue
                content = await self.request(task_id, "GET", "/file/content", params={"path": path})
                if isinstance(content, dict) and (content.get("type") == "binary" or content.get("encoding") == "base64"):
                    continue
                text = content.get("content") if isinstance(content, dict) else content
                if isinstance(text, str) and len(text.encode("utf-8")) <= 500_000:
                    files[path] = text
                    validate_files(files)
        if not files:
            raise ValueError("OpenCode completed without readable project files")
        return validate_files(files)

    async def artifacts(self, task_id):
        task = await self.db("get", task_id)
        return [{"id": task["artifact_id"], "verified_by": "platform_sandbox"}] if task.get("artifact_id") else []

    async def maintenance(self):
        for row in await self.db("pending_provider_deletions", self.provider):
            tid = row["task_id"]
            try:
                await self.cancel(tid)
                if row["session_id"]:
                    await self.request(tid, "DELETE", "/session/"+quote(row["session_id"], safe=""))
                if self.managed_gateway:
                    await self.request(tid, "DELETE", "/_workspace")
                await self.db("provider_deleted", tid)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    if self.managed_gateway:
                        await self.request(tid, "DELETE", "/_workspace")
                    await self.db("provider_deleted", tid)
            except httpx.HTTPError:
                pass
        for row in await self.db("pending_remote_controls", self.provider):
            try:
                if row["status"] == "CANCEL_REQUESTED":
                    await self.cancel(row["id"])
                if (await self.get_status(row["id"]))["status"] == "IDLE":
                    await self.db("confirm_remote_stop", row["id"], row["status"])
            except (httpx.HTTPError, ValueError):
                pass

    async def plan(self, task):
        try:
            return await self._plan(task)
        except httpx.HTTPError as exc:
            raise WorkModelUnavailable('OpenCode 服务连接或请求失败；任务未完成，保留现有会话以供恢复') from exc

    async def _plan(self, task):
        """WorkAgent planner: professional agent edits; platform independently tests."""
        from osa.domain.agents import CodingTaskRequest
        tid = task["id"]
        await self.submit(CodingTaskRequest(task.get("parent_task_id", tid), task["owner_id"], task["group_id"], task["workspace_id"], task["requirement"], [], tid))
        binding = await self.db("binding", tid)
        # Each plan/repair command is durable and distinguishable on recovery.
        signature = hashlib.sha256(json.dumps({"plan": task["plan_version"], "files": task.get("manifest_hash"), "error": task.get("blocker")}, sort_keys=True).encode()).hexdigest()
        prompt = json.dumps({"goal": task["requirement"], "changes": task["requirement_log"], "existing_files": task.get("files", {}), "test_failure": task.get("blocker"),
            "validation_environment": {'python':'3.12','packages':['numpy','matplotlib','Pillow','pygame==2.6.1'],
                'video_driver':'SDL dummy (headless)','audio_driver':'SDL dummy (headless)'},
            "immutable_tests": sorted(name for name in task.get('files', {}) if name.startswith('tests/')) if task.get('test_plan_version') == task['plan_version'] else [],
            "delivery_contract": 'Work directly in the isolated project and use any available coding tools, including shell commands, network access, package installation, and external paths inside this dedicated container, when needed. The container is isolated per workspace; never access another workspace, the host, Docker credentials, QQ credentials, or unrelated services. Do not return a JSON file bundle and do not paste the whole project into chat; the platform will read the files from the workspace after the session becomes idle. Include Python unittest tests under tests/test_*.py and tests/__init__.py. Maximum 80 files and 500KB. The platform independently runs tests in an offline Python 3.12 container with the validation_environment packages. Test gameplay logic and pygame rendering with SDL dummy drivers, not a real interactive display. Keep UI startup under an if __name__ == "__main__" guard so imports do not run the game loop. Once immutable_tests is nonempty, do not edit, delete, skip, or replace those tests during a repair; fix the implementation. Test-runner errors and missing environment dependencies must not be hidden by skipping tests. Do not claim to have executed platform tests unless you actually did so.'}, ensure_ascii=False)
        prompt += ('\nFor a calculation or hash request, implement the calculation and make a unittest print the actual computed result to stdout. '
                   'The platform will execute those tests and forward the real stdout to the coordinator. '
                   'Do not guess a hash or numerical result, and do not require sending a file for a calculation. '
                   'If a user requests a result file, have the program create it with a clear filename; tests must exercise it. '
                   'End with a short public summary of what you produced; never claim a platform test has passed before its result is available.')
        await self.send_instruction(tid, {"command_id": signature, "text": prompt})
        sid = binding["session_id"]
        last_progress = 0
        last_activity = None
        async with asyncio.timeout(3600):
            while True:
                live = await self.db("get", tid)
                if live["generation"] != task["generation"]:
                    raise ProviderStateUnknown("Worker fencing changed while OpenCode is running")
                if live["status"] == "CANCEL_REQUESTED":
                    await self.cancel(tid)
                    raise asyncio.CancelledError()
                state = await self.get_status(tid)
                if time.monotonic()-last_progress > 5:
                    messages = await self.request(tid, "GET", "/session/"+quote(sid, safe="")+"/message", params={"limit": 8})
                    tools = [p for m in messages for p in m.get("parts", []) if p.get("type") == "tool"]
                    latest = tools[-1] if tools else {}
                    tool_name = clean_text(str(latest.get("tool", "代理会话")), 100)
                    tool_state = clean_text(str(latest.get("state", {}).get("status", state["status"])), 100)
                    tool_input = latest.get("state", {}).get("input", {}) if isinstance(latest.get("state"), dict) else {}
                    raw_path = tool_input.get("filePath") or tool_input.get("path") or tool_input.get("command") or tool_input.get("pattern") or ""
                    detail = clean_text(str(raw_path), 180)
                    if tool_name in {"edit", "write", "apply_patch"}:
                        label = "编辑了文件"
                    elif tool_name in {"read", "list", "glob", "grep"}:
                        label = "读取了项目文件"
                    elif tool_name in {"shell", "execute", "bash"}:
                        label = "运行了命令"
                    elif tool_name == "代理会话" and state["status"] != "IDLE":
                        label = "模型正在推理，尚未调用工具"
                    else:
                        label = "调用了工具"
                    observed = (tool_name, tool_state, detail)
                    if observed != last_activity or time.monotonic()-last_progress > 30:
                        current = f"{label}：{detail}" if detail else label
                        old_activity = (await self.db("get", tid)).get("agent_activity") or []
                        activity = [*old_activity[-29:], {"label": label, "tool": tool_name, "detail": detail, "status": tool_state, "at": time.time()}]
                        await self.db("checkpoint", tid, task["generation"], "provider_observed", {"current_step": current, "agent_activity": activity},
                            {"type": "PROVIDER_OBSERVED", "provider_session_id": sid, "last_tool": tool_name, "tool_state": tool_state, "agent_activity": activity[-1], "reported_by_provider": True})
                        last_progress = time.monotonic()
                        last_activity = observed
                if state["status"] == "IDLE":
                    messages = await self.request(tid, "GET", "/session/"+quote(sid, safe="")+"/message", params={"limit": 100})
                    expected = (await self.db("binding", tid))["payload"]["message_id"]
                    answers = [m for m in messages if m.get("info", {}).get("role") == "assistant" and m["info"].get("parentID") == expected and m["info"].get("time", {}).get("completed")]
                    if answers:
                        final = answers[-1]
                        if final["info"].get("error"):
                            detail = self.provider_error(messages)
                            raise ValueError("OpenCode reported an error" + (": " + detail if detail else "; inspect provider session"))
                        if final["info"].get("finish") == "length" and not any(
                            part.get("type") == "tool" for message in messages
                            if message.get("info", {}).get("parentID") == expected
                            for part in message.get("parts", [])):
                            raise WorkModelUnavailable("编程模型已耗尽输出额度，但只进行了推理、没有调用文件工具；请提高模型输出上限或更换适合编程的模型")
                        try:
                            files = await self.project_files(tid)
                        except ValueError as exc:
                            detail = self.provider_error(messages)
                            if detail:
                                raise ValueError(f"{exc}: {detail}") from exc
                            raise
                        summary = '\n'.join(p.get('text','') for p in final.get('parts',[]) if p.get('type')=='text')
                        if summary:
                            previous_result = (await self.db('get',tid)).get('execution_result') or {}
                            await self.db('checkpoint',tid,task['generation'],'provider_summary',
                                {'execution_result':{**previous_result,'agent_summary':clean_text(summary,8000)}},
                                {'type':'PROVIDER_SUMMARY','provider_session_id':sid})
                        last = await self.db("binding", tid)
                        await self.db("bind", tid, task["generation"], self.provider, sid, "IDLE", last["payload"])
                        return files
                await asyncio.sleep(3)
