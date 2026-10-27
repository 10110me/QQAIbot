import secrets
import uuid
from dataclasses import asdict
from datetime import date
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from osa.domain.entities import Destination, GroupPolicy

router = APIRouter()


def response(data: Any) -> dict[str, Any]:
    return {"request_id": uuid.uuid4().hex, "data": data, "error": None}


async def require_admin(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    expected = "Bearer " + request.app.state.settings.admin_token.get_secret_value()
    if authorization is not None and secrets.compare_digest(authorization, expected):
        request.state.principal = {"name": "owner", "role": "owner", "groups": None}
        return
    account = next((value for value in request.app.state.settings.admin_accounts
                    if authorization and secrets.compare_digest(authorization, "Bearer "+value.token.get_secret_value())), None)
    if account is None:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    request.state.principal = {"name": account.name, "role": account.role, "groups": account.groups}
    path, method = request.url.path, request.method
    if method == "GET" and path in {"/api/v1/groups", "/api/v1/messages", "/api/v1/stats", "/api/v1/system/info"}:
        return
    if path.startswith("/api/v1/groups/"):
        group = path.split("/")[4]
        if group in account.groups and (method == "GET" or account.role == "admin"):
            return
    if path.startswith("/api/v1/messages/"):
        parts = path.split("/")
        value = await request.app.state.store.message_detail(parts[4])
        if value and value["conversation"] in {"group:"+group for group in account.groups}:
            if method == "GET" or (method == "POST" and path.endswith("/reviews") and account.role in {"admin","moderator","reviewer"}):
                return
    if path == "/api/v1/replays" and method == "POST" and account.role in {"admin","moderator","reviewer"}:
        value = await request.json()
        item = await request.app.state.store.message_detail(str(value.get("message_id", "")))
        if item and item["conversation"] in {"group:"+group for group in account.groups}:
            return
    raise HTTPException(403, "ROLE_OR_GROUP_SCOPE_DENIED")


admin = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    collect_messages: bool = True
    notice_read: bool = False
    file_metadata_read: bool = False
    cross_memory: bool = False
    context_limit: int = Field(default=40, ge=1, le=80)
    reply_ttl_seconds: int = Field(default=180, ge=10, le=600)
    min_send_interval: float = Field(default=2.0, ge=0.5, le=60)
    quiet_start: int | None = Field(default=None, ge=0, le=23)
    quiet_end: int | None = Field(default=None, ge=0, le=23)
    timezone: str = "Asia/Shanghai"
    file_content_index: bool = False
    media_read: bool = False
    research: bool = False
    sandbox: bool = False
    initiative: bool = False
    initiative_min_interval: int = Field(default=45, ge=10, le=86400)
    initiative_daily_limit: int = Field(default=0, ge=0, le=10000)
    memory_extract: bool = False
    experience_collect: bool = False
    experience_embedding: bool = False
    research_external: bool = False
    echo_silence: bool = True
    talk_frequency: float = Field(default=0.5, ge=0.01, le=1)
    batch_quiet_seconds: float = Field(default=1.0, ge=0, le=5)
    batch_max_wait_seconds: float = Field(default=8.0, ge=1, le=60)
    decision_cooldown_seconds: float = Field(default=8.0, ge=0, le=120)
    decision_wait_seconds: float = Field(default=5.0, ge=1, le=30)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown timezone") from exc
        return value

    @model_validator(mode="after")
    def quiet_pair(self) -> "PolicyInput":
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("quiet_start and quiet_end must be configured together")
        return self


REVIEW_TAGS = {"too_ai", "forced_joke", "overly_praising", "too_long", "unnecessary_reply", "context_mismatch", "memory_misuse", "fact_error", "privacy_risk", "natural", "echo_interruption", "fabricated_progress", "unsupported_experience", "unauthorized_disclosure"}


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: list[str] = Field(min_length=1, max_length=10)
    better_response: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, value: list[str]) -> list[str]:
        if not set(value) <= REVIEW_TAGS:
            raise ValueError("Unknown review tag")
        return sorted(set(value))


class PauseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool


class ReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str


@router.get("/health/live")
async def live() -> dict[str, Any]:
    return response({"status": "alive"})


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, Any]:
    try:
        await request.app.state.store.health()
    except Exception as exc:
        raise HTTPException(503, "DATABASE_UNAVAILABLE") from exc
    return response({"status": "ready", "onebot_connected": request.app.state.platform.ready})


@admin.get("/groups")
async def groups(request: Request) -> dict[str, Any]:
    values = await request.app.state.store.groups()
    scope = request.state.principal["groups"]
    names = await request.app.state.platform.group_names()
    return response([{**row, "name": names.get(row["id"], ""),
                      "settings": asdict(GroupPolicy(**row["settings"]))} for row in values
                     if row["id"] in request.app.state.settings.allowed_groups
                     and (scope is None or row["id"] in scope)])


def require_group(request: Request, group_id: str) -> None:
    if group_id not in request.app.state.settings.allowed_groups:
        raise HTTPException(403, "GROUP_NOT_ALLOWLISTED")
    scope = getattr(request.state, "principal", {}).get("groups")
    if scope is not None and group_id not in scope:
        raise HTTPException(403, "GROUP_SCOPE_DENIED")


@admin.get("/groups/{group_id}/settings")
async def get_policy(group_id: str, request: Request) -> dict[str, Any]:
    require_group(request, group_id)
    return response(asdict(await request.app.state.store.policy(Destination("group", group_id))))


@admin.patch("/groups/{group_id}/settings")
async def set_policy(group_id: str, payload: PolicyInput, request: Request) -> dict[str, Any]:
    require_group(request, group_id)
    policy = GroupPolicy(**payload.model_dump())
    await request.app.state.store.update_policy(group_id, policy)
    if request.app.state.v2:
        await request.app.state.v2.call("set_policy",group_id,payload.model_dump())
    return response(asdict(policy))


@admin.get("/messages")
async def messages(request: Request, conversation: str | None = None, q: str | None = None,
                   status: str | None = None, cursor: float | None = None,
                   limit: Annotated[int, Query(ge=1, le=100)] = 30) -> dict[str, Any]:
    scope = request.state.principal["groups"]
    if scope is None:
        values = await request.app.state.store.messages(conversation, q, status, cursor, limit)
    else:
        allowed = {"group:"+group for group in scope}
        if conversation and conversation not in allowed:
            raise HTTPException(403, "GROUP_SCOPE_DENIED")
        values = []
        for target in ([conversation] if conversation else sorted(allowed)):
            values.extend(await request.app.state.store.messages(target, q, status, cursor, limit))
        values = sorted(values, key=lambda row: row["created_at"], reverse=True)[:limit]
    return response({"items": values, "next_cursor": values[-1]["created_at"] if len(values) == limit else None})


@admin.get("/messages/{message_id}")
async def detail(message_id: str, request: Request) -> dict[str, Any]:
    value = await request.app.state.store.message_detail(message_id)
    if value is None:
        raise HTTPException(404, "RESOURCE_NOT_FOUND")
    return response(value)


@admin.post("/messages/{message_id}/reviews", status_code=201)
async def review(message_id: str, payload: ReviewInput, request: Request) -> dict[str, Any]:
    try:
        review_id = await request.app.state.store.review(message_id, payload.tags, payload.better_response, payload.note)
    except KeyError as exc:
        raise HTTPException(404, "RESOURCE_NOT_FOUND") from exc
    return response({"id": review_id})


@admin.post("/replays")
async def replay(payload: ReplayInput, request: Request) -> dict[str, Any]:
    value = await request.app.state.store.message_detail(payload.message_id)
    if value is None:
        raise HTTPException(404, "RESOURCE_NOT_FOUND")
    try:
        result = await request.app.state.runtime.replay(value["decision"]["snapshot"])
    except (PermissionError, ValueError) as exc:
        raise HTTPException(403, "REPLAY_SNAPSHOT_UNAVAILABLE_OR_REVOKED") from exc
    return response(result)


@admin.get("/memories")
async def memories(actor_id: str, request: Request) -> dict[str, Any]:
    # Personal memory administration is owner-only, never a public user endpoint.
    return response([asdict(value) for value in await request.app.state.store.own_memories(actor_id)])


@admin.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str, request: Request) -> dict[str, Any]:
    if not await request.app.state.store.delete_memory(memory_id):
        raise HTTPException(404, "RESOURCE_NOT_FOUND")
    if request.app.state.features:
        await request.app.state.features.memory_deleted(memory_id)
    return response({"deleted": True})


@admin.get("/groups/{group_id}/{kind}")
async def resources(group_id: str, kind: str, request: Request) -> dict[str, Any]:
    require_group(request, group_id)
    if kind not in {"notices", "files"}:
        raise HTTPException(404, "RESOURCE_NOT_FOUND")
    return response(await request.app.state.store.resources(group_id, kind))


@admin.post("/groups/{group_id}/{kind}/sync")
async def sync_resources(group_id: str, kind: str, request: Request) -> dict[str, Any]:
    require_group(request, group_id)
    if kind not in {"notices", "files"}:
        raise HTTPException(404, "RESOURCE_NOT_FOUND")
    try:
        result = await request.app.state.resources.sync(group_id, kind)
    except PermissionError as exc:
        raise HTTPException(403, "CAPABILITY_DENIED") from exc
    return response(result)


@admin.post("/groups/{group_id}/daily/{local_date}")
async def daily(group_id: str, local_date: date, request: Request) -> dict[str, Any]:
    require_group(request, group_id)
    policy = await request.app.state.store.policy(Destination("group", group_id))
    return response(await request.app.state.store.daily(group_id, local_date.isoformat(), policy.timezone))


@admin.post("/controls/emergency-stop")
async def pause(payload: PauseInput, request: Request) -> dict[str, Any]:
    await request.app.state.store.set_paused(payload.paused)
    return response({"paused": payload.paused})


@admin.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    scope = request.state.principal["groups"]
    if scope is None:
        return response(await request.app.state.store.stats())
    from collections import Counter
    states = Counter()
    for group in scope:
        values = await request.app.state.store.messages("group:"+group, None, None, None, 10000)
        states.update(row["status"] for row in values)
    return response({"events": None, "outbox": dict(states), "scope": scope})


@admin.get("/system/info")
async def system_info(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return response({
        "version": "0.4.0", "demo_mode": settings.demo_mode,
        "role": request.state.principal["role"], "account": request.state.principal["name"],
        "storage": "volatile_demo" if settings.database_url == "memory://" else (
            "sqlite" if settings.database_url.startswith("sqlite") else "postgresql"
        ),
        "onebot_connected": request.app.state.platform.ready,
        "paused": await request.app.state.store.paused(),
        "model_provider": settings.model_provider, "chat_model": settings.chat_model,
        "max_daily_model_calls": settings.max_daily_model_calls,
        "runtime_mode": settings.runtime_mode,
        "deepseek_web_enabled": settings.deepseek_web_enabled,
        "work_enabled": settings.work_enabled,
        "worker_concurrency": settings.worker_concurrency,
        "task_concurrency": settings.task_concurrency,
        "database_pool_size": settings.database_pool_size,
        "private_enabled": settings.private_enabled,
        "implemented": ["文字聊天", "授权记忆", "自动巩固", "文件全文/向量检索", "多模态", "DeepSeek可见浏览器研究", "隔离Python", "插件运行器", "审批与回放"],
        "disabled": [name for name, enabled in [("多模态服务", settings.media_enabled),
            ("DeepSeek浏览器", settings.deepseek_web_enabled), ("联网研究", settings.research_enabled),
            ("沙箱/第三方插件", settings.sandbox_enabled), ("QQ空间适配器", settings.qzone_enabled)] if not enabled],
    })


@router.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    from osa.interfaces.dashboard import page
    return page()


router.include_router(admin)
