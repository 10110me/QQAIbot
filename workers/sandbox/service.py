"""Trusted Docker controller; each user program runs in a separate restricted container."""
import asyncio
import base64
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from resource_slot import HeavySlot
from osa.domain.work import validate_files, decode_input_files


class RunInput(BaseModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32,64}$")
    kind: str = "python"
    code: str = Field(default="", max_length=40000)
    files: dict[str, str] | None = None
    inputs: dict[str, str] = Field(default_factory=dict, max_length=20)
    timeout: int = Field(default=20, ge=1, le=60)


async def docker(*arguments: str, timeout: int = 90) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec("docker", *arguments, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    return process.returncode, output[:200000]


def create_app() -> FastAPI:
    token = os.environ.get("OSA_WORKER_TOKEN", "")
    if len(token) < 24:
        raise RuntimeError("Worker token not configured")
    root = Path(os.environ.get("SANDBOX_DATA_DIR", "/sandbox-data"))
    root.mkdir(parents=True, exist_ok=True)
    # This volume is mounted at the same absolute host/controller path.
    host_root = os.environ.get("SANDBOX_HOST_DATA_DIR", str(root))
    image = os.environ.get("SANDBOX_IMAGE", "osa-python-runner:0.2")
    lock = asyncio.Lock()

    async def auth(authorization: str = Header(default="")) -> None:
        if not secrets.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "Unauthorized")

    app = FastAPI(dependencies=[Depends(auth)])

    @app.get("/health")
    async def health() -> dict:
        code, _ = await docker("version", "--format", "{{.Server.Version}}", timeout=5)
        return {"ready": code == 0}

    @app.post("/run")
    async def run(payload: RunInput) -> dict:
        if payload.kind not in {"python", "project"}:
            raise HTTPException(422, "Only the isolated Python runner is allowed")
        if payload.kind == "project":
            validate_files(payload.files or {})
        elif not payload.code:
            raise HTTPException(422, "Python code required")
        if payload.files:
            validate_files(payload.files)
        try:
            input_files = decode_input_files(payload.inputs)
        except (ValueError, PermissionError):
            raise HTTPException(422, 'Invalid task inputs') from None
        name = "osa-task-" + payload.request_id
        directory = root / payload.request_id
        result_path = directory / "result.json"
        async with lock, HeavySlot():
            if result_path.exists():
                return json.loads(result_path.read_text())
            directory.mkdir(mode=0o755, exist_ok=True)
            source, output = directory / "source", directory / "output"
            source.mkdir(mode=0o755, exist_ok=True)
            output.mkdir(mode=0o777, exist_ok=True)
            output.chmod(0o777)
            runner = 'python_runner.py' if payload.kind == 'python' else 'project_runner.py'
            (source / "main.py").write_text((Path(__file__).parent / runner).read_text(encoding='utf-8'), encoding='utf-8')
            if payload.kind == 'python':
                (source / 'program.json').write_text(json.dumps({'code': payload.code, 'files': payload.files or {}}), encoding='utf-8')
                (source / 'program.json').chmod(0o444)
            if payload.kind == "project":
                (source / "files.json").write_text(json.dumps(payload.files))
                (source / "files.json").chmod(0o444)
            (source / "main.py").chmod(0o444)
            inputs = source / 'inputs'
            inputs.mkdir(mode=0o755, exist_ok=True)
            for filename, content in input_files.items():
                target = inputs / filename
                target.write_bytes(content)
                target.chmod(0o444)
            host_dir = Path(host_root) / payload.request_id
            code, created = await docker("create", "--name", name, "--network=none", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit=64", "--ulimit=fsize=16777216:16777216",
                "--memory=" + os.environ.get("SANDBOX_MEMORY", "256m"),
                "--memory-swap=" + os.environ.get("SANDBOX_MEMORY_SWAP", "256m"), "--cpus=1", "--user=10004:10004",
                "--log-driver=local", "--log-opt=max-size=1m", "--log-opt=max-file=2",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=64m", "--workdir=/output",
                "--mount", f"type=bind,src={host_dir / 'source'},dst=/input,readonly",
                "--mount", f"type=bind,src={host_dir / 'source' / 'inputs'},dst=/inputs,readonly",
                "--mount", f"type=bind,src={host_dir / 'output'},dst=/output",
                "--env=OMP_NUM_THREADS=1", "--env=OPENBLAS_NUM_THREADS=1", "--env=PYTHONDONTWRITEBYTECODE=1", "--env=HOME=/tmp",
                "--env=HTTP_PROXY=", "--env=HTTPS_PROXY=", "--env=ALL_PROXY=", "--env=NO_PROXY=",
                "--env=http_proxy=", "--env=https_proxy=", "--env=all_proxy=", "--env=no_proxy=", image,
                "timeout", "--kill-after=2", str(payload.timeout), "python", "-I", "/input/main.py", timeout=15)
            if code:
                # Existing container after controller restart is not executed again.
                inspect_code, state = await docker("inspect", "--format", "{{.State.Status}}", name, timeout=5)
                if inspect_code == 0:
                    await docker("rm", "-f", name, timeout=10)
                    result = {"status": "unknown", "error": "A previous execution existed; not repeated"}
                    result_path.write_text(json.dumps(result))
                    return result
                raise HTTPException(503, "Runner image or Docker unavailable")
            status, logs, returncode = "succeeded", b"", None
            try:
                await docker("start", name, timeout=10)
                code, finished = await docker("wait", name, timeout=payload.timeout+5)
                returncode = int(finished.strip()) if code == 0 else None
                _, logs = await docker("logs", name, timeout=5)
                status = "succeeded" if returncode == 0 else "failed"
            except TimeoutError:
                status = "timeout"
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            finally:
                await docker("rm", "-f", name, timeout=10)
            artifacts, total = [], 0
            for item in sorted(output.iterdir()):
                if item.is_symlink() or not item.is_file() or item.stat().st_size > 5_000_000:
                    continue
                total += item.stat().st_size
                if total > 10_000_000 or len(artifacts) >= 10:
                    break
                raw = item.read_bytes()
                artifacts.append({"name": item.name[:200], "content": base64.b64encode(raw).decode(),
                                  "sha256": hashlib.sha256(raw).hexdigest()})
            result = {"status": status, "exit_code": returncode, "stdout": logs.decode(errors="replace")[:20000], "artifacts": artifacts}
            result_path.write_text(json.dumps(result))
            return result

    @app.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: str) -> dict:
        if not all(c in "0123456789abcdef" for c in task_id) or not 32 <= len(task_id) <= 64:
            raise HTTPException(422, "Invalid task ID")
        await docker("rm", "-f", "osa-task-" + task_id, timeout=10)
        return {"status": "cancelled"}

    return app
