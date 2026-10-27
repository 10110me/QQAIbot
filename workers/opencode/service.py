"""Trusted Docker gateway. Each workspace gets its own process, home and code volume.

Only this controller owns the socket. Coding containers never receive it, the
QQ token, admin token, browser profile, registry or other project volumes.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

logger = logging.getLogger(__name__)


def workspace_name(scope, workspace, backend="opencode"):
    if not re.fullmatch(r"[a-f0-9]{32}", workspace):
        raise ValueError("Invalid workspace")
    prefix = "osa-cx-" if backend == "codex" else "osa-oc-"
    return prefix+hashlib.sha256(scope.encode()).hexdigest()[:8]+"-"+workspace


def coding_config(base_url, model, api_key, headers=None, model_options=None):
    return json.dumps({"model": "osa/"+model, "share": "disabled", "autoupdate": False,
        "lsp": False, "mcp": {}, "plugin": [],
        "permission": {"*": "allow"},
        "provider": {"osa": {"npm": "@ai-sdk/openai-compatible", "name": "Dedicated coding provider",
            "options": {"baseURL": base_url, "apiKey": api_key, "headers": headers or {}}, "models": {model: {"name": model, "options": model_options or {}, "limit": {"context": 128000, "output": 32768}}}}}}, separators=(",", ":"))


def codex_config(base_url, model, headers=None, chat_bridge=False):
    lines = ["model = " + json.dumps(model), 'model_provider = "osa"',
             *(['disable_response_storage = true'] if chat_bridge else []),
             '[model_providers.osa]', 'name = "Dedicated coding provider"',
             'base_url = ' + json.dumps(base_url), 'env_key = "CODEX_API_KEY"',
             'wire_api = "responses"', 'requires_openai_auth = false',
             'supports_websockets = false']
    if headers:
        values = ', '.join(json.dumps(str(key)) + ' = ' + json.dumps(str(value)) for key, value in headers.items())
        lines.append('http_headers = {' + values + '}')
    return '\n'.join(lines) + '\n'


class Gateway:
    def __init__(self):
        self.backend = os.environ.get("OSA_CODING_BACKEND", "opencode")
        if self.backend not in {"opencode", "codex"}:
            raise RuntimeError("Invalid coding backend")
        self.scope = os.environ.get("OSA_OPENCODE_SCOPE", "osa")
        self.network = os.environ.get("OSA_OPENCODE_NETWORK", "")
        self.password = os.environ.get("OSA_OPENCODE_PASSWORD", "")
        self.username = os.environ.get("OSA_OPENCODE_USERNAME", "opencode")
        if not self.network or len(self.password) < 24:
            raise RuntimeError("OpenCode gateway credentials/network not configured")
        self.accessed = {}
        self.lock = asyncio.Lock()

    @property
    def prefix(self):
        return ("osa-cx-" if self.backend == "codex" else "osa-oc-") + hashlib.sha256(self.scope.encode()).hexdigest()[:8] + "-"

    def owns(self, name):
        return bool(re.fullmatch(re.escape(self.prefix) + r"[a-f0-9]{32}", name))

    async def sessions_idle(self, name):
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get("http://" + name + ":4096/session/status", auth=self.child_auth(name))
            response.raise_for_status()
            statuses = response.json()
        if not isinstance(statuses, dict) or any(not isinstance(value, dict) or not value.get("type") for value in statuses.values()):
            raise ValueError("Invalid workspace session status")
        return all(value["type"] == "idle" for value in statuses.values())

    async def retire(self, name, running=False):
        if not self.owns(name):
            raise ValueError("Workspace does not belong to this gateway")
        if running:
            await self.docker("stop", "--time", "10", name)
        await self.docker("rm", name)
        self.accessed.pop(name, None)
        logger.info("workspace_container_removed name=%s volumes_preserved=true", name)

    async def cleanup_once(self):
        async with self.lock:
            rows = await self.docker("ps", "-a", "--filter", "label=osa.opencode.scope=" + self.scope,
                                     "--format", "{{.Names}}|{{.State}}")
            for row in rows.decode().splitlines():
                name, state = row.split("|", 1)
                if not self.owns(name):
                    continue
                try:
                    if state in {"exited", "dead"}:
                        await self.retire(name)
                    elif time.monotonic() - self.accessed.setdefault(name, time.monotonic()) > 180:
                        if state == "created":
                            await self.retire(name)
                        elif state == "running" and await self.sessions_idle(name):
                            await self.retire(name, running=True)
                except Exception as error:
                    logger.warning("workspace_cleanup_deferred name=%s error_type=%s", name, type(error).__name__)

    def child_auth(self, name):
        # An exploited coding container must not inherit the gateway credential
        # or another project's server password.
        password = hmac.new(self.password.encode(), name.encode(), hashlib.sha256).hexdigest()
        return self.username, password

    async def docker(self, *args, input_data=None, optional=False, command_timeout=30):
        process = await asyncio.create_subprocess_exec("docker", *args, stdin=asyncio.subprocess.PIPE if input_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(input_data), command_timeout)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            if optional:
                return None
            raise HTTPException(503, "CODING_CONTAINER_OPERATION_FAILED")
        if len(output) > 2_000_000:
            raise HTTPException(503, "CODING_CONTAINER_METADATA_LIMIT")
        return output

    async def ensure(self, workspace):
        name = workspace_name(self.scope, workspace, self.backend)
        self.accessed[name] = time.monotonic()
        rows = await self.docker("ps", "--filter", "label=osa.opencode.scope="+self.scope, "--format", "{{.Names}}")
        for other in rows.decode().splitlines():
            if other != name and self.owns(other):
                try:
                    if not await self.sessions_idle(other):
                        raise HTTPException(409, "ANOTHER_CODING_WORKSPACE_IS_BUSY")
                except (httpx.HTTPError, ValueError):
                    raise HTTPException(503, "OTHER_WORKSPACE_STATUS_UNKNOWN")
                await self.retire(other, running=True)
        running = await self.docker("inspect", "--format", "{{.State.Running}}", name, optional=True)
        if running is not None and running.strip() == b"false":
            await self.retire(name)
            running = None
        if running is None:
            config_url = os.environ.get("OSA_CODEX_MODEL_CONFIG_URL" if self.backend == "codex" else "OSA_OPENCODE_MODEL_CONFIG_URL", "")
            direct_key = os.environ.get("OSA_CODEX_API_KEY", "") if self.backend == "codex" else ""
            if direct_key:
                codex_model = os.environ.get("OSA_CODEX_MODEL", "").strip()
                if not codex_model:
                    raise HTTPException(409, "CODEX_MODEL_NOT_CONFIGURED")
                model = {"base_url": os.environ.get("OSA_CODEX_BASE_URL", "https://api.openai.com/v1"),
                         "model": codex_model, "api_key": direct_key, "headers": {}}
            elif config_url:
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    response = await client.get(config_url, headers={"Authorization": "Bearer " + os.environ.get("OSA_WORKER_TOKEN", "")})
                    if response.status_code != 200:
                        raise HTTPException(409, "CODING_WEB_MODEL_NOT_CONFIGURED")
                    model = response.json()["data"]
            else:
                model = self.environment_model()
            # docker create --env reads only the explicitly whitelisted variables
            # from a minimal child environment; no user command is passed to a shell.
            if self.backend == "codex":
                chat_bridge = not bool(direct_key)
                configuration = codex_config("http://127.0.0.1:4096/v1" if chat_bridge else model["base_url"],
                                             model["model"], None if chat_bridge else model.get("headers"), chat_bridge)
                env = {"CODEX_SERVER_PASSWORD": self.child_auth(name)[1], "CODEX_SERVER_USERNAME": self.username,
                       "OSA_CODEX_CONFIG_CONTENT": configuration, "CODEX_API_KEY": model["api_key"],
                       "OSA_CODEX_MODEL": model["model"],
                       "OSA_CODEX_CHAT_BRIDGE": "true" if chat_bridge else "false",
                       "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                       "OSA_CODEX_UPSTREAM_BASE_URL": model["base_url"] if chat_bridge else "",
                       "OSA_CODEX_UPSTREAM_HEADERS": json.dumps(model.get("headers") or {}) if chat_bridge else "{}"}
            else:
                configuration = coding_config(model["base_url"], model["model"], model["api_key"], model.get("headers"), model.get("options"))
                env = {"OPENCODE_SERVER_PASSWORD": self.child_auth(name)[1], "OPENCODE_SERVER_USERNAME": self.username,
                       "OPENCODE_CONFIG_CONTENT": configuration}
            await self.create(name, env)
            running = b"false"
        if running.strip() != b"true":
            await self.docker("start", name)
        return name

    @staticmethod
    def environment_model():
        key = os.environ.get("OSA_WORK_MODEL_API_KEY", "")
        if not key:
            raise HTTPException(409, "CODING_MODEL_NOT_CONFIGURED")
        return {"base_url": os.environ.get("OSA_WORK_MODEL_BASE_URL", "https://api.openai.com/v1"),
                "model": os.environ.get("OSA_WORK_MODEL", "configured-work-model"), "api_key": key, "headers": {}}

    async def create(self, name, environment):
        args = ["docker", "create", "--name", name, "--label", "osa.opencode.scope="+self.scope,
            "--network", self.network, "--network-alias", name,
            "--memory", os.environ.get("OSA_OPENCODE_MEMORY", "768m"), "--memory-swap", os.environ.get("OSA_OPENCODE_SWAP", "1536m"),
            "--cpus", "1", "--pids-limit", "128", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m,uid=10005,gid=10005", "--init",
            "--log-driver", "local", "--log-opt", "max-size=5m", "--log-opt", "max-file=2",
            "--mount", "type=volume,source="+name+"-home,target=/home/agent",
            "--mount", "type=volume,source="+name+"-code,target=/project"]
        args += [arg for key in environment for arg in ("--env", key)]
        args += ["osa-codex-runner:0.147.0" if self.backend == "codex" else "osa-opencode-runner:1.2.27"]
        # A pipe-free exec with an explicit environment avoids secrets in argv.
        process = await asyncio.create_subprocess_exec(*args, env={**os.environ, **environment},
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), 30)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise HTTPException(503, "CODING_CONTAINER_CREATE_FAILED")

    async def erase(self, workspace):
        name = workspace_name(self.scope, workspace, self.backend)
        await self.docker("rm", "--force", name, optional=True)
        for suffix in ("-home", "-code"):
            await self.docker("volume", "rm", name+suffix, optional=True)
        remaining = await self.docker("ps", "-a", "--filter", "label=osa.opencode.scope="+self.scope, "--format", "{{.Names}}")
        volumes = await self.docker("volume", "ls", "--filter", "name="+name, "--format", "{{.Name}}")
        if name in remaining.decode().splitlines() or {name+'-home', name+'-code'} & set(volumes.decode().splitlines()):
            raise HTTPException(503, "WORKSPACE_ERASURE_NOT_CONFIRMED")
        self.accessed.pop(name, None)

    async def stop_children(self):
        async with self.lock:
            rows = await self.docker("ps", "-a", "--filter", "label=osa.opencode.scope="+self.scope,
                                     "--format", "{{.Names}}|{{.State}}")
            for row in rows.decode().splitlines():
                name, state = row.split("|", 1)
                if self.owns(name) and state in {"running", "created", "exited", "dead"}:
                    await self.retire(name, running=state == "running")

    async def backup(self):
        await self.stop_children()
        volumes = await self.docker("volume", "ls", "--filter", "name=" + self.prefix, "--format", "{{.Name}}")
        target = os.environ.get("OSA_OPENCODE_BACKUP_VOLUME", "")
        if not target or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]+", target):
            raise RuntimeError("Backup volume is not configured")
        timestamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        script = "import os,pathlib,sys,tarfile; os.umask(0o077); p=pathlib.Path('/backup/backups'); p.mkdir(exist_ok=True); os.chown(p,10001,10001); target=p/sys.argv[1]; a=tarfile.open(target,'w:gz'); a.add('/source',arcname='volume'); a.close(); os.chown(target,10001,10001)"
        count = 0
        for volume in volumes.decode().splitlines():
            name, separator, suffix = volume.rpartition("-")
            if not separator or suffix not in {"home", "code"} or not self.owns(name):
                continue
            await self.docker("run", "--rm", "--network", "none", "--read-only", "--memory", "128m", "--cpus", "0.5",
                "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--cap-add", "CHOWN", "--security-opt", "no-new-privileges", "--mount", "type=volume,source="+volume+",target=/source,readonly",
                "--mount", "type=volume,source="+target+",target=/backup", "python:3.12-slim", "python", "-c", script, volume+'-'+timestamp+'.tar.gz', command_timeout=600)
            count += 1
        print(f"{self.backend} volumes archived: {count}")

    async def idle_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.cleanup_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("workspace_cleanup_failed error_type=%s", type(error).__name__)


def create_app():
    gateway = Gateway()
    @asynccontextmanager
    async def lifespan(app):
        reaper = asyncio.create_task(gateway.idle_loop())
        try:
            yield
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
            await gateway.stop_children()
    app = FastAPI(lifespan=lifespan)

    @app.api_route("/projects/{workspace}/{path:path}", methods=["GET", "POST", "DELETE"])
    async def proxy(workspace: str, path: str, request: Request):
        expected = "Basic "+base64.b64encode((gateway.username+":"+gateway.password).encode()).decode()
        if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401, "UNAUTHORIZED")
        try:
            workspace_name(gateway.scope, workspace, gateway.backend)
        except ValueError:
            raise HTTPException(400, "INVALID_WORKSPACE")
        if path == "_workspace" and request.method == "DELETE":
            async with gateway.lock:
                await gateway.erase(workspace)
            return {"erased": True}
        # No configuration/auth/permission/shell/share endpoint can be proxied.
        allowed = ((request.method == "GET" and (path in {"global/health", "session", "session/status", "file", "file/status"} or path == "file/content" or re.fullmatch(r"session/[A-Za-z0-9_-]+(?:/message)?", path)))
            or (request.method == "POST" and (path == "session" or re.fullmatch(r"session/[A-Za-z0-9_-]+/(prompt_async|abort)", path)))
            or (request.method == "DELETE" and re.fullmatch(r"session/[A-Za-z0-9_-]+", path)))
        if not allowed:
            raise HTTPException(403, "OPENCODE_ENDPOINT_DENIED")
        if request.headers.get("content-length") and int(request.headers["content-length"]) > 700000:
            raise HTTPException(413, "BODY_LIMIT")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 700000:
                raise HTTPException(413, "BODY_LIMIT")
        # A credentialed caller cannot override the sandbox project directory.
        query_keys = {"path"} if path in {"file", "file/content"} else {"search", "limit"}
        if set(request.query_params)-query_keys:
            raise HTTPException(403, "QUERY_OVERRIDE_DENIED")
        if "path" in request.query_params:
            target = request.query_params["path"].replace("\\", "/")
            if target.startswith("/") or ":" in target or ".." in target.split("/"):
                raise HTTPException(403, "FILE_PATH_DENIED")
        async with gateway.lock:
            name = await gateway.ensure(workspace)
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                for attempt in range(12):
                    try:
                        async with client.stream(request.method, "http://"+name+":4096/"+path, content=bytes(body),
                            params=dict(request.query_params), headers={"Content-Type": "application/json"}, auth=gateway.child_auth(name)) as upstream:
                            output = bytearray()
                            async for chunk in upstream.aiter_bytes():
                                output.extend(chunk)
                                if len(output) > 2_000_000:
                                    raise HTTPException(502, "PROVIDER_RESPONSE_LIMIT")
                            return Response(bytes(output), status_code=upstream.status_code, media_type="application/json")
                    except httpx.ConnectError:
                        if attempt == 11:
                            raise HTTPException(503, "CODING_SERVER_STARTING")
                        await asyncio.sleep(.5)
    return app


if __name__ == '__main__' and sys.argv[1:] == ['--backup']:
    asyncio.run(Gateway().backup())
