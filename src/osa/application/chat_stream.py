"""Unified conversation stream and silent learning pipeline.

The stream owns message batching.  Replying is only one possible consumer;
learning is deliberately fed from inbound messages even when the agent stays
silent.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from osa.domain.entities import Destination, Event, Memory, Scope
from osa.domain.features import FeatureRepository, Record
from osa.domain.ports import ModelProvider, Store
from osa.application.prompt_catalog import CATALOG
from osa.application.memory_dedupe import duplicate_memory


@dataclass
class StreamState:
    key: str
    messages: list[Event] = field(default_factory=list)
    version: int = 0
    last_event_at: float = 0.0
    turn_running: bool = False

    def append(self, event: Event, limit: int = 80) -> None:
        if any(item.id == event.id for item in self.messages):
            return
        self.messages.append(event)
        self.messages = self.messages[-limit:]
        self.version += 1
        self.last_event_at = event.occurred_at

    def context(self, limit: int = 24) -> list[dict[str, Any]]:
        from osa.application.message_context import render_segments
        return [{"id": item.id, "actor_id": item.actor_id, "actor_name":item.actor_name, "text": item.text,
                 "content": render_segments(item.segments), "segments": item.segments,
                 "message_id": item.message_id, "occurred_at": item.occurred_at,
                 "reply_to_id": item.reply_to_id, "mentioned": item.mentioned}
                for item in self.messages[-limit:] if item.event_type == "message.created"]


class ChatStreamCoordinator:
    def __init__(self, *, context_limit: int = 40) -> None:
        self.context_limit = context_limit
        self.streams: dict[str, StreamState] = {}
        self._lock = asyncio.Lock()

    async def observe(self, event: Event) -> StreamState:
        async with self._lock:
            state = self.streams.setdefault(event.destination.key, StreamState(event.destination.key))
            state.append(event, self.context_limit * 2)
            return state

    async def recalled(self, event):
        async with self._lock:
            state=self.streams.get(event.destination.key)
            if state:
                before=len(state.messages)
                state.messages=[m for m in state.messages if m.message_id != event.message_id]
                if len(state.messages)!=before:state.version+=1

    async def snapshot(self, destination: Destination, limit: int | None = None) -> dict[str, Any]:
        state = self.streams.get(destination.key)
        if state is None:
            return {"stream_id": destination.key, "version": 0, "messages": []}
        return {"stream_id": state.key, "version": state.version,
                "messages": state.context(limit or self.context_limit)}


class BotSkillLearningService:
    """Register, probe and learn external QQ bot interaction workflows."""

    COMMAND_WORDS = ("help", "帮助", "用法", "指令", "命令", "菜单")
    PRIORITY_ACTION_WORDS = ("禁言", "解禁", "解除禁言", "永久禁言", "踢出", "封禁", "撤回")

    def __init__(self, repo: FeatureRepository, store: Store, bot_id: str, *, ttl: float = 180.0,
                 enabled: bool = False, admin_ids: set[str] | None = None, platform=None) -> None:
        self.repo, self.store, self.bot_id, self.ttl = repo, store, str(bot_id), ttl
        self.flow_ttl = max(600.0, ttl)
        self.enabled = enabled
        self.admin_ids = {str(value) for value in (admin_ids or set())}
        self.platform = platform
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._last_bot_reply: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._probe_lock = asyncio.Lock()

    @staticmethod
    def _flow_id(group_id: str) -> str:
        return hashlib.sha256(("bot-flow:" + str(group_id)).encode()).hexdigest()

    async def _flow(self, group_id: str, now: float | None = None) -> Record | None:
        row = await self.repo.get(self._flow_id(group_id))
        if not row or row.status != "active":
            return None
        if row.payload.get("guard_version") != 2:
            await self.repo.patch(row.id, status="expired")
            return None
        now = time.time() if now is None else now
        if now > float(row.payload.get("expires_at", 0)):
            await self.repo.patch(row.id, status="expired")
            return None
        return row

    async def should_hold(self, event: Event) -> bool:
        """Keep Nova silent while a registered bot owns a multi-message flow."""
        if not self.enabled or event.destination.kind != "group":
            return False
        if self.is_direct_mention(event):
            await self.release_flow(event.destination.id, "direct_mention")
            return False
        if self.is_priority_action(event):
            return False
        flow = await self._flow(event.destination.id, event.occurred_at or time.time())
        if not flow:
            return False
        return event.actor_id != self.bot_id

    def is_direct_mention(self, event: Event) -> bool:
        return (event.event_type == "message.created" and event.destination.kind == "group"
                and event.actor_id != self.bot_id and self.bot_id in self._mentions(event))

    @classmethod
    def is_priority_action(cls, event: Event) -> bool:
        return event.actor_role in {"owner", "admin"} and any(
            word in (event.text or "") for word in cls.PRIORITY_ACTION_WORDS
        )

    async def release_flow(self, group_id: str, reason: str = "priority_action") -> None:
        row = await self.repo.get(self._flow_id(group_id))
        if row and row.status == "active":
            await self.repo.patch(row.id, status="cancelled",
                                  payload={**row.payload, "state": "cancelled", "release_reason": reason,
                                           "expires_at": time.time()})
            self._pending.pop("group:" + str(group_id), None)
            self._last_bot_reply.pop("group:" + str(group_id), None)
            for probe in await self.repo.find("bot_skill_probe", group_id=group_id, status="sent", limit=32):
                if probe.payload.get("bot_id") == row.payload.get("bot_id"):
                    await self.repo.patch(probe.id, status="cancelled")

    @staticmethod
    def _flow_finished(text: str) -> bool:
        value = str(text or "")
        return bool(re.search(r"正在播放|已播放|播放成功|点歌成功|已添加|已加入|已取消|取消成功|失败|错误|不存在|超时", value))

    @staticmethod
    def _flow_has_choices(text: str) -> bool:
        value = str(text or "")
        return bool(re.search(r"(?m)^\s*\d+[.)、]|请选择|输入\s*(?:数字|序号|\d)", value))

    async def _activate_flow(self, event: Event, item: dict[str, Any], observation_id: str) -> None:
        now = event.occurred_at or time.time()
        profile = await self.repo.get(self._profile_id(event.destination.id, event.actor_id))
        if not profile or profile.status != "active":
            return
        current = await self._flow(event.destination.id, now)
        if current and current.payload.get("bot_id") != event.actor_id:
            return
        if not current and not self._flow_has_choices(event.text):
            return
        previous = current.payload if current else {}
        awaiting_response = bool(previous.get("awaiting_response"))
        finished = self._flow_finished(event.text) or (awaiting_response and not self._flow_has_choices(event.text))
        state = "cooldown" if finished else "active"
        expires = now + (10.0 if state == "cooldown" else self.flow_ttl)
        payload = {**previous, "group_id": event.destination.id, "bot_id": event.actor_id,
                   "actor_id": item.get("actor_id", ""), "last_bot_message_id": event.message_id,
                   "last_bot_event_id": event.id, "last_observation_id": observation_id,
                   "state": state, "awaiting_response": False, "updated_at": now, "expires_at": expires,
                   "nova_managed": bool(previous.get("nova_managed") or item.get("probe") or item.get("actor_id") == self.bot_id),
                   "guard_version": 2}
        saved = await self.repo.get(self._flow_id(event.destination.id))
        if saved:
            await self.repo.patch(saved.id, status="active", payload=payload)
        else:
            await self.repo.put(Record(self._flow_id(event.destination.id), "bot_skill_flow", None,
                                       event.destination.id, "active", payload,
                                       "bot-flow:" + event.destination.id))

    async def _forward_flow_input(self, event: Event, flow: Record) -> bool:
        if not flow.payload.get("nova_managed") or self._mentions(event):
            return False
        if event.actor_id != flow.payload.get("actor_id") or not re.fullmatch(r"\s*\d{1,3}\s*", event.text or ""):
            return False
        if self.platform is None or not getattr(self.platform, "ready", False):
            return False
        bot_id = str(flow.payload.get("bot_id") or "")
        if not bot_id.isdecimal():
            return False
        forward_id = hashlib.sha256(("bot-forward:" + event.id).encode()).hexdigest()
        if await self.repo.get(forward_id):
            return False
        sent = await self.platform.call("send_group_msg", {"group_id": int(event.destination.id),
            "message": [{"type": "at", "data": {"qq": bot_id}},
                        {"type": "text", "data": {"text": " " + event.text.strip()}}]})
        now = event.occurred_at or time.time()
        message_id = str(sent.get("message_id", "")) if isinstance(sent, dict) else ""
        payload = {**flow.payload, "last_input_event_id": event.id, "last_input_message_id": message_id,
                   "state": "active", "awaiting_response": True, "updated_at": now,
                   "expires_at": now + self.flow_ttl}
        await self.repo.patch(flow.id, status="active", payload=payload)
        if not await self.repo.get(forward_id):
            await self.repo.put(Record(forward_id, "bot_skill_flow_forward", event.actor_id,
                                       event.destination.id, "sent", {"bot_id": bot_id,
                                       "source_event_id": event.id, "message_id": message_id,
                                       "text": event.text.strip(), "sent_at": now},
                                       "bot-forward:" + event.id))
        return True

    async def _is_admin(self, event: Event) -> bool:
        configured = set(self.admin_ids)
        row = await self.repo.get("bot-skill-admins")
        if row:
            configured.update(str(value) for value in row.payload.get("qq_ids", []))
        return event.actor_id in configured or event.actor_role in {"owner", "admin"}

    async def admin_ids_configured(self) -> list[str]:
        values = set(self.admin_ids)
        row = await self.repo.get("bot-skill-admins")
        if row:
            values.update(str(value) for value in row.payload.get("qq_ids", []))
        return sorted(values)

    async def set_admin_ids(self, values: list[str]) -> list[str]:
        values = sorted({str(value).strip() for value in values if str(value).strip()})
        if any(not value.isdecimal() for value in values) or len(values) > 100:
            raise ValueError("管理员 QQ 号必须是数字且不超过 100 个")
        current = await self.repo.get("bot-skill-admins")
        if current:
            await self.repo.patch(current.id, status="active", payload={"qq_ids": values})
        else:
            await self.repo.put(Record("bot-skill-admins", "bot_skill_admins", None, None, "active",
                                       {"qq_ids": values}, "bot-skill-admins"))
        return values

    @classmethod
    def _target_bots(cls, event: Event, own_id: str) -> list[str]:
        return [value for value in cls._mentions(event) if value != own_id]

    @staticmethod
    def _registration_requested(text: str) -> bool:
        value = text.casefold().replace(" ", "")
        return any(phrase in value for phrase in (
            "是个机器人", "是机器人", "这是机器人", "这个账号是机器人", "这个号是机器人",
            "当作机器人", "记住这是机器人", "标记为机器人", "登记机器人",
        ))

    @staticmethod
    def _profile_id(group_id: str, bot_id: str) -> str:
        return hashlib.sha256(("bot-profile:" + group_id + ":" + bot_id).encode()).hexdigest()

    async def register(self, event: Event, bot_id: str, name: str = "") -> Record:
        if event.destination.kind != "group" or not await self._is_admin(event):
            raise PermissionError("只有群主、群管理员或配置的 QQ 管理员可以登记外部机器人")
        if not bot_id.isdecimal() or bot_id == self.bot_id:
            raise ValueError("无效的外部机器人 QQ 号")
        row_id = self._profile_id(event.destination.id, bot_id)
        current = await self.repo.get(row_id)
        observed_commands = []
        observed_reply = ""
        if current is None:
            observations = await self.repo.find("bot_skill_observation", group_id=event.destination.id,
                                                limit=500)
            for observation in observations:
                if str(observation.payload.get("bot_id")) != bot_id:
                    continue
                command = str(observation.payload.get("trigger_command") or "").strip()
                if not command.startswith("/") or command in observed_commands:
                    continue
                observed_commands.append(command)
                if not observed_reply:
                    observed_reply = re.sub(r"\s+", " ",
                                            str(observation.payload.get("response_text") or ""))[:180]
                if len(observed_commands) >= 12:
                    break
        payload = {
            **(current.payload if current else {}),
            "bot_id": bot_id, "bot_name": name or (current.payload.get("bot_name", "") if current else ""),
            "group_id": event.destination.id, "registered_by": event.actor_id,
            "registered_at": current.payload.get("registered_at", time.time()) if current else time.time(),
            "last_seen_at": time.time(), "summary": current.payload.get("summary", "") if current else (
                "外部 QQ 机器人，已观察到指令：" + "、".join(observed_commands) +
                "。最近回复摘要：" + observed_reply if observed_commands else ""),
            "capabilities": current.payload.get("capabilities", []) if current else observed_commands,
            "workflow_count": current.payload.get("workflow_count", 0) if current else 0,
        }
        if current:
            return await self.repo.patch(row_id, status="active", payload=payload)
        return await self.repo.put(Record(row_id, "bot_profile", None, event.destination.id, "active", payload,
                                          "bot-profile:" + event.destination.id + ":" + bot_id))

    async def profiles(self, destination: Destination | None = None, *, include_rejected: bool = False) -> list[Record]:
        statuses = None if include_rejected else "active"
        return await self.repo.find("bot_profile", group_id=destination.id if destination and destination.kind == "group" else None,
                                    status=statuses, limit=200)

    async def prompt_context(self, destination: Destination) -> dict[str, Any]:
        profiles = await self.profiles(destination)
        registered_ids = {str(row.payload.get("bot_id")) for row in profiles}
        workflows = await self.repo.find("bot_skill_workflow", group_id=destination.id if destination.kind == "group" else None,
                                         status="active", limit=200)
        return {
            "registered_bots": [
                {"bot_id": row.payload.get("bot_id"), "name": row.payload.get("bot_name", ""),
                 "summary": row.payload.get("summary", ""), "capabilities": row.payload.get("capabilities", []),
                 "workflow_count": row.payload.get("workflow_count", 0)} for row in profiles
            ],
            "learned_workflows": [
                {"bot_id": row.payload.get("bot_id"), "summary": row.payload.get("summary", "")[:300],
                 "steps": [{"input": str(step.get("input", ""))[:200],
                            "output": str(step.get("output", ""))[:240]}
                           for step in row.payload.get("steps", [])[-2:]]}
                for row in workflows if str(row.payload.get("bot_id")) in registered_ids
            ][:8],
        }

    async def probe(self, event: Event, bot_id: str, command: str, *, requested: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return {"error": "BOT_SKILLS_DISABLED"}
        if event.destination.kind != "group":
            return {"error": "GROUP_REQUIRED"}
        profile = await self.repo.get(self._profile_id(event.destination.id, bot_id))
        if not profile or profile.status != "active":
            return {"error": "BOT_NOT_REGISTERED", "detail": "先由管理员登记这个 QQ 账号为外部机器人"}
        source_bot = await self.repo.get(self._profile_id(event.destination.id, event.actor_id))
        if event.actor_id == self.bot_id or source_bot and source_bot.status == "active":
            return {"error": "BOT_ACTION_NOT_REQUESTED", "detail": "机器人回执不是用户委托，不能由此再次调用机器人。"}
        command = str(command or "").strip()[:300]
        if not command:
            raise ValueError("试探指令不能为空")
        discovery = command.casefold() in {"help", "/help", "帮助", "/帮助", "菜单", "/菜单"}
        if not requested and (not discovery or event.text.strip().casefold() == command.casefold()):
            return {"error": "BOT_ACTION_NOT_REQUESTED", "detail": "群友正在自行使用机器人时只能观察学习，不得复制或重发指令；业务调用须由用户明确委托 Nova。"}
        if not requested and (await self._flow(event.destination.id) or any(
                bot_id in item.get("targets", []) and time.time() - item["time"] <= self.ttl
                for item in self._pending.get(event.destination.key, []))):
            return {"error": "BOT_INTERACTION_BUSY", "detail": "正在观察已有机器人交互，暂不发送新的帮助试探。"}
        async with self._probe_lock:
            return await self._send_probe(event, bot_id, command)

    async def _send_probe(self, event: Event, bot_id: str, command: str) -> dict[str, Any]:
        probe_id = hashlib.sha256(("bot-probe:" + event.id + ":" + bot_id + ":" + command).encode()).hexdigest()
        previous = await self.repo.get(probe_id)
        if previous:
            return {"status": previous.status, "bot_id": bot_id, "command": command,
                    "probe_id": probe_id, "deduplicated": True}
        if self.platform is None or not getattr(self.platform, "ready", False):
            return {"error": "QQ_UNAVAILABLE"}
        message = [{"type": "at", "data": {"qq": bot_id}}, {"type": "text", "data": {"text": " " + command}}]
        sent = await self.platform.call("send_group_msg", {"group_id": int(event.destination.id), "message": message})
        now = time.time()
        item = {"event_id": "probe:" + uuid.uuid4().hex, "message_id": str(sent.get("message_id", "")) if isinstance(sent, dict) else "",
                "actor_id": event.actor_id, "text": command, "targets": [bot_id], "command": command,
                "time": now, "bot_id": bot_id, "probe": True, "probe_id": probe_id}
        async with self._lock:
            self._pending.setdefault(event.destination.key, []).append(item)
        await self.repo.put(Record(probe_id, "bot_skill_probe", event.actor_id, event.destination.id, "sent", {
            "bot_id": bot_id, "command": command, "source_event_id": event.id, "sent_at": now,
            "platform_message_id": item["message_id"]}, "bot-probe:" + probe_id))
        return {"status": "sent", "bot_id": bot_id, "command": command, "probe_id": probe_id}

    @staticmethod
    def _mentions(event: Event) -> list[str]:
        return [str(s.get("data", {}).get("qq")) for s in event.segments
                if isinstance(s, dict) and s.get("type") == "at" and s.get("data", {}).get("qq")]

    def _looks_like_command(self, event: Event) -> bool:
        text = event.text.strip()
        mentions = [value for value in self._mentions(event) if value != self.bot_id]
        if text.startswith("/") or any(word in text.lower() for word in self.COMMAND_WORDS):
            return bool(mentions) or text.startswith("/")
        return bool(mentions)

    @staticmethod
    def _looks_like_bot_reply(event: Event) -> bool:
        text = event.text.strip()
        if not text:
            return any(isinstance(s, dict) and s.get("type") in {"image", "record", "file"} for s in event.segments)
        if re.search(r"(?m)^\s*(?:\d+[.)、]|[一二三四五六七八九十]+[、.])", text):
            return True
        return bool(re.search(r"帮助|用法|指令|命令|菜单|请选择|输入\s*\d|成功|失败|点歌|搜索结果", text, re.I))

    async def observe(self, event: Event, policy) -> None:
        if not self.enabled:
            return
        if event.event_type != "message.created" or event.destination.kind != "group":
            return
        if not policy.enabled or not policy.collect_messages:
            return
        now = event.occurred_at or time.time()
        async with self._lock:
            if self.is_direct_mention(event):
                await self.release_flow(event.destination.id, "direct_mention")
            flow = await self._flow(event.destination.id, now)
            forwarded = False
            observed_input = False
            if flow and event.actor_id != self.bot_id:
                forwarded = await self._forward_flow_input(event, flow)
                observed_input = (not flow.payload.get("nova_managed")
                    and event.actor_id == flow.payload.get("actor_id") and not self._mentions(event)
                    and bool(re.fullmatch(r"\s*\d{1,3}\s*", event.text or "")))
                if observed_input:
                    await self.repo.patch(flow.id, payload={**flow.payload, "last_input_event_id": event.id,
                        "last_input_message_id": event.message_id, "awaiting_response": True,
                        "updated_at": now, "expires_at": now + self.flow_ttl})
            targets_for_registration = self._target_bots(event, self.bot_id)
            if await self._is_admin(event) and targets_for_registration and self._registration_requested(event.text):
                for bot_id in targets_for_registration[:4]:
                    await self.register(event, bot_id, "")
            pending = [item for item in self._pending.get(event.destination.key, [])
                       if now - item["time"] <= self.ttl]
            self._pending[event.destination.key] = pending[-32:]

            # Recover probes after a process restart from the durable feature store.
            for row in await self.repo.find("bot_skill_probe", group_id=event.destination.id, status="sent", limit=32):
                if now - row.payload.get("sent_at", now) <= self.ttl and not any(item.get("probe_id") == row.id for item in pending):
                    pending.append({"event_id": "probe:" + row.id, "message_id": row.payload.get("platform_message_id", ""),
                                    "actor_id": row.owner_id or "", "text": row.payload.get("command", ""),
                                    "targets": [row.payload.get("bot_id", "")], "command": row.payload.get("command", ""),
                                    "time": row.payload.get("sent_at", now), "bot_id": row.payload.get("bot_id", ""),
                                    "probe": True, "probe_id": row.id})

            if (flow and event.actor_id == str(flow.payload.get("bot_id", ""))
                    and flow.payload.get("awaiting_response")
                    and event.id != flow.payload.get("last_bot_event_id")
                    and not any(item.get("event_id") == flow.payload.get("last_input_event_id") for item in pending)):
                pending.append({"event_id": flow.payload.get("last_input_event_id", "flow-input"),
                                "message_id": flow.payload.get("last_input_message_id", ""),
                                "actor_id": flow.payload.get("actor_id", ""), "text": "",
                                "targets": [event.actor_id], "command": "flow-input", "time": now,
                                "workflow_parent": flow.payload.get("last_observation_id"), "flow": True})

            if (forwarded or observed_input) and flow:
                pending.append({"event_id": event.id, "message_id": event.message_id,
                                "actor_id": event.actor_id, "text": event.text,
                                "targets": [str(flow.payload.get("bot_id", ""))],
                                "command": event.text.strip(), "time": now,
                                "workflow_parent": flow.payload.get("last_observation_id"),
                                "flow": True})

            # A response from the mentioned account (or a direct reply) completes
            # an observation. The original messages remain the evidence source.
            for item in list(pending):
                targets = set(item["targets"])
                is_target = event.actor_id in targets
                is_reply = event.reply_to_id in {item["message_id"], item["event_id"]}
                if not (is_target and (is_reply or now >= item["time"])):
                    continue
                profile_id = self._profile_id(event.destination.id, event.actor_id)
                profile = await self.repo.get(profile_id)
                if not profile or profile.status != "active":
                    pending.remove(item)
                    continue
                if not event.text.strip() and not any(
                        isinstance(segment, dict) and segment.get("type") in {"image", "record", "file"}
                        for segment in event.segments):
                    continue
                digest = hashlib.sha256(("bot-skill:" + event.destination.key + ":" + item["event_id"] + ":" + event.id).encode()).hexdigest()
                payload = {
                    "bot_id": event.actor_id,
                    "bot_name": event.actor_name,
                    "trigger_event_id": item["event_id"],
                    "trigger_message_id": item["message_id"],
                    "trigger_actor_id": item["actor_id"],
                    "trigger_text": item["text"][:2000],
                    "trigger_command": item["command"],
                    "response_event_id": event.id,
                    "response_message_id": event.message_id,
                    "response_text": event.text[:4000],
                    "response_segments": event.segments[:12],
                    "observed_at": now,
                    "confidence": 0.72 if is_reply else 0.58,
                    "source_ids": [item["event_id"], event.id],
                    "learning_state": "待审核",
                    "probe": bool(item.get("probe")),
                    "workflow_parent": self._last_bot_reply.get(event.destination.key, {}).get("observation_id"),
                }
                if not await self.repo.get(digest):
                    await self.repo.put(Record(digest, "bot_skill_observation", None,
                                               event.destination.id, "candidate", payload,
                                               "bot-skill:" + event.destination.key + ":" + item["event_id"] + ":" + event.id))
                capabilities = list(profile.payload.get("capabilities", []))
                command = str(item.get("command") or "@触发")
                if command not in capabilities:
                    capabilities.append(command)
                summary = ("外部 QQ 机器人，已观察到指令：" + "、".join(capabilities[:12]) +
                           "。最近回复摘要：" + re.sub(r"\s+", " ", event.text)[:180])
                await self.repo.patch(profile_id, payload={**profile.payload, "last_seen_at": now,
                    "capabilities": capabilities[:40], "summary": summary})
                observation_id = digest
                parent = payload.get("workflow_parent")
                if parent:
                    parent_row = await self.repo.get(parent)
                    if parent_row:
                        steps = list(parent_row.payload.get("steps", []))
                        if not steps:
                            steps.append({"input": parent_row.payload.get("trigger_text", "")[:300],
                                          "output": parent_row.payload.get("response_text", "")[:1000],
                                          "bot_id": parent_row.payload.get("bot_id"), "at": parent_row.payload.get("observed_at", now)})
                        steps.append({"input": item.get("text", "")[:300], "output": event.text[:1000],
                                      "bot_id": event.actor_id, "at": now})
                        workflow_id = hashlib.sha256(("workflow:" + parent + ":" + digest).encode()).hexdigest()
                        await self.repo.put(Record(workflow_id,
                            "bot_skill_workflow", None, event.destination.id, "active", {
                                "bot_id": event.actor_id, "steps": steps[-12:],
                                "summary": "；".join(str(step.get("input", "")) for step in steps[-4:]),
                                "source_ids": list(dict.fromkeys(parent_row.payload.get("source_ids", []) + [item["event_id"], event.id]))
                            }, "bot-workflow:" + parent + ":" + digest))
                        profile = await self.repo.get(profile_id)
                        if profile:
                            await self.repo.patch(profile_id, payload={**profile.payload,
                                "workflow_count": profile.payload.get("workflow_count", 0) + 1})
                self._last_bot_reply[event.destination.key] = {"bot_id": event.actor_id,
                    "observation_id": observation_id, "response_text": event.text, "time": now}
                await self._activate_flow(event, item, observation_id)
                pending.remove(item)

            last = self._last_bot_reply.get(event.destination.key, {})
            is_numeric_followup = False
            registration_only = bool(targets_for_registration and self._registration_requested(event.text))
            if (self._looks_like_command(event) and not registration_only) or is_numeric_followup:
                targets = [value for value in self._mentions(event) if value != self.bot_id]
                command = event.text.strip().split(None, 1)[0][:120] if event.text.strip().startswith("/") else "@触发"
                if is_numeric_followup:
                    targets = [last["bot_id"]]
                    command = event.text.strip()
                pending.append({"event_id": event.id, "message_id": event.message_id,
                                "actor_id": event.actor_id, "text": event.text,
                                "targets": targets, "command": command, "time": now,
                                "workflow_parent": last.get("observation_id")})
            self._pending[event.destination.key] = pending[-32:]


class SilentLearningService:
    """Extract candidate facts from inbound group windows without replying."""

    def __init__(self, repo: FeatureRepository, store: Store, model: ModelProvider,
                 *, window_size: int = 16) -> None:
        self.repo, self.store, self.model = repo, store, model
        self.window_size = window_size
        self._pending: dict[str, list[Event]] = {}
        self._running: set[str] = set()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        from osa.application.people import PeopleService
        self.people = PeopleService(repo, store)

    async def observe(self, event: Event, policy) -> None:
        if event.event_type != "message.created" or event.destination.kind != "group":
            return
        if not policy.collect_messages or not policy.memory_extract:
            return
        async with self._lock:
            if event.destination.key not in self._pending:
                buffer_id = hashlib.sha256(('learning-buffer:' + event.destination.key).encode()).hexdigest()
                saved = await self.repo.get(buffer_id)
                if saved and saved.status == 'pending':
                    self._pending[event.destination.key] = [Event.from_dict(item) for item in saved.payload.get('events', [])]
            bucket = self._pending.setdefault(event.destination.key, [])
            if any(item.id == event.id for item in bucket):
                return
            bucket.append(event)
            await self._persist_buffer(event.destination, bucket)
            if len(bucket) < self.window_size or event.destination.key in self._running:
                return
            batch = bucket[:self.window_size]
            del bucket[:self.window_size]
            self._running.add(event.destination.key)
            await self._persist_batch(event.destination, batch)
            await self._persist_buffer(event.destination, bucket)
        task = asyncio.create_task(self._extract(event.destination, batch), name=f"silent-learning:{event.destination.key}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def flush_idle(self, seconds: float = 90) -> None:
        """Quiet groups also learn; they need not reach the full window size."""
        for row in await self.repo.find('learning_buffer', status='pending', limit=1000):
            key = row.payload.get('conversation', '')
            if key and key not in self._pending and key not in self._running:
                self._pending[key] = [Event.from_dict(item) for item in row.payload.get('events', [])]
        # A process may have stopped after dequeuing a batch. Replaying uses
        # deterministic memory ids and knowledge dedupe keys.
        for row in await self.repo.find('learning_batch', status='pending', limit=100):
            key = row.payload['conversation']
            if key not in self._running:
                kind, identity = key.split(':', 1)
                self._running.add(key)
                await self._extract(Destination(kind, identity), [Event.from_dict(item) for item in row.payload['events']])
        for key, batch in list(self._pending.items()):
            if batch and time.time() - batch[-1].occurred_at >= seconds:
                kind, identity = key.split(':', 1)
                await self.flush(Destination(kind, identity))

    async def shutdown(self):
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def flush(self, destination: Destination) -> None:
        async with self._lock:
            if destination.key in self._running:
                return
            batch = self._pending.pop(destination.key, [])
            if not batch:
                return
            self._running.add(destination.key)
            await self._persist_batch(destination, batch)
            await self._persist_buffer(destination, [])
        await self._extract(destination, batch)

    async def _persist_buffer(self, destination, events):
        key = hashlib.sha256(('learning-buffer:' + destination.key).encode()).hexdigest()
        payload = {'conversation': destination.key, 'events': [event.to_dict() for event in events]}
        row = await self.repo.get(key)
        if row:
            await self.repo.patch(key, status='pending' if events else 'idle', payload=payload)
        else:
            await self.repo.put(Record(key, 'learning_buffer', None, destination.id,
                'pending' if events else 'idle', payload))

    async def _persist_batch(self, destination, batch):
        key = self._batch_id(destination, batch)
        await self.repo.put(Record(key, 'learning_batch', None, destination.id, 'pending', {
            'conversation': destination.key, 'events': [event.to_dict() for event in batch]}))

    @staticmethod
    def _batch_id(destination, batch):
        return hashlib.sha256(('learning-batch:' + destination.key + ':' + ':'.join(event.id for event in batch)).encode()).hexdigest()

    async def _extract(self, destination: Destination, batch: list[Event]) -> None:
        cancelled = False
        try:
            policy = await self.store.policy(destination)
            if not policy.collect_messages or not policy.memory_extract or not policy.enabled:
                return
            # Recalled messages and revoked collection permission never reach
            # the memory model, including batches recovered after restart.
            valid_batch = [event for event in batch if await self.store.event_active(event.id)]
            payload = [{"id": e.id, "actor_id": e.actor_id, "actor_name":e.actor_name, "text": e.text[:3000],
                        "reply_to_id": e.reply_to_id, "message_id":e.message_id,
                        "mentioned_ids":[s['data'].get('qq') for s in e.segments if s['type']=='at'], "time": e.occurred_at}
                       for e in valid_batch if e.text.strip()]
            if not payload:
                return
            result = await self.model.generate([{"role": "system", "content": CATALOG['passive_memory']['default']},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], "memory")
            raw = json.loads(result.text.removeprefix("```json").removesuffix("```").strip())
            source_ids = {event.id for event in valid_batch if await self.store.event_active(event.id)}
            current = await self.store.policy(destination)
            if not current.collect_messages or not current.memory_extract:
                return
            for item in raw.get("knowledge", [])[:20]:
                await self._save_knowledge(destination, item, source_ids)
            for item in raw.get('aliases', [])[:20]:
                await self.people.learn_alias(destination, item, valid_batch)
            for item in raw.get("facts", [])[:20]:
                # A self-statement must come from the claimed speaker, not
                # from another member describing them.
                if isinstance(item, dict) and any(e.actor_id == str(item.get('subject_id')) and e.id in item.get('source_ids', []) for e in batch):
                    own_sources = {e.id for e in valid_batch if e.actor_id == str(item.get('subject_id')) and e.id in source_ids}
                    await self._save_fact(destination, item, own_sources)
        except (asyncio.CancelledError,):
            cancelled = True
            raise
        except Exception as exc:
            await self.repo.put(Record(uuid.uuid4().hex, "learning_attempt", None,
                destination.id, "failed", {"error_type": type(exc).__name__,
                "source_ids": [e.id for e in batch]},
                "learning:" + destination.key + ":" + str(batch[-1].id)))
        finally:
            key = self._batch_id(destination, batch)
            if not cancelled and await self.repo.get(key):
                await self.repo.patch(key, status='completed', payload={'conversation': destination.key})
            async with self._lock:
                self._running.discard(destination.key)

    async def _save_knowledge(self, destination: Destination, item: Any, source_ids: set[str]) -> None:
        if not isinstance(item, dict):
            return
        content = str(item.get("content") or "").strip()
        sources = [str(x) for x in item.get("source_ids", []) if str(x) in source_ids]
        confidence = float(item.get("confidence", 0) or 0)
        if not content or not sources or len(content) > 2000 or confidence < .75:
            return
        shareable = bool(item.get("shareable")) and confidence >= .9
        scope = "shared_unverified" if shareable else "group_private"
        status = "shared" if shareable else "candidate"
        previous = [row for row in await self.repo.find('knowledge_candidate', limit=1000)
                    if row.status == status and row.payload.get('origin_group_id') == destination.id
                    and duplicate_memory(content, str(row.payload.get('content') or ''))]
        if previous:
            for duplicate in sorted(previous, key=lambda row: (row.created_at, row.id))[1:]:
                await self.repo.patch(duplicate.id, status='superseded')
            return
        key = "knowledge:" + destination.id + ":" + content
        await self.repo.put(Record(uuid.uuid4().hex, "knowledge_candidate", None,
            None if shareable else destination.id, status,
            {"content": content, "source_ids": sources, "origin_group_id": destination.id,
             "scope": scope, "trust_status": "unverified", "confidence": confidence,
             "shareable": shareable, "learned_at": time.time()}, key))

    async def _save_fact(self, destination: Destination, item: Any, source_ids: set[str]) -> None:
        if not isinstance(item, dict):
            return
        subject = str(item.get("subject_id") or "").strip()
        content = str(item.get("content") or "").strip()
        sources = [str(x) for x in item.get("source_ids", []) if str(x) in source_ids]
        confidence = float(item.get("confidence", 0) or 0)
        if not subject or not content or len(content)>2000 or not sources or confidence < .9:
            return
        # A fact stays scoped to its source group until the subject explicitly
        # grants cross-group consent; it is never made a global personality rule.
        memory_id = "learned:" + uuid.uuid5(uuid.NAMESPACE_URL, destination.id + subject + content).hex
        saved = await self.store.add_memory(Memory(memory_id, content, Scope.LONG_GROUP, subject,
            destination.id, sources[0]))
        if saved.id == memory_id:
            await self.repo.put(Record('evidence:'+memory_id, 'memory_evidence', subject, destination.id, 'active',
                {'memory_id':memory_id, 'source_ids':sources, 'confidence':confidence, 'origin':'explicit_self_statement'}))

    async def recall(self, destination: Destination, query: str) -> list[dict]:
        query = query.lower().strip()
        terms = set(re.findall(r'[a-z0-9_]{2,}', query))
        for phrase in re.findall(r'[\u4e00-\u9fff]+', query):
            terms.update(phrase[i:i+2] for i in range(len(phrase)-1))
        candidates = await self.repo.find('knowledge_candidate', limit=1000)
        ranked = []
        for row in candidates:
            if row.status not in {'candidate', 'shared'}:
                continue
            globally_visible = self.store.permissions.shares_group_memory(row.group_id, destination)
            if (row.group_id != (destination.id if destination.kind == 'group' else None)
                    and row.status != 'shared' and not globally_visible):
                continue
            sources = row.payload.get('source_ids', [])
            if not sources or any([not await self.store.event_active(sid) for sid in sources]):
                continue
            if not row.payload.get('origin_group_id') or not (await self.store.policy(Destination('group', row.payload['origin_group_id']))).collect_messages:
                continue
            content = str(row.payload.get('content', ''))
            score = sum(term in content.lower() for term in terms)
            if query and not score:
                continue
            ranked.append((score, row.created_at, {'id': row.id, 'content': content,
                'trust_status': 'unverified', 'scope': '共享知识' if row.status == 'shared' or globally_visible else '当前群知识',
                'source_type': 'community_claim', 'learned_at': row.payload.get('learned_at')}))
        return [item for _, _, item in sorted(ranked, key=lambda x: (x[0], x[1]), reverse=True)[:8]]
