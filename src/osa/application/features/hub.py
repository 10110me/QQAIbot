from osa.application.prompt_catalog import CATALOG
import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, replace
from typing import Any

from osa.application.features.tasks import TaskService
from osa.domain.entities import Destination, Event, ModelResult
from osa.domain.features import FeatureRepository, Record
from osa.domain.ports import Store
from osa.domain.work import external_question
from osa.application.chat_stream import BotSkillLearningService, SilentLearningService
from osa.domain.features import ParserRejected

logger=logging.getLogger(__name__)


class FeatureHub:
    def __init__(self, settings: Any, repo: FeatureRepository, store: Store, tasks: TaskService,
                 knowledge, media, research, consolidation, parser, downloader, objects, sandbox,
                 resources, platform, model, social) -> None:
        self.settings, self.repo, self.store, self.tasks = settings, repo, store, tasks
        self.knowledge, self.media, self.research, self.consolidation = knowledge, media, research, consolidation
        self.parser, self.downloader, self.objects, self.sandbox = parser, downloader, objects, sandbox
        self.resources, self.platform, self.model, self.social = resources, platform, model, social
        self.learning = SilentLearningService(repo, store, model)
        self.learning.people.bot_id = settings.bot_id
        self.bot_skills = BotSkillLearningService(repo, store, settings.bot_id,
                                                enabled=settings.bot_skills_enabled,
                                                admin_ids=set(getattr(settings, "qq_admin_ids", [])),
                                                platform=platform)
        self.media_inputs: dict[str, list[dict]] = {}
        self.media_events: dict[str, Event] = {}
        self.media_times: dict[str, float] = {}
        from osa.application.features.image_context import ImageContext
        self.images=ImageContext(self)
        tasks.handlers.update({"job.research": self.run_research, "job.sandbox": self.run_sandbox,
            "job.index": self.run_index, "job.consolidate": consolidation.run, "job.sync": self.run_sync,
            "job.media": self.run_media, "job.social": self.run_social})
        tasks.cancel_callbacks["job.research"] = research.cancel
        tasks.cancel_callbacks["job.sandbox"] = self.cancel_sandbox

    async def bot_skill_context(self, destination: Destination) -> dict[str, Any]:
        return await self.bot_skills.prompt_context(destination)

    async def observe(self, event: Event, raw_media: list[dict] | None = None) -> None:
        if raw_media:
            self.media_inputs[event.id] = raw_media[:3]
            self.media_times[event.id] = time.time()
            self.media_events[event.id] = replace(event, text='', segments=[])
            self.images.register(event,raw_media)
        await self.learning.people.observe(event, await self.store.policy(event.destination))
        if event.destination.kind == "group" and event.event_type == "message.created":
            await self.bot_skills.observe(event, await self.store.policy(event.destination))
            await self.learning.observe(event, await self.store.policy(event.destination))
            key = hashlib.sha256(("thread:" + event.destination.id).encode()).hexdigest()[:32]
            old = await self.repo.get(key)
            if old and (old.payload["last_event_id"] == event.id or old.payload["time"] > event.occurred_at):
                return
            payload = {"last_event_id": event.id, "last_actor": event.actor_id,
                       "version": old.payload.get("version", 0)+1 if old else 1, "time": event.occurred_at}
            if old:
                await self.repo.patch(key, payload=payload)
            else:
                await self.repo.put(Record(key, "thread", None, event.destination.id, "active", payload))

    async def deepseek_attachments(self, event: Event, *, include_images: bool = True) -> list[dict]:
        """Resolve transient QQ media into bounded upload payloads."""
        image_attachments=await self.images.attachments(event) if include_images else []
        if hasattr(self, 'work_inputs'):
            # Only attach media from this message or an explicit reply target.
            # The old implementation used a broad recent() window, which could
            # send an unrelated screenshot from the same group to DeepSeek.
            try:
                await self.work_inputs.capture_media(event)
            except Exception as exc:
                # Optional media preparation cannot cancel a direct request.
                # Keep good images, and let thinking explain unavailable inputs.
                logger.warning('consultation_attachment_failed event_id=%s error_type=%s',event.id,type(exc).__name__)
            rows = await self.work_inputs.recent(event)
            allowed_sources = {event.id}
            if event.reply_to_id:
                target=await self.store.event_by_message(event.destination,event.reply_to_id)
                if target:allowed_sources.add(target.id)
            rows = [row for row in rows if row.status == 'ready' and row.payload.get('source_id') in allowed_sources]
            result = []
            import base64
            for row in rows[:4]:
                # Original images were resolved above, without a file byte cap.
                if row.payload.get('mime_type','').startswith('image/'):continue
                raw = await self.work_inputs.features.objects.get(row.payload['object_key'])
                if len(raw) <= 5_000_000:
                    result.append({'name': row.payload.get('name', 'attachment'),
                                   'mime_type': row.payload.get('mime_type', 'application/octet-stream'),
                                   'data_base64': base64.b64encode(raw).decode('ascii')})
            # ImageContext resolves the true quoted message ID, including an
            # image posted by another member in the same group.
            seen={hashlib.sha256(base64.b64decode(x['data_base64'])).hexdigest() for x in image_attachments}
            return image_attachments+[x for x in result if hashlib.sha256(base64.b64decode(x['data_base64'])).hexdigest() not in seen]
        items = self.media_inputs.get(event.id, [])
        result: list[dict] = []
        for item in items[:4]:
            if not include_images and item.get('kind') == 'image':continue
            try:
                content, mime, _ = await self.downloader.fetch(item["url"], max_bytes=5_000_000)
                import base64
                result.append({"name": "qq-" + str(item.get("kind", "file")),
                               "mime_type": mime, "data_base64": base64.b64encode(content).decode("ascii")})
            except Exception:
                continue
        if hasattr(self, "work_inputs"):
            try:
                for row in (await self.work_inputs.recent(event))[:4-len(result)]:
                    if row.status != "ready" or not row.payload.get("object_key"):
                        continue
                    content = await self.objects.get(row.payload["object_key"])
                    if len(content) <= 5_000_000:
                        result.append({"name": row.payload.get("name", "attachment"),
                                       "mime_type": "application/octet-stream",
                                       "data_base64": base64.b64encode(content).decode("ascii")})
            except Exception:
                pass
        return result

    async def initiative_eligible(self, event: Event) -> bool:
        group = await self.store.policy(event.destination)
        if not group.initiative or event.destination.kind != "group" or not group.collect_messages:
            return False
        day = time.strftime("%Y-%m-%d", time.gmtime())
        recent = await self.repo.find("initiative", group_id=event.destination.id, limit=100)
        if any(time.time()-row.created_at < group.initiative_min_interval for row in recent):
            return False
        if group.initiative_daily_limit > 0 and sum(row.payload.get("day") == day for row in recent) >= group.initiative_daily_limit:
            return False
        return True

    async def consider_social(self, event: Event) -> bool:
        if not await self.initiative_eligible(event):
            return False
        day = time.strftime("%Y-%m-%d", time.gmtime())
        context = await self.store.context(event, 8)
        result = await self.model.generate([
            {"role": "system", "content": CATALOG['social_decision']['default']},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ], "decision")
        data = json.loads(result.text)
        if data.get("action") != "REPLY" or float(data.get("confidence", 0)) < 0.85:
            return False
        await self.repo.put(Record(uuid.uuid4().hex, "initiative", None, event.destination.id,
                                   "candidate", {"day": day, "source_id": event.id}))
        return True

    async def route(self, event: Event) -> str | ModelResult | None:
        text = event.text
        group = event.destination.id if event.destination.kind == "group" else None
        policy = await self.store.policy(event.destination)
        payload = {"event": event.to_dict(), "source_event_id": event.id}
        if text in {"/记忆 开启向量", "/记忆 撤销向量"}:
            key = hashlib.sha256(("vector_consent:"+event.actor_id).encode()).hexdigest()[:32]
            consent = await self.repo.get(key)
            enabled = text.endswith("开启向量")
            if consent:
                await self.repo.patch(key, status="active" if enabled else "revoked")
            else:
                await self.repo.put(Record(key, "vector_consent", event.actor_id, None, "active" if enabled else "revoked", {}))
            if not enabled:
                for index in await self.repo.find("memory_index", owner_id=event.actor_id, limit=10000):
                    await self.repo.delete(index.id)
            return "已允许将你的本人记忆发送给配置的 Embedding 服务建立向量索引。" if enabled else "已撤销向量授权并删除对应向量索引。"
        if text == "/隐私 删除我的数据":
            result = await self.erase_actor(event.actor_id, event.id)
            return f"已清理当前数据库中你的原始消息正文、个人记忆、研究和媒体记录，并撤销跨群授权（{result['sources']} 条源记录）。已发送到 QQ 的内容及独立备份需要由部署者按保留策略处理。"
        if text.startswith(("/任务 ", "/取消任务 ")):
            task_id = text.split(" ", 1)[1].strip()
            job = await self.repo.get(task_id)
            if not job or not job.kind.startswith("job.") or job.owner_id != event.actor_id or job.group_id != group:
                return "未找到你在当前会话中的任务。"
            if text.startswith("/取消任务 "):
                await self.tasks.cancel(task_id)
                return "已请求取消任务。"
            return f"任务 {job.id}：{job.status}\n" + str(job.payload.get("external_status") or job.payload.get("error") or "")
        if text.startswith("/插件 "):
            if not self.settings.sandbox_enabled or not group:
                return "当前未启用隔离插件，或不在群会话中。"
            parts = text.split(" ", 2)
            arguments = json.loads(parts[2]) if len(parts) > 2 else {}
            records = await self.repo.find("plugin", status="enabled", limit=1000)
            plugin = next((r for r in records if r.id == parts[1] or r.payload["manifest"]["id"] == parts[1]), None)
            if not plugin:
                return "插件不存在或未启用。"
            context = await self.store.context(event, 10)
            result = await self.plugins.run(plugin.id, event.destination, event.actor_id, arguments, {"messages": context})
            return json.dumps(result["output"], ensure_ascii=False)[:2000]
        if text.startswith("/表情 "):
            if not group or not policy.media_read:
                return "当前会话未授权使用表情素材。"
            tag = text[4:].strip()
            sticker = await self.media.select_sticker(group, tag)
            if not sticker:
                return "当前群未找到符合描述的已启用表情。"
            return ModelResult("", "builtin", "group-sticker", attachment_ids=[sticker.payload["asset_id"]])
        if text.startswith("/语音 "):
            if getattr(self.settings, "v3_enabled", False):
                return "V3 未启用语音合成；可发送语音或视频进行已授权的音轨理解。"
            if not self.settings.media_enabled or (group and not policy.media_read):
                return "当前未授权或未启用语音服务。"
            raw = await self.media.provider.speech(text[4:])
            asset = await self.media.save(raw, "audio/mpeg", "record", group, event.actor_id)
            return ModelResult("语音已生成。", "workflow", "tts", attachment_ids=[asset.id])
        if text.startswith("/研究 "):
            if getattr(self.settings, "v3_enabled", False):
                return None  # V3 Thinking delegates and resumes through the durable bus.
            if not self.settings.research_enabled or not self.settings.deepseek_web_enabled or (group and not policy.research):
                return "当前未授权联网研究或未启用 DeepSeek 浏览器。请由管理员配置，并在浏览器中完成首次登录。"
            try:external_question(text[4:].strip())
            except PermissionError:return "研究问题包含身份、凭据、路径或 URL，请改写成去身份化的通用技术问题。"
            job = await self.tasks.submit("research", {**payload, "query": text[4:].strip()}, actor=event.actor_id,
                group=group, dedupe="research:"+event.id, priority=5)
            return f"研究已排队，所有研究将经过 DeepSeek 网页。任务：{job.id}；需要扫码或验证时会在后台提示。"
        if text.startswith("/运行 Python\n"):
            if not self.settings.sandbox_enabled or (group and not policy.sandbox):
                return "当前未启用或未授权隔离 Python 执行。"
            code = text.split("\n", 1)[1]
            job = await self.tasks.submit("sandbox", {**payload, "code": code, "timeout": 20}, actor=event.actor_id,
                group=group, dedupe="sandbox:"+event.id, approval=True, priority=3)
            return f"已创建代码任务 {job.id}，等待管理员审批；通过后在无网络的独立容器中执行。"
        if text.startswith("/截图 "):
            if not self.settings.sandbox_enabled or (group and not policy.sandbox):
                return "当前未启用或未授权隔离浏览器截图。"
            job = await self.tasks.submit("screenshot", {**payload, "url": text[4:].strip()}, actor=event.actor_id,
                group=group, dedupe="screenshot:"+event.id, approval=True, priority=3)
            return f"截图任务 {job.id} 已创建，等待管理员审批；不会使用 DeepSeek 登录会话。"
        if event.id in self.media_inputs:
            if not self.settings.media_enabled or (group and not policy.media_read):
                self.media_inputs.pop(event.id, None)
                return "当前未开启或未授权图片/音视频理解。"
            job = await self.tasks.submit("media", payload, actor=event.actor_id, group=group,
                dedupe="media:"+event.id, priority=4)
            return f"媒体分析已排队，任务：{job.id}。"
        if self.settings.deepseek_trigger in {"freshness", "always"} and self.settings.research_enabled:
            keywords = ("最新", "联网", "搜索", "查一下", "新闻", "最近发布", "当前版本")
            if any(word in text for word in keywords):
                return await self.route(Event.from_dict({**event.to_dict(), "text": "/研究 " + text}))
        return None

    async def evidence(self, event: Event, *, use_embeddings: bool = True) -> dict:
        group = event.destination.id if event.destination.kind == "group" else None
        policy = await self.store.policy(event.destination)
        chunks = [] if (group and not policy.file_content_index) else await self.knowledge.retrieve(event.text, group, event.actor_id, use_embeddings=use_embeddings)
        # Shared candidates are explicitly marked as unverified.  They can be
        # reused across groups without exposing their source chat or members.
        shared = await self.learning.recall(event.destination, event.text)
        all_items = (chunks or []) + shared[:8]
        members, sources = await self.learning.people.members(event)
        return {'knowledge':all_items, 'participants':members, '_name_source_ids':sources}

    async def rank_memories(self, event: Event, memories: list, *, use_embeddings: bool = True) -> list:
        embedder = self.knowledge.embedder
        if not use_embeddings or not embedder or not memories:
            return memories[:8]
        index_to_memory = {}
        for memory in memories:
            if not memory.subject_id:
                continue
            consent_id = hashlib.sha256(("vector_consent:"+memory.subject_id).encode()).hexdigest()[:32]
            consent = await self.repo.get(consent_id)
            if not consent or consent.status != "active":
                continue
            index_id = hashlib.sha256(("memory_vector:"+memory.id+":"+embedder.model_id).encode()).hexdigest()[:32]
            old = await self.repo.get(index_id)
            if not old:
                old = await self.repo.put(Record(index_id, "memory_index", memory.subject_id, memory.origin_group_id, "active",
                    {"memory_id": memory.id, "version": memory.version, "source_id": memory.source_event_id}))
                vector = (await embedder.embed([memory.content]))[0]
                await self.repo.vector_put(index_id, embedder.model_id, vector)
            elif old.payload.get("version") != memory.version:
                vector = (await embedder.embed([memory.content]))[0]
                await self.repo.vector_put(index_id, embedder.model_id, vector)
                await self.repo.patch(index_id, payload={**old.payload, "version": memory.version})
            index_to_memory[index_id] = memory.id
        if not index_to_memory:
            return memories[:8]
        query = (await embedder.embed([event.text]))[0]
        scores = await self.repo.vector_scores(list(index_to_memory), embedder.model_id, query)
        vector_order = {index_to_memory[key]: rank for rank, (key, _) in enumerate(sorted(scores.items(), key=lambda x:x[1], reverse=True),1)}
        keyword_order = {memory.id: i for i,memory in enumerate(memories,1)}
        return sorted(memories, key=lambda m: 1/(60+keyword_order[m.id]) + (1/(60+vector_order[m.id]) if m.id in vector_order else 0), reverse=True)[:8]

    async def memory_deleted(self, memory_id: str, actor: str | None = None) -> None:
        for row in await self.repo.find("memory_index", owner_id=actor, limit=10000):
            if row.payload["memory_id"] == memory_id:
                await self.repo.delete(row.id)

    async def allowed(self, snapshot: dict, destination: Destination, actor: str) -> bool:
        policy = await self.store.policy(destination)
        for knowledge_id in snapshot.get('knowledge_ids', []):
            row = await self.repo.get(knowledge_id)
            if not row or row.status not in {'candidate', 'shared'}:
                return False
            if (row.status != 'shared' and row.group_id != (destination.id if destination.kind == 'group' else None)
                    and not self.store.permissions.shares_group_memory(row.group_id, destination)):
                return False
            origin = row.payload.get('origin_group_id')
            if origin and not (await self.store.policy(Destination('group', origin))).collect_messages:
                return False
            if any([not await self.store.event_active(sid) for sid in row.payload.get('source_ids', [])]):
                return False
        if snapshot.get('input_ids') and destination.kind == 'group' and not policy.file_content_index:
            return False
        for input_id in snapshot.get('input_ids', []):
            row = await self.repo.get(input_id)
            if not row or row.status != 'ready' or row.owner_id != actor or row.group_id != (destination.id if destination.kind == 'group' else None):
                return False
            if not await self.store.event_active(row.payload['source_id']):
                return False
        for document_id, version in snapshot.get("document_versions", {}).items():
            doc = await self.repo.get(document_id)
            if not doc or doc.status != "active" or doc.payload.get("version") != version:
                return False
            if not doc.payload.get("public"):
                if destination.kind == "group" and (doc.group_id != destination.id or not policy.file_content_index):
                    return False
                if destination.kind == "private" and (doc.group_id is not None or doc.owner_id != actor):
                    return False
        capability = snapshot.get("task_capability")
        if destination.kind == "group" and capability and not getattr(policy, capability, False):
            return False
        if snapshot.get("initiative_source"):
            # A living group inevitably receives messages during generation.
            # New messages alone do not revoke an otherwise valid reply.
            # Freshness is checked by the reply model with latest context and
            # the bounded outbox deadline; revocation remains enforced here.
            if not policy.initiative or not policy.collect_messages:
                return False
            if not await self.store.event_active(snapshot['initiative_source']):
                return False
        return True

    async def attachments(self, ids: list[str], destination: Destination, actor: str) -> list[dict]:
        result = []
        for asset_id in ids[:3]:
            asset = await self.repo.get(asset_id)
            if not asset or asset.kind != "media" or asset.status != "active" or asset.payload["type"] not in {"image", "record"}:
                raise PermissionError("Attachment unavailable")
            if destination.kind == "group":
                shared_sticker = asset.payload["type"] == "image" and any(
                    row.payload.get("asset_id") == asset_id and row.payload.get("shared")
                    for row in await self.repo.find("sticker", private_only=True, status="active", limit=10000)
                )
                imported_global = (
                    asset.group_id is None
                    and asset.owner_id == "maibot-import"
                    and asset.payload.get("imported_from") == "maibot"
                )
                if (asset.group_id != destination.id and not imported_global and not shared_sticker) or not (
                    await self.store.policy(destination)
                ).media_read:
                    raise PermissionError("Cross-group attachment denied")
            elif asset.group_id is not None or asset.owner_id != actor:
                raise PermissionError("Private attachment scope denied")
            raw = await self.objects.get(asset.payload["object_key"])
            if len(raw) > 5_000_000:
                raise ValueError("Outgoing media too large")
            result.append({"type": asset.payload["type"], "content": base64.b64encode(raw).decode()})
        return result

    async def check_job(self, job: Record, capability: str) -> Event | None:
        current = await self.repo.get(job.id)
        if current is None or current.status == "cancelled" or await self.store.paused():
            raise PermissionError("Job cancelled or bot paused")
        event = Event.from_dict(job.payload["event"]) if job.payload.get("event") else None
        if job.group_id:
            policy = await self.store.policy(Destination("group", job.group_id))
            if not policy.enabled or not getattr(policy, capability, False):
                raise PermissionError("Group permission revoked")
        if event and not await self.store.event_active(event.id):
            raise PermissionError("Source recalled")
        return event

    async def run_research(self, job: Record) -> dict:
        event = await self.check_job(job, "research")
        result = await self.research.run(job)
        if event:
            await self.check_job(job, "research")
            text = result["answer"][:3500] + "\n网页模型回答，尚未独立核实。研究来源：DeepSeek 网页；时间：" + time.strftime("%Y-%m-%d %H:%M", time.localtime())
            await self.store.emit_result(event, job.id, text, {"task_capability": "research"})
        return result

    async def run_sandbox(self, job: Record) -> dict:
        if not self.settings.sandbox_enabled:
            raise PermissionError("Sandbox disabled")
        event = await self.check_job(job, "sandbox")
        approval = await self.repo.get(job.payload.get("approval_id", ""))
        if not approval or approval.status not in {"approved", "consumed"} or approval.payload["job_id"] != job.id:
            raise PermissionError("Matching approval required")
        await self.repo.patch(approval.id, status="consumed")
        current = await self.repo.get(job.id)
        await self.repo.patch(job.id, payload={**current.payload, "remote_id": job.id})
        result = await self.sandbox.run("python", {"code": job.payload["code"], "timeout": job.payload.get("timeout", 20)}, job.id)
        artifacts = []
        for artifact in result.pop("artifacts", []):
            raw = base64.b64decode(artifact["content"], validate=True)
            row = await self.media.save(raw, "application/octet-stream", "artifact", job.group_id, job.owner_id)
            artifacts.append({"asset_id": row.id, "name": artifact["name"]})
        result["artifacts"] = artifacts
        if event:
            await self.check_job(job, "sandbox")
            await self.store.emit_result(event, job.id, f"执行结果：{result['status']}\n{result.get('stdout','')[:3000]}\n附件可在管理后台任务详情中下载。", {"task_capability": "sandbox"})
        return result

    async def cancel_sandbox(self, job: Record) -> None:
        await self.sandbox.cancel(job.id)

    async def run_screenshot(self, job: Record) -> dict:
        if not self.settings.sandbox_enabled:
            raise PermissionError("Isolated screenshot disabled")
        event = await self.check_job(job, "sandbox")
        approval = await self.repo.get(job.payload.get("approval_id", ""))
        if not approval or approval.status != "approved":
            raise PermissionError("Screenshot approval required")
        await self.repo.patch(approval.id, status="consumed")
        result = await self.screenshots.screenshot(job.payload["url"])
        raw = base64.b64decode(result["content"], validate=True)
        asset = await self.media.save(raw, "image/png", "image", job.group_id, job.owner_id)
        if event:
            await self.check_job(job, "sandbox")
            await self.store.emit_result(event, job.id, "网页截图已完成。", {"task_capability": "sandbox", "attachment_ids": [asset.id]})
        return {"asset_id": asset.id, "url": job.payload["url"]}

    async def run_index(self, job: Record) -> dict:
        if not self.settings.group_file_download_enabled:
            return {'status':'disabled','note':'群文件下载与自动索引已临时暂停，未获取文件'}
        await self.check_job(job, "file_content_index")
        if not job.group_id:
            raise PermissionError("Group file indexing requires a group")
        file_id = job.payload["file_id"]
        values = await self.store.resources(job.group_id, "files")
        metadata = next((row["payload"] for row in values if row["platform_id"] == file_id), None)
        if not metadata or metadata["size"] > self.settings.max_download_bytes:
            raise ValueError("File missing or too large")
        try:
            raw = await self.platform.call("get_group_file_url", {"group_id": int(job.group_id), "file_id": file_id})
        except Exception:
            try:
                raw = await self.platform.call("get_file", {"file_id": file_id})
            except Exception:
                raw = await self.platform.call("get_file", {"file": file_id})
        url = raw.get("url") or raw.get("file_url") if isinstance(raw, dict) else None
        if not url:
            raise ValueError("NapCat 未返回群文件下载地址")
        content, mime, _ = await self.downloader.fetch(url)
        try:
            pages = await self.parser.parse(content, metadata["name"])
        except ParserRejected:
            # Unsupported/unsafe parser input is a terminal result.  Keep the
            # file metadata available for manual download and avoid retrying a
            # deterministic 422 response every few seconds.
            return {"status": "unsupported", "filename": metadata["name"], "reason": "parser_rejected"}
        await self.check_job(job, "file_content_index")
        document = await self.knowledge.ingest(metadata["name"], pages, group=job.group_id, owner=None,
            source={"type": "group_file", "file_id": file_id, "group_id": job.group_id, "indexed_at": time.time()})
        return {"document_id": document.id, "pages": len(pages), "filename": metadata["name"]}

    async def run_media(self, job: Record) -> dict:
        event = await self.check_job(job, "media_read")
        items = self.media_inputs.pop(job.payload["source_event_id"], None)
        if not items:
            raise ValueError("Transient media URL expired; please upload again")
        results = []
        for item in items:
            asset = await self.media.import_media(item["url"], item["kind"], job.group_id, job.owner_id)
            results.append(await self.media.analyze(asset.id, event.text if event else ""))
        if event:
            await self.check_job(job, "media_read")
            await self.store.emit_result(event, job.id, "\n\n".join(row["text"] for row in results)[:3500], {"task_capability": "media_read"})
        return {"analyses": results}

    async def run_sync(self, job: Record) -> dict:
        values = await self.resources.sync(job.group_id, job.payload["resource_kind"])
        if self.settings.group_file_download_enabled and job.payload["resource_kind"] == "files" and (await self.store.policy(Destination("group", job.group_id))).file_content_index:
            for file in values:
                digest = hashlib.sha256(json.dumps(file["payload"], sort_keys=True).encode()).hexdigest()
                await self.tasks.submit("index", {"file_id": file["platform_id"]}, actor=None, group=job.group_id,
                    dedupe=f"index:{job.group_id}:{file['platform_id']}:{digest}", priority=-2)
        return {"count": len(values)}

    async def run_social(self, job: Record) -> dict:
        if not self.settings.qzone_enabled or self.social is None:
            raise PermissionError("QQ Space adapter disabled")
        await self.check_job(job, "enabled")
        approval = await self.repo.get(job.payload.get("approval_id", ""))
        if not approval or approval.status != "approved":
            raise PermissionError("Social write approval required")
        await self.repo.patch(approval.id, status="consumed")
        return await self.social.execute(job.payload["action"], job.payload.get("arguments", {}), job.id)

    async def recalled(self, event: Event) -> None:
        self.media_inputs.pop(event.id, None)
        self.media_events.pop(event.id, None)
        if event.destination.kind == "group":
            for row in await self.repo.find("sticker", limit=10000):
                origin_group = row.payload.get("origin_group_id", row.group_id)
                if origin_group == event.destination.id and row.payload.get("source_message_id") == event.message_id:
                    await self.media.delete(row.payload["asset_id"])
        for kind in self.tasks.handlers:
            for job in await self.repo.find(kind, group_id=event.destination.id if event.destination.kind == "group" else None, limit=1000):
                source = job.payload.get("event", {})
                if source.get("message_id") == event.message_id and source.get("destination") == asdict(event.destination) and job.status not in {"succeeded", "failed", "cancelled"}:
                    await self.tasks.cancel(job.id)
        for kind in ("episode", "memory_evidence", "memory_index", "knowledge_candidate"):
            for row in await self.repo.find(kind, group_id=event.destination.id, limit=10000):
                sources = row.payload.get("source_ids", [row.payload["source_id"]] if row.payload.get("source_id") else [])
                invalid = [sid for sid in sources if not await self.store.event_active(sid)]
                if invalid:
                    if kind == "memory_index":
                        await self.repo.delete(row.id)
                        continue
                    if kind == "memory_evidence":
                        await self.store.delete_memory(row.payload["memory_id"])
                    await self.repo.patch(row.id, status="invalidated", payload={"source_ids": sources})

    async def scheduler(self) -> None:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        while True:
            try:
                await self.learning.flush_idle()
                for event_id, captured in list(self.media_times.items()):
                    if time.time()-captured > 900:
                        self.media_inputs.pop(event_id, None)
                        self.media_events.pop(event_id, None)
                        self.media_times.pop(event_id, None)
                for pending in await self.repo.find("research", status="deletion_pending", limit=20):
                    try:
                        await self.research.browser.request("DELETE", "/tasks/"+pending.payload["browser_request_id"])
                        await self.repo.delete(pending.id)
                    except Exception:
                        pass
                cleanup_key = "retention:" + time.strftime("%Y-%m-%d", time.gmtime())
                marker = hashlib.sha256(cleanup_key.encode()).hexdigest()[:32]
                if await self.repo.get(marker) is None:
                    await self.cleanup()
                    await self.repo.put(Record(marker, "maintenance", None, None, "succeeded", {}, cleanup_key))
                for group in self.settings.allowed_groups:
                    policy = await self.store.policy(Destination("group", group))
                    if not policy.enabled:
                        continue
                    local = datetime.now(ZoneInfo(policy.timezone))
                    if policy.memory_extract and local.hour >= self.settings.nightly_hour:
                        for days in range(1, self.settings.retention_days+1):
                            date = (local.date()-timedelta(days=days)).isoformat()
                            await self.tasks.submit("consolidate", {"date": date}, actor=None, group=group,
                                dedupe=f"consolidate:{group}:{date}:v1", priority=-5)
                    interval = int(time.time()//600)
                    for kind, enabled in (("notices", policy.notice_read), ("files", policy.file_metadata_read)):
                        if enabled and self.platform.ready:
                            await self.tasks.submit("sync", {"resource_kind": kind}, actor=None, group=group,
                                dedupe=f"sync:{group}:{kind}:{interval}", priority=-3)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(30)

    async def cleanup(self) -> dict:
        cutoff = time.time()-self.settings.retention_days*86400
        sources = await self.store.purge_raw(cutoff)
        approved_assets = {row.payload.get("asset_id") for row in
                           await self.repo.find("sticker", status="active", limit=10000)}
        for kind in ("research", "episode", "media", "image_context", "image_description", "work_input", "work_input_binding", "learning_buffer", "learning_batch"):
            for row in await self.repo.find(kind, before=cutoff, limit=10000):
                if kind == "media" and row.status == "active":
                    if row.id not in approved_assets:
                        await self.media.delete(row.id)
                else:
                    if kind in {'work_input','image_context'} and row.payload.get('object_key'):
                        await self.objects.delete(row.payload['object_key'])
                    if kind == "research":
                        try:
                            await self.research.browser.request("DELETE", "/tasks/"+row.payload["browser_request_id"])
                        except Exception:
                            continue  # Keep a retryable record until the worker can clear its cache.
                    await self.repo.delete(row.id)
        for kind in self.tasks.handlers:
            for row in await self.repo.find(kind, before=cutoff, limit=10000):
                if row.status in {"succeeded", "failed", "cancelled", "rejected", "unknown"}:
                    await self.repo.patch(row.id, payload={"retained_status": row.status, "redacted": True})
        return {"sources": len(sources)}

    async def erase_actor(self, actor: str, except_id: str | None = None) -> dict:
        sources = await self.store.purge_raw(time.time()+1, actor, except_id)
        for kind in self.tasks.handlers:
            for job in await self.repo.find(kind, owner_id=actor, limit=10000):
                if job.status not in {"succeeded", "failed", "cancelled", "rejected", "unknown"}:
                    await self.tasks.cancel(job.id)
                await self.repo.patch(job.id, payload={"redacted": True})
        for kind in ("research", "media", "media_analysis", "image_context", "image_description", "memory_evidence", "memory_index", "vector_consent", "work_input", "work_input_binding"):
            for row in await self.repo.find(kind, owner_id=actor, limit=10000):
                if kind == "media" and row.status == "active":
                    await self.media.delete(row.id)
                else:
                    if kind in {'work_input','image_context'} and row.payload.get('object_key'):
                        await self.objects.delete(row.payload['object_key'])
                    if kind == "research":
                        try:
                            await self.research.browser.request("DELETE", "/tasks/"+row.payload["browser_request_id"])
                        except Exception:
                            await self.repo.patch(row.id, status="deletion_pending", payload={"browser_request_id": row.payload.get("browser_request_id"), "redacted": True})
                            continue
                    await self.repo.delete(row.id)
        for row in await self.repo.find("episode", limit=10000):
            if set(sources).intersection(row.payload.get("source_ids", [])):
                await self.repo.patch(row.id, status="invalidated", payload={"source_ids": row.payload.get("source_ids", [])})
        return {"sources": len(sources)}
