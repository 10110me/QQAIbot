import uuid
import hashlib
import time
from dataclasses import asdict
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field

from osa.domain.entities import Destination, Event
from osa.domain.work import external_question
from osa.domain.features import Record
from osa.interfaces.api import require_admin, require_group, response

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])


def hub(request: Request):
    value = request.app.state.features
    if value is None:
        raise HTTPException(409, "FEATURES_DISABLED")
    return value


class ResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=12000)
    group_id: str | None = None


class SandboxInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=40000)
    timeout: int = Field(default=20, ge=1, le=60)
    group_id: str | None = None


class ScreenshotInput(BaseModel):
    url: str = Field(pattern=r"^https?://", max_length=2000)
    group_id: str | None = None


@router.post("/screenshots")
async def screenshot(payload: ScreenshotInput, request: Request) -> dict:
    features = hub(request)
    if not features.settings.sandbox_enabled:
        raise HTTPException(409, "ISOLATED_SCREENSHOT_DISABLED")
    if payload.group_id:
        require_group(request, payload.group_id)
    job = await features.tasks.submit("screenshot", {"url": payload.url}, actor="system-admin", group=payload.group_id, approval=True)
    return response(asdict(job))


@router.post("/maintenance/cleanup")
async def cleanup(request: Request) -> dict:
    return response(await hub(request).cleanup())


@router.delete("/users/{actor_id}/data")
async def erase_actor(actor_id: str, request: Request) -> dict:
    if not actor_id.isdecimal():
        raise HTTPException(422, "INVALID_QQ_ID")
    return response(await hub(request).erase_actor(actor_id))


class DecisionInput(BaseModel):
    allowed: bool


class BotSkillDecisionInput(BaseModel):
    allowed: bool


class BotProfileInput(BaseModel):
    bot_id: str = Field(pattern=r"^\d{5,20}$")
    group_id: str = Field(pattern=r"^\d+$")
    name: str = Field(default="", max_length=100)


class BotAdminsInput(BaseModel):
    qq_ids: list[str] = Field(default_factory=list, max_length=100)


class TextDocumentInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=500000)
    group_id: str
    public: bool = False
    verified: bool = False
    license: str | None = Field(default=None, max_length=100)


@router.get("/tasks")
async def tasks(request: Request) -> dict:
    return response(await hub(request).tasks.list())


@router.get("/tasks/{task_id}")
async def task_detail(task_id: str, request: Request) -> dict:
    row = await hub(request).repo.get(task_id)
    if not row or not row.kind.startswith("job."):
        raise HTTPException(404, "TASK_NOT_FOUND")
    return response(asdict(row))


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> dict:
    try:
        return response(asdict(await hub(request).tasks.cancel(task_id)))
    except ValueError as exc:
        raise HTTPException(409, "TASK_NOT_CANCELLABLE") from exc


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, request: Request) -> dict:
    try:
        return response(asdict(await hub(request).tasks.retry(task_id)))
    except ValueError as exc:
        raise HTTPException(409, "TASK_NOT_SAFELY_RETRYABLE") from exc


@router.get("/approvals")
async def approvals(request: Request) -> dict:
    return response([asdict(row) for row in await hub(request).repo.find("approval")])


@router.get("/bot-skills")
async def bot_skills(request: Request, group_id: str | None = None, status: str | None = None) -> dict:
    """List observed external-bot skill samples for the WebUI review page."""
    return response([asdict(row) for row in await hub(request).repo.find(
        "bot_skill_observation", group_id=group_id, status=status, limit=500)])


@router.get("/bot-skills/status")
async def bot_skills_status(request: Request) -> dict:
    return response({"enabled": hub(request).bot_skills.enabled,
                     "qq_admin_ids": await hub(request).bot_skills.admin_ids_configured()})


@router.put("/bot-skills/admins")
async def bot_skill_admins(payload: BotAdminsInput, request: Request) -> dict:
    principal = request.state.principal
    if principal["role"] not in {"owner", "admin"}:
        raise HTTPException(403, "BOT_ADMIN_SETTINGS_REQUIRES_ADMIN")
    try:
        return response({"qq_admin_ids": await hub(request).bot_skills.set_admin_ids(payload.qq_ids)})
    except ValueError as exc:
        raise HTTPException(422, "INVALID_BOT_ADMIN_IDS") from exc


@router.get("/bot-skills/profiles")
async def bot_skill_profiles(request: Request, group_id: str | None = None) -> dict:
    destination = Destination("group", group_id) if group_id else None
    rows = await hub(request).bot_skills.profiles(destination, include_rejected=True)
    workflows = await hub(request).repo.find("bot_skill_workflow", group_id=group_id, status="active", limit=500)
    return response({"profiles": [asdict(row) for row in rows], "workflows": [asdict(row) for row in workflows]})


@router.post("/bot-skills/profiles")
async def register_bot_profile(payload: BotProfileInput, request: Request) -> dict:
    if not hub(request).bot_skills.enabled:
        raise HTTPException(409, "BOT_SKILLS_DISABLED")
    require_group(request, payload.group_id)
    principal = request.state.principal
    if principal["role"] not in {"owner", "admin"}:
        raise HTTPException(403, "BOT_REGISTRATION_REQUIRES_ADMIN")
    event = Event(
        id="web-register:" + uuid.uuid4().hex, dedupe_key=uuid.uuid4().hex, adapter_id="web",
        event_type="message.created", destination=Destination("group", payload.group_id),
        actor_id="web-admin", message_id="", text="登记机器人", segments=[], occurred_at=time.time(),
        actor_role="owner")
    return response(asdict(await hub(request).bot_skills.register(event, payload.bot_id, payload.name)))


@router.post("/bot-skills/{skill_id}/decision")
async def bot_skill_decision(skill_id: str, payload: BotSkillDecisionInput, request: Request) -> dict:
    if not hub(request).bot_skills.enabled:
        raise HTTPException(409, "群机器人技能模块已停用，已有记录仅供查看")
    row = await hub(request).repo.get(skill_id)
    if not row or row.kind != "bot_skill_observation":
        raise HTTPException(404, "BOT_SKILL_NOT_FOUND")
    return response(asdict(await hub(request).repo.patch(
        skill_id, status="active" if payload.allowed else "rejected",
        payload={**row.payload, "learning_state": "已启用" if payload.allowed else "已拒绝"})))


@router.post("/approvals/{approval_id}/decision")
async def decide(approval_id: str, payload: DecisionInput, request: Request) -> dict:
    try:
        return response(asdict(await hub(request).tasks.approve(approval_id, payload.allowed)))
    except ValueError as exc:
        raise HTTPException(409, "APPROVAL_ALREADY_HANDLED") from exc


@router.post("/research")
async def research(payload: ResearchInput, request: Request) -> dict:
    features = hub(request)
    if not features.settings.research_enabled or not features.settings.deepseek_web_enabled:
        raise HTTPException(409, "DEEPSEEK_RESEARCH_DISABLED")
    if payload.group_id:
        require_group(request, payload.group_id)
    try:external_question(payload.query)
    except PermissionError:raise HTTPException(400,"RESEARCH_REQUIRES_REDACTION")
    job = await features.tasks.submit("research", {"query": payload.query}, actor="system-admin", group=payload.group_id, priority=5)
    return response(asdict(job))


@router.get("/research/history")
async def research_history(request: Request, group_id: str | None = None) -> dict:
    return response([asdict(row) for row in await hub(request).repo.find("research", group_id=group_id, limit=200)])


@router.post("/sandbox")
async def sandbox(payload: SandboxInput, request: Request) -> dict:
    features = hub(request)
    if not features.settings.sandbox_enabled:
        raise HTTPException(409, "SANDBOX_DISABLED")
    if payload.group_id:
        require_group(request, payload.group_id)
    job = await features.tasks.submit("sandbox", {"code": payload.code, "timeout": payload.timeout}, actor="system-admin",
                                      group=payload.group_id, approval=True, priority=3)
    return response(asdict(job))


@router.get("/knowledge/documents")
async def documents(request: Request, group_id: str | None = None) -> dict:
    return response([asdict(row) for row in await hub(request).repo.find("document", group_id=group_id)])


@router.post("/knowledge/documents")
async def add_document(payload: TextDocumentInput, request: Request) -> dict:
    require_group(request, payload.group_id)
    features = hub(request)
    policy = await features.store.policy(Destination("group", payload.group_id))
    if not policy.file_content_index:
        raise HTTPException(403, "CONTENT_INDEX_PERMISSION_REQUIRED")
    row = await features.knowledge.ingest(payload.title, [{"page": 1, "text": payload.text}], group=payload.group_id,
                                          owner=None, source={"type": "admin_text", "verified": payload.verified,
                                                              "license": payload.license}, public=payload.public)
    return response(asdict(row))


@router.post("/knowledge/upload")
async def upload_document(request: Request, file: UploadFile, group_id: str) -> dict:
    require_group(request, group_id)
    features = hub(request)
    policy = await features.store.policy(Destination("group", group_id))
    if not policy.file_content_index:
        raise HTTPException(403, "CONTENT_INDEX_PERMISSION_REQUIRED")
    raw = await file.read(features.settings.max_download_bytes+1)
    if len(raw) > features.settings.max_download_bytes:
        raise HTTPException(413, "FILE_TOO_LARGE")
    name = (file.filename or "document.txt").replace("\\", "/").rsplit("/", 1)[-1][:200]
    pages = await features.parser.parse(raw, name)
    row = await features.knowledge.ingest(name, pages, group=group_id, owner=None,
                                          source={"type": "admin_upload", "filename": name})
    return response(asdict(row))


@router.delete("/knowledge/documents/{document_id}")
async def delete_document(document_id: str, request: Request) -> dict:
    await hub(request).knowledge.delete(document_id)
    return response({"deleted": True})


@router.post("/groups/{group_id}/files/{file_id}/index")
async def index_file(group_id: str, file_id: str, request: Request) -> dict:
    require_group(request, group_id)
    features = hub(request)
    policy = await features.store.policy(Destination("group", group_id))
    if not policy.file_content_index:
        raise HTTPException(403, "CONTENT_INDEX_PERMISSION_REQUIRED")
    row = await features.tasks.submit("index", {"file_id": file_id}, actor="system-admin", group=group_id)
    return response(asdict(row))


@router.get("/episodes")
async def episodes(request: Request, group_id: str | None = None) -> dict:
    return response([asdict(row) for row in await hub(request).repo.find("episode", group_id=group_id)])


@router.get("/media")
async def media(request: Request, group_id: str | None = None) -> dict:
    return response([asdict(row) for row in await hub(request).repo.find("media", group_id=group_id)])


@router.post("/media/upload")
async def upload_media(request: Request, file: UploadFile, group_id: str, kind: str = "image") -> dict:
    require_group(request, group_id)
    features = hub(request)
    if kind not in {"image", "record", "video"}:
        raise HTTPException(422, "INVALID_MEDIA_KIND")
    raw = await file.read(features.settings.max_download_bytes+1)
    if len(raw) > features.settings.max_download_bytes:
        raise HTTPException(413, "FILE_TOO_LARGE")
    return response(asdict(await features.media.save(raw, file.content_type or "application/octet-stream", kind, group_id, "system-admin")))


@router.post("/media/{asset_id}/analyze")
async def analyze_media(asset_id: str, request: Request, question: str = "描述内容") -> dict:
    return response(await hub(request).media.analyze(asset_id, question[:2000]))


@router.get("/media/{asset_id}/content")
async def media_content(asset_id: str, request: Request) -> Response:
    features = hub(request)
    row = await features.repo.get(asset_id)
    if not row or row.kind != "media" or row.status != "active":
        raise HTTPException(404, "MEDIA_NOT_FOUND")
    content = await features.objects.get(row.payload["object_key"])
    # Download by opaque ID; active HTML/SVG is never served as same-origin executable content.
    return Response(content, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{asset_id}"', "X-Content-Type-Options": "nosniff"})


@router.delete("/media/{asset_id}")
async def delete_media(asset_id: str, request: Request) -> dict:
    await hub(request).media.delete(asset_id)
    return response({"deleted": True})


@router.get("/stickers")
async def stickers(request: Request, group_id: str | None = None) -> dict:
    if group_id:
        require_group(request, group_id)
    rows = await hub(request).repo.find("sticker", limit=1000)
    if group_id:
        rows = [row for row in rows if row.group_id == group_id or row.payload.get("shared") or row.owner_id == "maibot-import"]
    return response([asdict(row) for row in rows])


@router.post("/stickers/{asset_id}")
async def add_sticker(asset_id: str, request: Request, tags: str = "") -> dict:
    asset = await hub(request).repo.get(asset_id)
    if not asset or asset.kind != "media":
        raise HTTPException(404, "MEDIA_NOT_FOUND")
    if asset.group_id:
        require_group(request, asset.group_id)
    return response(asdict(await hub(request).media.add_sticker(asset_id, tags.split(","))))


class StickerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(candidate|active|rejected)$")
    description: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=20)


@router.patch("/stickers/{sticker_id}")
async def update_sticker(sticker_id: str, payload: StickerUpdate, request: Request) -> dict:
    features = hub(request)
    row = await features.repo.get(sticker_id)
    if not row or row.kind != "sticker":
        raise HTTPException(404, "STICKER_NOT_FOUND")
    if row.group_id:
        require_group(request, row.group_id)
    asset = await features.repo.get(row.payload.get("asset_id", ""))
    if payload.status == "active" and (not asset or asset.status != "active"):
        raise HTTPException(409, "MEDIA_NOT_AVAILABLE")
    updated = await features.repo.patch(row.id, status=payload.status, payload={**row.payload,
        "description": payload.description.strip(), "tags": features.media.clean_tags(payload.tags)})
    return response(asdict(updated))


@router.delete("/stickers/{sticker_id}")
async def delete_sticker(sticker_id: str, request: Request) -> dict:
    features = hub(request)
    row = await features.repo.get(sticker_id)
    if not row or row.kind != "sticker":
        raise HTTPException(404, "STICKER_NOT_FOUND")
    if row.group_id:
        require_group(request, row.group_id)
    await features.repo.delete(sticker_id)
    return response({"deleted": True})


@router.get("/stickers/{sticker_id}/thumbnail")
async def sticker_thumbnail(sticker_id: str, request: Request) -> Response:
    features = hub(request)
    row = await features.repo.get(sticker_id)
    if not row or row.kind != "sticker":
        raise HTTPException(404, "STICKER_NOT_FOUND")
    if row.group_id:
        require_group(request, row.group_id)
    asset = await features.repo.get(row.payload.get("asset_id", ""))
    if not asset or asset.kind != "media" or asset.status != "active" or asset.payload.get("type") != "image":
        raise HTTPException(404, "MEDIA_NOT_FOUND")
    from PIL import Image, ImageOps
    raw = await features.objects.get(asset.payload["object_key"])
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 20_000_000:
            raise HTTPException(413, "IMAGE_TOO_LARGE")
        preview = ImageOps.exif_transpose(source).convert("RGBA")
        preview.thumbnail((240, 240))
        output = io.BytesIO()
        preview.save(output, format="WEBP", quality=78)
    return Response(output.getvalue(), media_type="image/webp", headers={
        "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@router.get("/qzone/timeline")
async def timeline(request: Request) -> dict:
    features = hub(request)
    if not features.settings.qzone_enabled or not features.social:
        raise HTTPException(409, "QZONE_ADAPTER_DISABLED")
    return response(await features.social.timeline())


class SocialInput(BaseModel):
    action: str
    arguments: dict = Field(default_factory=dict)


class PluginInput(BaseModel):
    manifest: dict
    source: str = Field(min_length=1, max_length=30000)


class PluginGrantInput(BaseModel):
    grants: list[str]
    groups: list[str]


class PluginRunInput(BaseModel):
    group_id: str
    arguments: dict = Field(default_factory=dict)


class SpeechInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = "alloy"
    group_id: str


@router.post("/media/speech")
async def speech(payload: SpeechInput, request: Request) -> dict:
    if request.app.state.settings.v3_enabled:
        raise HTTPException(409, "TTS_DISABLED_IN_V3")
    require_group(request, payload.group_id)
    features = hub(request)
    if not features.settings.media_enabled:
        raise HTTPException(409, "MEDIA_DISABLED")
    raw = await features.media.provider.speech(payload.text, payload.voice)
    return response(asdict(await features.media.save(raw, "audio/mpeg", "record", payload.group_id, "system-admin")))


@router.get("/plugins")
async def plugins(request: Request) -> dict:
    return response([asdict(row) for row in await hub(request).repo.find("plugin")])


@router.post("/plugins")
async def install_plugin(payload: PluginInput, request: Request) -> dict:
    return response(asdict(await hub(request).plugins.install(payload.manifest, payload.source)))


@router.post("/plugins/{plugin_id}/enable")
async def approve_plugin(plugin_id: str, payload: PluginGrantInput, request: Request) -> dict:
    for group in payload.groups:
        require_group(request, group)
    return response(asdict(await hub(request).plugins.approve(plugin_id, payload.grants, payload.groups)))


@router.post("/plugins/{plugin_id}/disable")
async def disable_plugin(plugin_id: str, request: Request) -> dict:
    features = hub(request)
    row = await features.repo.get(plugin_id)
    if not row or row.kind != "plugin":
        raise HTTPException(404, "PLUGIN_NOT_FOUND")
    return response(asdict(await features.repo.patch(plugin_id, status="disabled")))


@router.post("/plugins/{plugin_id}/run")
async def run_plugin(plugin_id: str, payload: PluginRunInput, request: Request) -> dict:
    require_group(request, payload.group_id)
    features = hub(request)
    if not features.settings.sandbox_enabled:
        raise HTTPException(409, "ISOLATED_PLUGIN_RUNNER_DISABLED")
    return response(await features.plugins.run(plugin_id, Destination("group", payload.group_id), "system-admin", payload.arguments, {}))


@router.get("/metrics/models")
async def model_metrics(request: Request, group_id: str | None = None) -> dict:
    rows = await hub(request).repo.find("model_usage", group_id=group_id, limit=10000)
    return response({"calls": len(rows), "input_tokens": sum(r.payload.get("input_tokens", 0) for r in rows),
        "output_tokens": sum(r.payload.get("output_tokens", 0) for r in rows),
        "failed": sum(r.status == "failed" for r in rows), "items": [asdict(row) for row in rows[:100]],
        "billing_estimate": None})


@router.get("/exports/user/{actor_id}")
async def export_user(actor_id: str, request: Request) -> dict:
    features = hub(request)
    if not actor_id.isdecimal():
        raise HTTPException(422, "INVALID_QQ_ID")
    values = {"memories": [asdict(row) for row in await features.store.own_memories(actor_id)]}
    for kind in ("research", "media", "media_analysis", "memory_evidence"):
        values[kind] = [asdict(row) for row in await features.repo.find(kind, owner_id=actor_id, limit=10000)]
    return response(values)


class ModelRouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str = Field(pattern=r"^(chat|decision|thinking|reply|memory|reasoning)$")
    base_url: str = Field(pattern=r"^https?://[^\s]+$", max_length=1000)
    model: str = Field(min_length=1, max_length=150)
    api_key_env: str = Field(pattern=r"^OSA_(MODEL_API_KEY|PROVIDER_KEY_[A-Z0-9_]+)$")


@router.get("/models")
async def model_routes(request: Request) -> dict:
    return response([asdict(row) for row in await hub(request).repo.find("model_route")])


@router.post("/models")
async def update_model(payload: ModelRouteInput, request: Request) -> dict:
    # API keys are references to environment bindings, not returned/stored secret values.
    await request.app.state.apply_model_route(payload.model_dump())
    features = hub(request)
    key = hashlib.sha256(("model_route:"+payload.purpose).encode()).hexdigest()[:32]
    existing = await features.repo.get(key)
    row = await features.repo.patch(key, payload=payload.model_dump()) if existing else await features.repo.put(
        Record(key, "model_route", "system-admin", None, "active", payload.model_dump(), "model_route:"+payload.purpose))
    return response(asdict(row))


@router.post("/qzone/actions")
async def social_action(payload: SocialInput, request: Request) -> dict:
    if payload.action not in {"post", "comment", "like"}:
        raise HTTPException(422, "INVALID_SOCIAL_ACTION")
    row = await hub(request).tasks.submit("social", payload.model_dump(), actor="system-admin", group=None, approval=True)
    return response(asdict(row))
