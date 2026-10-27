"""Workspace-local HTTP bridge for one isolated Codex CLI process."""
import asyncio
import base64
import json
import os
import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


HOME = Path(os.environ.get("CODEX_HOME", "/home/agent/.codex"))
PROJECT = Path("/project")
STATE_FILE = HOME / "osa-state.json"
VERSION = "0.147.0"


class Workspace:
    def __init__(self):
        HOME.mkdir(parents=True, exist_ok=True)
        config = os.environ.get("OSA_CODEX_CONFIG_CONTENT")
        if config:
            (HOME / "config.toml").write_text(config, encoding="utf-8")
        self.sessions = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        self.lock = asyncio.Lock()
        self.process = None
        self.active_session = None
        for session in self.sessions.values():
            if session.get("status") == "busy":
                session["status"] = "idle"
                for message in reversed(session.get("messages", [])):
                    if message.get("info", {}).get("role") == "assistant" and not message["info"].get("time", {}).get("completed"):
                        message["info"].update(error={"message": "Codex CLI process interrupted; resume the task"},
                                               time={**message["info"].get("time", {}), "completed": int(time.time() * 1000)})
                        break
        self.save()

    def save(self):
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.sessions, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(STATE_FILE)

    def session(self, session_id):
        session = self.sessions.get(session_id)
        if not session:
            raise HTTPException(404, "SESSION_NOT_FOUND")
        return session

    async def run(self, session_id, prompt, assistant_id):
        session = self.session(session_id)
        command = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox",
                   "--skip-git-repo-check", "-C", str(PROJECT)]
        if session.get("thread_id"):
            command += ["resume", session["thread_id"], "-"]
        else:
            command += ["-"]
        message = next(item for item in session["messages"] if item["info"]["id"] == assistant_id)
        error = None
        final_text = ""
        stderr_tail = bytearray()
        try:
            self.process = await asyncio.create_subprocess_exec(*command, cwd=PROJECT,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            self.process.stdin.write(prompt.encode("utf-8"))
            await self.process.stdin.drain()
            self.process.stdin.close()

            async def read_stderr():
                async for chunk in self.process.stderr:
                    stderr_tail.extend(chunk[:2000])
                    if len(stderr_tail) > 4000:
                        del stderr_tail[:-4000]

            stderr_reader = asyncio.create_task(read_stderr())
            async for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type", "")
                if kind == "thread.started" and event.get("thread_id"):
                    session["thread_id"] = event["thread_id"]
                elif kind.startswith("item."):
                    item = event.get("item") or {}
                    item_type = item.get("type")
                    if item_type == "agent_message" and kind == "item.completed":
                        final_text = str(item.get("text") or "")[:8000]
                    elif item_type in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                        tool = {"command_execution": "bash", "file_change": "apply_patch",
                                "mcp_tool_call": "mcp", "web_search": "web"}[item_type]
                        detail = item.get("command") or item.get("changes") or item.get("server") or item.get("query") or ""
                        if not isinstance(detail, str):
                            detail = json.dumps(detail, ensure_ascii=False)
                        state = "running" if kind == "item.started" else "completed"
                        if item.get("status") in {"failed", "error"}:
                            state = "error"
                        part = {"type": "tool", "tool": tool, "state": {"status": state,
                                "input": {"command": detail[:180]}, "error": str(item.get("error") or "")[:500]}}
                        if message["parts"] and message["parts"][-1].get("id") == item.get("id"):
                            message["parts"][-1] = {"id": item.get("id"), **part}
                        else:
                            message["parts"].append({"id": item.get("id"), **part})
                            message["parts"] = message["parts"][-40:]
                elif kind in {"turn.failed", "error"}:
                    detail = event.get("error") or event.get("message") or "Codex CLI turn failed"
                    error = str(detail)[:800]
                self.save()
            await self.process.wait()
            await stderr_reader
            if self.process.returncode and not error:
                error = stderr_tail.decode(errors="replace")[-800:] or f"Codex CLI exited {self.process.returncode}"
            if not error and not final_text and not message["parts"]:
                error = "Codex CLI ended without a message or tool action"
        except asyncio.CancelledError:
            error = "Codex CLI process cancelled"
            if self.process and self.process.returncode is None:
                self.process.terminate()
                await self.process.wait()
        except Exception as exc:
            error = f"Codex CLI bridge error: {type(exc).__name__}: {exc}"
        finally:
            if final_text:
                message["parts"].append({"type": "text", "text": final_text})
            message["info"]["time"]["completed"] = int(time.time() * 1000)
            if error:
                message["info"]["error"] = {"message": error[:800]}
            session["status"] = "idle"
            self.active_session = None
            self.process = None
            self.save()


def create_app():
    workspace = Workspace()
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request: Request, next_handler):
        if request.url.path == "/v1/responses":
            key = os.environ.get("CODEX_API_KEY", "")
            expected = "Bearer " + key
            if not key or not secrets.compare_digest(request.headers.get("authorization", ""), expected):
                return JSONResponse({"detail": "UNAUTHORIZED"}, status_code=401)
            return await next_handler(request)
        username = os.environ.get("CODEX_SERVER_USERNAME", "codex")
        password = os.environ.get("CODEX_SERVER_PASSWORD", "")
        expected = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
        if not password or not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            return JSONResponse({"detail": "UNAUTHORIZED"}, status_code=401)
        return await next_handler(request)

    @app.post("/v1/responses")
    async def responses(request: Request):
        if os.environ.get("OSA_CODEX_CHAT_BRIDGE") != "true":
            raise HTTPException(404, "CHAT_BRIDGE_DISABLED")
        body = await request.json()
        model = os.environ.get("OSA_CODEX_MODEL", "")
        if body.get("model") != model:
            raise HTTPException(400, "MODEL_MISMATCH")
        upstream = os.environ.get("OSA_CODEX_UPSTREAM_BASE_URL", "")
        if not upstream:
            raise HTTPException(503, "UPSTREAM_NOT_CONFIGURED")
        headers = json.loads(os.environ.get("OSA_CODEX_UPSTREAM_HEADERS", "{}"))
        from litellm import aresponses
        stream = body.get("stream") is True
        options = {key: value for key, value in body.items() if key != "model"}
        try:
            result = await aresponses(model="custom_openai/" + model, api_base=upstream,
                api_key=os.environ["CODEX_API_KEY"], extra_headers=headers,
                drop_params=True, **options)
        except Exception as error:
            raise HTTPException(502, "CODEX_MODEL_BRIDGE_FAILED: " + type(error).__name__) from error
        if not stream:
            return JSONResponse(result.model_dump(mode="json"))

        async def events():
            try:
                async for event in result:
                    data = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
                    event_type = data.get("type", "message")
                    yield "event: " + event_type + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
            except Exception as error:
                failure = {"type": "error", "error": {"message": "CODEX_MODEL_BRIDGE_STREAM_FAILED: " + type(error).__name__}}
                yield "event: error\ndata: " + json.dumps(failure) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/global/health")
    async def health():
        return {"healthy": True, "version": VERSION}

    @app.get("/session")
    async def sessions(search: str = "", limit: int = 100):
        return [{"id": session_id, "title": session["title"]} for session_id, session in workspace.sessions.items()
                if not search or search in session["title"]][:min(limit, 100)]

    @app.post("/session")
    async def create_session(request: Request):
        body = await request.json()
        title = str(body.get("title") or "")[:120]
        if not title:
            raise HTTPException(422, "TITLE_REQUIRED")
        async with workspace.lock:
            session_id = "ses_" + uuid.uuid4().hex
            workspace.sessions[session_id] = {"title": title, "thread_id": None,
                "status": "idle", "messages": []}
            workspace.save()
        return {"id": session_id, "title": title}

    @app.get("/session/status")
    async def status():
        return {session_id: {"type": session["status"]} for session_id, session in workspace.sessions.items()}

    @app.get("/session/{session_id}")
    async def get_session(session_id: str):
        session = workspace.session(session_id)
        return {"id": session_id, "title": session["title"]}

    @app.delete("/session/{session_id}")
    async def delete_session(session_id: str):
        async with workspace.lock:
            session = workspace.session(session_id)
            if session["status"] == "busy" or workspace.active_session is not None:
                raise HTTPException(409, "SESSION_BUSY")
            del workspace.sessions[session_id]
            workspace.save()
        return {"deleted": True}

    @app.get("/session/{session_id}/message")
    async def messages(session_id: str, limit: int = 100):
        return workspace.session(session_id)["messages"][-min(limit, 100):]

    @app.post("/session/{session_id}/prompt_async")
    async def prompt(session_id: str, request: Request):
        body = await request.json()
        message_id = str(body.get("messageID") or "")
        parts = body.get("parts") or []
        instruction = parts[0].get("text") if parts and isinstance(parts[0], dict) else None
        if not message_id or not isinstance(instruction, str) or not instruction:
            raise HTTPException(422, "INVALID_PROMPT")
        async with workspace.lock:
            session = workspace.session(session_id)
            if any(item["info"]["id"] == message_id for item in session["messages"]):
                return {"accepted": True}
            if session["status"] == "busy" or workspace.active_session is not None:
                raise HTTPException(409, "SESSION_BUSY")
            assistant_id = "msg_" + uuid.uuid4().hex
            now = int(time.time() * 1000)
            session["messages"].extend([
                {"info": {"id": message_id, "role": "user", "time": {"created": now}},
                 "parts": [{"type": "text", "text": instruction[:30000]}]},
                {"info": {"id": assistant_id, "role": "assistant", "parentID": message_id,
                          "time": {"created": now}}, "parts": []},
            ])
            session["status"] = "busy"
            workspace.active_session = session_id
            workspace.save()
            asyncio.create_task(workspace.run(session_id, instruction, assistant_id))
        return {"accepted": True}

    @app.post("/session/{session_id}/abort")
    async def abort(session_id: str):
        workspace.session(session_id)
        if workspace.active_session == session_id and workspace.process and workspace.process.returncode is None:
            workspace.process.terminate()
        return {"aborting": True}

    def project_path(value):
        target = (PROJECT / value).resolve()
        if not target.is_relative_to(PROJECT.resolve()):
            raise HTTPException(403, "FILE_PATH_DENIED")
        return target

    @app.get("/file")
    async def files(path: str = "."):
        directory = project_path(path)
        if not directory.is_dir():
            raise HTTPException(404, "DIRECTORY_NOT_FOUND")
        return [{"path": str(child.relative_to(PROJECT)), "type": "directory" if child.is_dir() else "file"}
                for child in sorted(directory.iterdir()) if not child.is_symlink()]

    @app.get("/file/content")
    async def content(path: str):
        target = project_path(path)
        if not target.is_file() or target.is_symlink() or target.stat().st_size > 500000:
            raise HTTPException(404, "FILE_NOT_READABLE")
        return {"type": "text", "content": target.read_text(encoding="utf-8")}

    return app
