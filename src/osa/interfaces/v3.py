"""Owner console API; QQ and Thinking use their separately scoped service paths."""
import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from osa.interfaces.api import require_admin, response
from osa.interfaces.v2 import (CreateWork, WorkCommand, create_work, work_task, work_tasks,
    work_command, work_artifacts, work_changes, task, service, delete_work_task)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])


def enabled(request):
    if not request.app.state.v3:
        raise HTTPException(409, "V3_DISABLED")
    return request.app.state.v3


@router.get("/agents/config")
async def agent_config(request: Request):
    enabled(request)
    s = request.app.state.settings
    routes = request.app.state.runtime_configuration.snapshot().task_configs
    work_models = routes.get("work").model_list if routes.get("work") else []
    observer_models = routes.get("observer").model_list if routes.get("observer") else []
    def models(name, fallback):
        return routes[name].model_list if routes.get(name) else fallback
    return response({"protocol_version": 2, "decision": {"mode": "thinking", "models": models('planner', []), "model": s.thinking_model or s.chat_model},
        "thinking": {"model": s.thinking_model or s.chat_model, "models": models('planner', []), "max_steps": s.thinking_max_steps, "max_seconds": s.thinking_max_seconds},
        "reply": {"mode": "integrated", "model": s.thinking_model or s.chat_model, "tools": False}, "memory": {"model": s.memory_model or s.chat_model},
        "research": {"provider": "deepseek_web", "manual_login": True}, "embedding": {"model": s.embedding_model},
        "image": {"model": s.vision_model}, "video": {"provider": "keyframes+actual_audio_asr", "audio_model": s.audio_model},
        "coding_agent": {"adapter": s.coding_provider, "models": work_models, "pinned_version": s.opencode_version if s.coding_provider == "opencode" else s.codex_version if s.coding_provider == "codex" else None,
            "can_pause_natively": False, "pause_mode": "safe_boundary", "scope": "Python project + static assets, platform sandbox validation"},
        "code_observer": {"model": observer_models[0] if observer_models else s.observer_model, "models": observer_models,
                          "readonly": True, "enabled": bool(observer_models or s.observer_model), "fallback": "immutable snapshot only"}, "tts": False})


@router.post("/coding/tasks")
async def create_coding(body: CreateWork, request: Request):
    enabled(request)
    return await create_work(body, request)


@router.get("/coding/tasks")
async def list_coding(request: Request):
    enabled(request)
    return await work_tasks(request)


@router.get("/coding/tasks/{tid}")
async def get_coding(tid: str, request: Request):
    enabled(request)
    result = await work_task(tid, request)
    result["data"]["provider"] = await service(request).call("binding", tid)
    return result


@router.delete('/coding/tasks/{tid}')
async def delete_coding(tid: str, request: Request, expected_version: int | None = Query(default=None, ge=1)):
    enabled(request)
    return await delete_work_task(tid, request, expected_version)


@router.get("/coding/tasks/{tid}/progress")
async def progress(tid: str, request: Request):
    enabled(request)
    row = await task(request, tid)
    value = service(request).registry.public(row)
    return response({**value, "git_revision": row.get("git_commit"), "last_event_at": row["last_progress_at"],
                     "waiting_for": row.get("blocker"), "provider": await service(request).call("binding", tid)})


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class Instruction(Control):
    text: str = Field(min_length=1, max_length=8000)


async def control(request, tid, body, kind, payload=None):
    enabled(request)
    return await work_command(tid, WorkCommand(kind=kind, payload=payload or {}, expected_version=body.expected_version, idempotency_key=body.idempotency_key), request)


@router.post("/coding/tasks/{tid}/instructions")
async def instruction(tid: str, body: Instruction, request: Request):
    return await control(request, tid, body, "ADD_REQUIREMENT", {"text": body.text})


@router.post("/coding/tasks/{tid}/pause")
async def pause(tid: str, body: Control, request: Request):
    return await control(request, tid, body, "PAUSE")


@router.post("/coding/tasks/{tid}/resume")
async def resume(tid: str, body: Control, request: Request):
    return await control(request, tid, body, "RESUME")


@router.post("/coding/tasks/{tid}/cancel")
async def cancel(tid: str, body: Control, request: Request):
    return await control(request, tid, body, "CANCEL")


class Inspection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["STATUS", "INSPECT", "REVIEW"] = "STATUS"
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40,64}$")


@router.post("/coding/tasks/{tid}/inspect")
async def inspect(tid: str, body: Inspection, request: Request):
    enabled(request)
    await task(request, tid)
    try:
        return response(await request.app.state.observer.inspect(tid, body.mode, body.revision))
    except ValueError:
        raise HTTPException(409, "FIXED_SNAPSHOT_OR_REVIEW_INVALID")


@router.get("/coding/tasks/{tid}/reviews")
async def reviews(tid: str, request: Request):
    enabled(request)
    await task(request, tid)
    return response(await service(request).call("reviews", tid))


@router.get("/coding/tasks/{tid}/events")
async def events(tid: str, request: Request):
    enabled(request)
    await task(request, tid)
    try:
        after = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "INVALID_EVENT_CURSOR")
    async def stream():
        nonlocal after
        while not await request.is_disconnected():
            for row in await service(request).call("domain_events", tid, after):
                after = row["seq"]
                yield f'id: {after}\nevent: {row["event_type"]}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n'
            yield ': heartbeat\n\n'
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/coding/tasks/{tid}/timeline")
async def timeline(tid: str, request: Request):
    """Return a compact snapshot for the task detail drawer.

    The live SSE endpoint remains available for integrations. The console uses
    this bounded snapshot so opening a task does not create a second long-lived
    connection for every card.
    """
    enabled(request)
    await task(request, tid)
    return response(await service(request).call("domain_events", tid, 0, latest=True))


@router.get("/coding/tasks/{tid}/artifacts")
async def artifacts(tid: str, request: Request):
    enabled(request)
    return await work_artifacts(tid, request)


@router.get("/coding/tasks/{tid}/changes")
async def changes(tid: str, request: Request):
    enabled(request)
    return await work_changes(tid, request)


@router.get("/thinking/sessions/{sid}")
async def thinking(sid: str, request: Request):
    enabled(request)
    try:
        row = await service(request).call("thinking", sid)
        # Public plan/action/trace only. Never exposes a hidden model chain of thought.
        value = row["payload"]
        return response({"id": sid, "status": row["status"], "updated": row["updated"], "result": value.get("result"), "traces": value.get("traces", [])})
    except KeyError:
        raise HTTPException(404, "THINKING_SESSION_NOT_FOUND")
