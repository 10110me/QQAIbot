"""Volatile demo adapter. Never use for live QQ or persistent personal data."""
import asyncio
from osa.application.reply_delivery import delivery_parts
import hashlib
import json
import logging
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from osa.domain.entities import (
    Consent, Destination, Event, GroupPolicy, Memory, ModelResult, Scope, utcnow,
)
from osa.domain.policy import PermissionPolicy


class InMemoryStore:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.group_policies: dict[str, GroupPolicy] = {}
        self.memories: dict[str, Memory] = {}
        self.consents: dict[str, Consent] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.outboxes: dict[str, dict[str, Any]] = {}
        self.resource_rows: list[dict[str, Any]] = []
        self.daily_rows: dict[str, dict[str, Any]] = {}
        self._paused = False
        self._lock = asyncio.Lock()
        self.permissions = PermissionPolicy()

    async def initialize(self, groups: list[str], create_schema: bool = False) -> None:
        for group in groups:
            self.group_policies.setdefault(group, GroupPolicy())
        self.permissions.group_memory_sources.clear()
        for group_id, policy in self.group_policies.items():
            self.permissions.update_group_memory_source(group_id, policy)

    async def close(self) -> None:
        pass

    async def health(self) -> bool:
        return True

    async def policy(self, destination: Destination) -> GroupPolicy:
        return GroupPolicy() if destination.kind == "private" else self.group_policies.get(destination.id, GroupPolicy(enabled=False))

    async def groups(self) -> list[dict[str, Any]]:
        return [{"id": group, "settings": asdict(policy)} for group, policy in self.group_policies.items()]

    async def ensure_group(self, group_id: str) -> None:
        self.group_policies.setdefault(group_id, GroupPolicy())
        self.permissions.update_group_memory_source(group_id, self.group_policies[group_id])

    async def update_policy(self, group_id: str, policy: GroupPolicy) -> None:
        if group_id not in self.group_policies:
            raise KeyError(group_id)
        self.group_policies[group_id] = policy
        self.permissions.update_group_memory_source(group_id, policy)

    async def paused(self) -> bool:
        return self._paused

    async def set_paused(self, paused: bool) -> None:
        self._paused = paused

    async def publish(self, event: Event, collect: bool) -> bool:
        async with self._lock:
            if event.id in self.events:
                return False
            recalled = any(row["event"].event_type == "message.recalled" and row["event"].destination == event.destination and row["event"].message_id == event.message_id for row in self.events.values())
            self.events[event.id] = {"event": event, "status": "pending", "collect": collect, "recalled": recalled, "error": None}
            return True

    async def claim_events(self, limit: int, exclude_conversations: set[str] | None = None) -> list[Event]:
        async with self._lock:
            rows = [row for row in self.events.values() if row["status"] == "pending" and row["event"].destination.key not in (exclude_conversations or set())][:limit]
            for row in rows:
                row["status"] = "processing"
            return [row["event"] for row in rows]

    async def finish_event(self, event_id: str, error: str | None = None) -> None:
        row = self.events[event_id]
        row["status"], row["error"] = ("failed" if error else "done"), error
        if not row["collect"]:
            row["event"] = replace(row["event"], text="", segments=[])

    async def recall(self, event: Event) -> None:
        for row in self.events.values():
            original = row["event"]
            if original.destination == event.destination and original.message_id == event.message_id and original.event_type in {"message.created","group.file_uploaded"}:
                row["recalled"], row["event"] = True, replace(original, text="", segments=[])
                for memory in list(self.memories.values()):
                    if memory.source_event_id == original.id:
                        await self.delete_memory(memory.id)
                for decision in self.decisions.values():
                    if original.id in decision["snapshot"].get("source_ids", []):
                        decision["snapshot"] = {}
                for item in self.outboxes.values():
                    if item["event_id"] == original.id and item["status"] == "pending":
                        item["status"], item["text"] = "cancelled", ""
                for daily in self.daily_rows.values():
                    if original.id in daily["source_ids"]:
                        daily["status"], daily["summary"] = "invalidated", ""

    async def event_active(self, event_id: str) -> bool:
        return event_id in self.events and not self.events[event_id]["recalled"]

    async def event_by_message(self, destination: Destination, message_id: str) -> Event | None:
        for row in self.events.values():
            event=row['event']
            if event.destination==destination and event.message_id==message_id and not row['recalled']:return event
        return None

    async def reply_is_bot(self, event: Event) -> bool:
        return bool(event.reply_to_id) and any(item["conversation"] == event.destination.key and item["status"] == "sent" and item["platform_message_id"] == event.reply_to_id for item in self.outboxes.values())

    async def context(self, event: Event, limit: int) -> list[dict[str, str]]:
        from osa.application.message_context import render_event
        from osa.application.conversation_prompt import message_metadata
        values = [(row["event"].occurred_at, {"role": "user", "content": render_event(row["event"], include_id=True), "source_id": row["event"].id,
                                            **message_metadata(row["event"])}) for row in self.events.values()
                  if row["collect"] and not row["recalled"] and row["event"].destination == event.destination and row["event"].event_type == "message.created" and row["event"].occurred_at <= event.occurred_at]
        values += [(item["sent_at"], {"role": "assistant", "content": item["text"], "source_id": item["event_id"],
                    "actor_id": item["actor_id"], "occurred_at": item["sent_at"], "reply_to_id": item.get("reply_to"),
                    "message_id": item.get("platform_message_id")}) for item in self.outboxes.values()
                   if item["status"] == "sent" and item["conversation"] == event.destination.key
                   and item["sent_at"] <= event.occurred_at
                   and self.permissions.can_reuse_group_reply(item["memory_ids"], item.get("resource_kinds", []),
                                                              self.memories, event.destination, utcnow().timestamp())]
        return [value for _, value in sorted(values, key=lambda x: x[0])[-limit:]]

    async def quoted_context(self, event: Event) -> dict[str, str] | None:
        if not event.reply_to_id:
            return None
        from osa.application.message_context import render_event
        source = next((row["event"] for row in self.events.values()
                       if row["collect"] and not row["recalled"]
                       and row["event"].destination == event.destination
                       and row["event"].message_id == event.reply_to_id
                       and row["event"].event_type == "message.created"), None)
        if source:
            return {"content": render_event(source, include_id=True), "source_id": source.id}
        reply = next((item for item in self.outboxes.values()
                      if item["conversation"] == event.destination.key
                      and item["platform_message_id"] == event.reply_to_id
                      and item["status"] == "sent"
                      and self.permissions.can_reuse_group_reply(item["memory_ids"], item.get("resource_kinds", []),
                                                                 self.memories, event.destination, utcnow().timestamp())), None)
        if reply:
            parts = sorted((item for item in self.outboxes.values()
                            if item["decision_id"] == reply["decision_id"] and item["status"] == "sent"),
                           key=lambda item: item["created_at"])
            if any(not self.permissions.can_reuse_group_reply(item["memory_ids"], item.get("resource_kinds", []),
                                                              self.memories, event.destination, utcnow().timestamp())
                   for item in parts):
                return None
            return {"content": "[引用的 Nova 回复] " + "".join(item["text"] for item in parts),
                    "source_id": reply["event_id"]}
        return None

    async def search_events(self, destination: Destination, after: float, before: float,
                            query: str, cursor: tuple[float, str] | None, limit: int) -> tuple[list[Event], str | None]:
        rows = [row["event"] for row in self.events.values()
                if row["collect"] and not row["recalled"] and row["event"].destination == destination
                and row["event"].event_type == "message.created" and after <= row["event"].occurred_at < before
                and (not query or query.casefold() in row["event"].text.casefold())
                and (not cursor or (row["event"].occurred_at, row["event"].id) < cursor)]
        rows.sort(key=lambda event: (event.occurred_at, event.id), reverse=True)
        page = rows[:limit]
        next_cursor = f"{page[-1].occurred_at}|{page[-1].id}" if len(rows) > limit else None
        return page, next_cursor

    async def published_work(self, destination: Destination, limit: int = 30) -> list[str]:
        if destination.kind != 'group':return []
        ids=[]
        for row in sorted(self.outboxes.values(), key=lambda r:r.get('sent_at') or 0, reverse=True):
            if row['conversation'] != destination.key or row['status'] != 'sent':continue
            snapshot=self.decisions[row['decision_id']]['snapshot']
            tid=snapshot.get('work_task_id')
            if snapshot.get('deliver_work_file') and tid and tid not in ids:ids.append(tid)
        return ids[:limit]

    async def record_decision(self, event: Event, action: str, reason: str, snapshot: dict[str, Any], result: ModelResult | None) -> None:
        async with self._lock:
            if event.id in self.decisions:
                return
            self.decisions[event.id] = {"id": event.id, "event_id": event.id, "action": action, "reason": reason, "snapshot": snapshot, "created_at": utcnow().timestamp()}
            if result:
                parts = delivery_parts(result)
                for index, part in enumerate(parts):
                    outbox_id = event.id if index == 0 else event.id+':part:'+str(index)
                    self.outboxes[outbox_id] = {"id": outbox_id, "event_id": event.id, "decision_id": event.id,
                    "conversation": event.destination.key, "actor_id": event.actor_id,
                    # Quote only the first segment; follow-up segments are a
                    # continuation of the same reply and should read naturally.
                    "reply_to": event.message_id if index == 0 else None,
                    "text": part, "memory_ids": snapshot.get("memory_ids", []), "resource_kinds": snapshot.get("resources_used", []), "attachment_ids": result.attachment_ids if index == len(parts)-1 else [], "status": "pending",
                    "created_at": utcnow().timestamp(), "expires_at": min(event.occurred_at + snapshot["ttl"], snapshot.get('send_deadline', float('inf'))),
                    "platform_message_id": None, "provider": result.provider, "model": result.model,
                    "input_tokens": result.input_tokens if index == 0 else 0, "output_tokens": result.output_tokens if index == 0 else 0,
                    "sent_at": None, "error": None, "reviews": []}
        logging.getLogger(__name__).info("decision_recorded event_id=%s action=%s reason=%s",event.id,action,reason)

    async def claim_outbox(self) -> dict[str, Any] | None:
        async with self._lock:
            for item in self.outboxes.values():
                if item["status"] == "pending":
                    item["status"] = "sending"
                    return {**item, "dependency_snapshot": self.decisions[item["decision_id"]]["snapshot"]}
        return None

    async def finish_outbox(self, outbox_id: str, status: str, message_id: str | None = None, error: str | None = None) -> None:
        item = self.outboxes[outbox_id]
        item.update(status=status, platform_message_id=message_id, error=error)
        logging.getLogger(__name__).log(logging.WARNING if error else logging.INFO,
            "outbox_finished outbox_id=%s status=%s error=%s",outbox_id,status,error)
        if status == "sent":
            item["sent_at"] = utcnow().timestamp()
        elif status in {'unknown','failed','cancelled'}:
            for other in self.outboxes.values():
                if other['decision_id']==item['decision_id'] and other['status']=='pending':
                    other.update(status='cancelled',error='previous_part_not_sent')

    async def consent(self, actor: str) -> Consent:
        return self.consents.get(actor, Consent())

    async def set_consent(self, actor: str, enabled: bool, group_id: str | None = None) -> None:
        old = await self.consent(actor)
        groups = (old.disclosure_groups | {group_id}) if enabled and group_id else (old.disclosure_groups if enabled else frozenset())
        self.consents[actor] = Consent(enabled, frozenset(groups))

    async def visible_memories(self, actor: str, destination: Destination, query: str, limit: int = 8,
                               *, subject_id: str | None = None) -> list[Memory]:
        from osa.application.memory_search import rank_memories
        consent, policy = await self.consent(actor), await self.policy(destination)
        visible = [memory for memory in self.memories.values()
                   if (subject_id is None or memory.subject_id == subject_id)
                   and self.permissions.can_disclose(memory, actor, destination, consent, policy, utcnow().timestamp())
                   and self.events.get(memory.source_event_id, {}).get('collect', False)
                   and await self.event_active(memory.source_event_id)]
        visible.sort(key=lambda memory: (
            self.events[memory.source_event_id]['event'].occurred_at, memory.id), reverse=True)
        return rank_memories(visible, query, limit)

    async def add_memory(self, memory: Memory) -> Memory:
        from osa.application.memory_dedupe import duplicate_memory
        if memory.id in self.memories:
            return self.memories[memory.id]
        matches = [previous for previous in self.memories.values()
            if (previous.status == 'active' and previous.subject_id == memory.subject_id
                    and previous.origin_group_id == memory.origin_group_id and previous.scope == memory.scope
                    and duplicate_memory(memory.content, previous.content))]
        if matches:
            for duplicate in matches[1:]:
                await self.delete_memory(duplicate.id)
            return matches[0]
        self.memories[memory.id] = memory
        return memory

    async def own_memories(self, actor: str) -> list[Memory]:
        return [m for m in self.memories.values() if m.subject_id == actor and m.status == "active"]

    async def memories_by_ids(self, ids: list[str]) -> list[Memory]:
        return [self.memories[mid] for mid in ids if mid in self.memories]

    async def delete_memory(self, memory_id: str, actor: str | None = None) -> bool:
        memory = self.memories.get(memory_id)
        if memory is None or (actor is not None and memory.subject_id != actor):
            return False
        self.memories[memory_id] = replace(memory, content="", status="deleted")
        for decision in self.decisions.values():
            if memory_id in decision["snapshot"].get("memory_ids", []):
                decision["snapshot"] = {}
        for item in self.outboxes.values():
            if memory_id in item["memory_ids"]:
                item["text"] = ""
                if item["status"] == "pending":
                    item["status"] = "cancelled"
                for review in item["reviews"]:
                    review["note"], review["better_response"] = None, None
        return True

    async def correct_memory(self, memory_id: str, actor: str, content: str, source_id: str) -> bool:
        memory = self.memories.get(memory_id)
        if memory is None or memory.subject_id != actor or memory.status != "active":
            return False
        self.memories[memory_id] = replace(memory, content=content, source_event_id=source_id, version=memory.version + 1)
        for item in self.outboxes.values():
            if memory_id in item["memory_ids"] and item["status"] == "pending":
                item["status"], item["text"] = "cancelled", ""
        for decision in self.decisions.values():
            if memory_id in decision["snapshot"].get("memory_ids", []):
                decision["snapshot"] = {}
        return True

    async def save_resources(self, group_id: str, kind: str, items: list[dict[str, Any]]) -> None:
        existing = {row["platform_id"]: row for row in self.resource_rows if row["group_id"] == group_id and row["kind"] == kind and row["current"]}
        for item in items:
            digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
            previous = existing.get(item["id"])
            if previous and previous["content_hash"] == digest:
                previous.update(missing_count=0, observed_at=utcnow().timestamp())
                continue
            if previous:
                previous["current"] = False
            versions = [row["version"] for row in self.resource_rows if row["group_id"] == group_id and row["kind"] == kind and row["platform_id"] == item["id"]]
            self.resource_rows.append({"group_id": group_id, "kind": kind, "platform_id": item["id"], "payload": item,
                "content_hash": digest, "version": max(versions, default=0) + 1, "current": True, "missing_count": 0, "observed_at": utcnow().timestamp()})
        seen = {item["id"] for item in items}
        for pid, previous in existing.items():
            if pid not in seen:
                previous["missing_count"] += 1
                if previous["missing_count"] >= 2:
                    previous["current"] = False

    async def resources(self, group_id: str, kind: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.resource_rows if row["group_id"] == group_id and row["kind"] == kind and row["current"]]

    async def messages(self, conversation: str | None, query: str | None, status: str | None, cursor: float | None, limit: int) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.outboxes.values() if (not conversation or row["conversation"] == conversation) and (not query or query in row["text"]) and (not status or row["status"] == status) and (cursor is None or row["created_at"] < cursor)]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit]

    async def message_detail(self, message_id: str) -> dict[str, Any] | None:
        row = self.outboxes.get(message_id)
        return {**row, "decision": self.decisions[row["decision_id"]]} if row else None

    async def review(self, message_id: str, tags: list[str], better_response: str | None, note: str | None) -> str:
        if message_id not in self.outboxes:
            raise KeyError(message_id)
        review_id = f"review-{len(self.outboxes[message_id]['reviews']) + 1}"
        self.outboxes[message_id]["reviews"].append({"id": review_id, "tags": tags, "better_response": better_response, "note": note})
        return review_id

    async def daily(self, group_id: str, local_date: str, timezone: str) -> dict[str, Any]:
        key = f"{group_id}:{local_date}"
        old = self.daily_rows.get(key)
        if old and old["status"] == "active":
            return old
        start = datetime.fromisoformat(local_date).replace(tzinfo=ZoneInfo(timezone))
        end = start + timedelta(days=1)
        ids = [event_id for event_id, row in self.events.items() if row["collect"] and not row["recalled"] and row["event"].destination == Destination("group", group_id) and row["event"].event_type == "message.created" and start.timestamp() <= row["event"].occurred_at < end.timestamp()]
        result = {"id": key, "group_id": group_id, "local_date": local_date, "source_ids": ids, "summary": f"来源卡片：{len(ids)} 条消息；尚未进行模型事实巩固。", "status": "active"}
        self.daily_rows[key] = result
        return result

    async def stats(self) -> dict[str, Any]:
        from collections import Counter
        return {"events": len(self.events), "outbox": dict(Counter(row["status"] for row in self.outboxes.values()))}

    async def day_events(self, group_id: str, local_date: str, timezone: str) -> list[Event]:
        start = datetime.fromisoformat(local_date).replace(tzinfo=ZoneInfo(timezone))
        end = start + timedelta(days=1)
        return [row["event"] for row in self.events.values() if row["collect"] and not row["recalled"]
                and row["event"].event_type == "message.created" and row["event"].destination == Destination("group", group_id)
                and start.timestamp() <= row["event"].occurred_at < end.timestamp()]

    async def emit_result(self, event: Event, task_id: str, text: str, snapshot: dict) -> None:
        derived_id = hashlib.sha256(f"result:{task_id}".encode()).hexdigest()
        derived = replace(event, id=derived_id, dedupe_key=derived_id, event_type="task.result", text="", segments=[], occurred_at=utcnow().timestamp())
        await self.publish(derived, False)
        await self.finish_event(derived_id)
        await self.record_decision(derived, "TASK", "task_completed", {"ttl": 600, "source_ids": [event.id],
            "memory_ids": [], "memory_versions": {}, "task_id": task_id, **snapshot}, ModelResult(text, "workflow", task_id, attachment_ids=snapshot.get("attachment_ids", [])))

    async def purge_raw(self, before: float, actor: str | None = None, except_id: str | None = None) -> list[str]:
        ids = []
        for key, row in self.events.items():
            event = row["event"]
            if event.event_type == "message.created" and event.occurred_at < before and key != except_id and (not actor or event.actor_id == actor):
                row["event"], row["collect"] = replace(event, text="", segments=[]), False
                if actor:
                    row["recalled"] = True
                ids.append(key)
        for decision in self.decisions.values():
            if set(ids).intersection(decision["snapshot"].get("source_ids", [])):
                decision["snapshot"] = {}
        if actor:
            for memory in list(self.memories.values()):
                if memory.subject_id == actor or memory.source_event_id in ids:
                    await self.delete_memory(memory.id)
            await self.set_consent(actor, False)
        return ids
