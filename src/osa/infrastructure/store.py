import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, event as sql_event, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from osa.domain.entities import Consent, Destination, Event, GroupPolicy, Memory, ModelResult, Scope, utcnow
from osa.domain.policy import PermissionPolicy
from osa.infrastructure.schema import (
    AuditRow, Base, ConsentRow, ControlRow, DailyRow, DecisionRow, EventRow,
    GroupRow, MemoryRow, OutboxRow, ResourceRow, ReviewRow,
)
from osa.application.reply_delivery import delivery_parts


def memory_dto(row: MemoryRow) -> Memory:
    return Memory(row.id, row.content, Scope(row.scope), row.subject_id, row.origin_group_id,
                  row.source_event_id, row.status, row.expires_at, row.version)


def row_dict(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class SQLStore:
    """Transactional adapter for a single application with bounded async workers."""

    def __init__(self, url: str, pool_size: int = 5) -> None:
        parsed = make_url(url)
        in_memory = parsed.get_backend_name() == "sqlite" and parsed.database in {None, "", ":memory:"}
        options = {} if in_memory else {"pool_size": pool_size, "max_overflow": 0}
        self.engine: AsyncEngine = create_async_engine(url, **options)
        if url.startswith("sqlite"):
            @sql_event.listens_for(self.engine.sync_engine, "connect")
            def sqlite_pragmas(connection: Any, record: Any) -> None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA cache_size=-2048")
                cursor.execute("PRAGMA temp_store=FILE")
                cursor.execute("PRAGMA secure_delete=ON")
                cursor.close()
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._write_lock = asyncio.Lock()
        self._transient_events: dict[str, Event] = {}
        self.permissions = PermissionPolicy()

    async def initialize(self, groups: list[str], create_schema: bool = False) -> None:
        if create_schema:
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        async with self.session.begin() as session:
            for row in (await session.scalars(select(GroupRow))).all():
                if row.id not in groups:
                    row.settings = {**row.settings, "enabled": False}
            for group_id in groups:
                if await session.get(GroupRow, group_id) is None:
                    session.add(GroupRow(id=group_id, settings=asdict(GroupPolicy())))
            if await session.get(ControlRow, "global") is None:
                session.add(ControlRow(id="global", paused=False))
            # Crash recovery: generation can restart; ambiguous sends cannot.
            await session.execute(update(EventRow).where(EventRow.status == "processing").values(status="pending"))
            await session.execute(update(OutboxRow).where(OutboxRow.status == "sending").values(status="unknown", error="process_restarted_during_send"))
        self.permissions.group_memory_sources.clear()
        for row in await self.groups():
            self.permissions.update_group_memory_source(row['id'], GroupPolicy(**row['settings']))

    async def close(self) -> None:
        await self.engine.dispose()

    async def health(self) -> bool:
        async with self.session() as session:
            await session.execute(text("SELECT 1"))
        return True

    async def policy(self, destination: Destination) -> GroupPolicy:
        if destination.kind == "private":
            return GroupPolicy()
        async with self.session() as session:
            row = await session.get(GroupRow, destination.id)
            return GroupPolicy(**row.settings) if row else GroupPolicy(enabled=False)

    async def groups(self) -> list[dict[str, Any]]:
        async with self.session() as session:
            return [row_dict(row) for row in (await session.scalars(select(GroupRow).order_by(GroupRow.id))).all()]

    async def update_policy(self, group_id: str, policy: GroupPolicy) -> None:
        async with self._write_lock, self.session.begin() as session:
            row = await session.get(GroupRow, group_id)
            if row is None:
                raise KeyError(group_id)
            row.settings = asdict(policy)
            session.add(AuditRow(action="group_policy_updated", resource_id=group_id, detail=asdict(policy), created_at=utcnow().timestamp()))
        self.permissions.update_group_memory_source(group_id, policy)

    async def ensure_group(self, group_id: str) -> None:
        async with self._write_lock, self.session.begin() as session:
            if await session.get(GroupRow, group_id) is None:
                session.add(GroupRow(id=group_id, settings=asdict(GroupPolicy())))
        self.permissions.update_group_memory_source(group_id, await self.policy(Destination('group', group_id)))

    async def paused(self) -> bool:
        async with self.session() as session:
            row = await session.get(ControlRow, "global")
            return row is None or row.paused

    async def set_paused(self, paused: bool) -> None:
        async with self._write_lock, self.session.begin() as session:
            row = await session.get(ControlRow, "global")
            if row:
                row.paused = paused
            session.add(AuditRow(action="pause_bot", resource_id="global", detail={"paused": paused}, created_at=utcnow().timestamp()))

    async def publish(self, event: Event, collect: bool) -> bool:
        try:
            async with self._write_lock, self.session.begin() as session:
                was_recalled = await session.scalar(select(EventRow.id).where(
                    EventRow.conversation == event.destination.key,
                    EventRow.message_id == event.message_id,
                    EventRow.event_type == "message.recalled",
                ).limit(1)) if event.event_type == "message.created" else None
                session.add(EventRow(
                    id=event.id, dedupe_key=event.dedupe_key, conversation=event.destination.key,
                    message_id=event.message_id, actor_id=event.actor_id, event_type=event.event_type,
                    payload=event.to_dict() if collect else {**event.to_dict(), "text": "", "segments": []},
                    occurred_at=event.occurred_at,
                    received_at=utcnow().timestamp(), collect=collect, recalled=bool(was_recalled),
                ))
            if not collect:
                self._transient_events[event.id] = event
            return True
        except IntegrityError:
            return False

    async def claim_events(self, limit: int, exclude_conversations: set[str] | None = None) -> list[Event]:
        async with self._write_lock, self.session.begin() as session:
            statement = select(EventRow).where(EventRow.status == "pending")
            if exclude_conversations:
                statement = statement.where(EventRow.conversation.not_in(exclude_conversations))
            rows = (await session.scalars(statement
                    .order_by(EventRow.received_at, EventRow.id).limit(limit).with_for_update(skip_locked=True))).all()
            events = []
            for row in rows:
                transient = self._transient_events.pop(row.id, None) if not row.collect else None
                if not row.collect and transient is None:
                    row.status = "done"
                    continue
                row.status = "processing"
                events.append(transient or Event.from_dict(row.payload))
            return events

    async def finish_event(self, event_id: str, error: str | None = None) -> None:
        async with self._write_lock, self.session.begin() as session:
            row = await session.get(EventRow, event_id)
            if row:
                row.status, row.error = ("failed" if error else "done"), error
                if not row.collect:
                    row.payload = {**row.payload, "text": "", "segments": []}

    async def _invalidate_snapshots(self, session: Any, key: str, references: set[str]) -> None:
        if not references:
            return
        cursor = None
        while True:
            statement = select(DecisionRow.id, DecisionRow.snapshot[key]).order_by(DecisionRow.id).limit(200)
            if cursor is not None:
                statement = statement.where(DecisionRow.id > cursor)
            rows = (await session.execute(statement)).all()
            if not rows:
                return
            matches = [identifier for identifier, values in rows if values and references.intersection(values)]
            if matches:
                await session.execute(update(DecisionRow).where(DecisionRow.id.in_(matches)).values(snapshot={})
                                      .execution_options(synchronize_session=False))
            cursor = rows[-1][0]

    async def recall(self, event: Event) -> None:
        async with self._write_lock, self.session.begin() as session:
            rows = (await session.scalars(select(EventRow).where(
                EventRow.conversation == event.destination.key,
                EventRow.message_id == event.message_id, EventRow.event_type.in_(["message.created","group.file_uploaded"]),
            ))).all()
            for row in rows:
                row.recalled = True
                self._transient_events.pop(row.id, None)
                row.payload = {**row.payload, "text": "", "segments": []}
                await session.execute(update(MemoryRow).where(MemoryRow.source_event_id == row.id)
                                      .values(status="deleted", content=""))
                await session.execute(update(OutboxRow).where(OutboxRow.event_id == row.id, OutboxRow.status == "pending")
                                      .values(status="cancelled", text="", error="source_recalled"))
                dailies = (await session.scalars(select(DailyRow).where(DailyRow.group_id == event.destination.id))).all()
                for daily in dailies:
                    if row.id in daily.source_ids:
                        daily.summary, daily.status = "", "invalidated"
            await self._invalidate_snapshots(session, 'source_ids', {row.id for row in rows})

    async def event_active(self, event_id: str) -> bool:
        async with self.session() as session:
            row = await session.get(EventRow, event_id)
            return row is not None and not row.recalled

    async def event_by_message(self, destination: Destination, message_id: str) -> Event | None:
        async with self.session() as session:
            row=await session.scalar(select(EventRow).where(EventRow.conversation==destination.key,
                EventRow.message_id==message_id,EventRow.recalled.is_(False),EventRow.event_type=='message.created').limit(1))
            return (self._transient_events.get(row.id) or Event.from_dict(row.payload)) if row else None

    async def reply_is_bot(self, event: Event) -> bool:
        if not event.reply_to_id:
            return False
        async with self.session() as session:
            found = await session.scalar(select(OutboxRow.id).where(
                OutboxRow.conversation == event.destination.key, OutboxRow.status == "sent",
                OutboxRow.platform_message_id == event.reply_to_id,
            ).limit(1))
            return found is not None

    async def _reusable_replies(self, session, replies, destination: Destination):
        ids = {memory_id for reply in replies for memory_id in reply.memory_ids}
        memories = {}
        if ids:
            rows = (await session.scalars(select(MemoryRow).where(MemoryRow.id.in_(ids)))).all()
            memories = {row.id: memory_dto(row) for row in rows}
        now = utcnow().timestamp()
        return [reply for reply in replies if self.permissions.can_reuse_group_reply(
            reply.memory_ids, reply.resource_kinds, memories, destination, now)]

    async def context(self, event: Event, limit: int) -> list[dict[str, str]]:
        from osa.application.message_context import render_event
        from osa.application.conversation_prompt import message_metadata
        async with self.session() as session:
            rows = (await session.scalars(select(EventRow).where(
                EventRow.conversation == event.destination.key, EventRow.collect.is_(True),
                EventRow.recalled.is_(False), EventRow.event_type == "message.created",
                EventRow.occurred_at <= event.occurred_at,
            ).order_by(EventRow.occurred_at.desc(), EventRow.id.desc()).limit(limit))).all()
            bot_rows = (await session.scalars(select(OutboxRow).where(
                OutboxRow.conversation == event.destination.key, OutboxRow.status == "sent",
                func.json_array_length(OutboxRow.resource_kinds) == 0,
                OutboxRow.sent_at <= event.occurred_at,
            ).order_by(OutboxRow.sent_at.desc()).limit(limit * 3))).all()
            bot_rows = (await self._reusable_replies(session, bot_rows, event.destination))[:limit]
            combined = [(row.occurred_at, {"role": "user", "content": render_event(row.payload, include_id=True), "source_id": row.id,
                                          **message_metadata(row.payload)}) for row in rows]
            combined.extend((row.sent_at or 0, {"role": "assistant", "content": row.text, "source_id": row.event_id,
                "actor_id": row.actor_id, "occurred_at": row.sent_at, "reply_to_id": row.reply_to,
                "message_id": row.platform_message_id}) for row in bot_rows)
            return [message for _, message in sorted(combined, key=lambda x: x[0])[-limit:]]

    async def quoted_context(self, event: Event) -> dict[str, str] | None:
        if not event.reply_to_id:
            return None
        from osa.application.message_context import render_event
        async with self.session() as session:
            source = await session.scalar(select(EventRow).where(
                EventRow.conversation == event.destination.key,
                EventRow.message_id == event.reply_to_id,
                EventRow.event_type == "message.created",
                EventRow.collect.is_(True), EventRow.recalled.is_(False)).limit(1))
            if source:
                return {"content": render_event(source.payload, include_id=True), "source_id": source.id}
            reply = await session.scalar(select(OutboxRow).where(
                OutboxRow.conversation == event.destination.key,
                OutboxRow.platform_message_id == event.reply_to_id,
                OutboxRow.status == "sent").limit(1))
            if reply:
                parts = (await session.scalars(select(OutboxRow).where(
                    OutboxRow.decision_id == reply.decision_id, OutboxRow.status == "sent")
                    .order_by(OutboxRow.created_at))).all()
                if len(await self._reusable_replies(session, parts, event.destination)) != len(parts):
                    return None
                return {"content": "[引用的 Nova 回复] " + "".join(part.text for part in parts),
                        "source_id": reply.event_id}
        return None

    async def search_events(self, destination: Destination, after: float, before: float,
                            query: str, cursor: tuple[float, str] | None, limit: int) -> tuple[list[Event], str | None]:
        async with self.session() as session:
            statement = select(EventRow).where(
                EventRow.conversation == destination.key,
                EventRow.event_type == "message.created",
                EventRow.collect.is_(True), EventRow.recalled.is_(False),
                EventRow.occurred_at >= after, EventRow.occurred_at < before,
            )
            if query:
                statement = statement.where(func.lower(EventRow.payload["text"].as_string()).contains(query.casefold()))
            if cursor:
                statement = statement.where(or_(EventRow.occurred_at < cursor[0],
                    and_(EventRow.occurred_at == cursor[0], EventRow.id < cursor[1])))
            rows = (await session.scalars(statement.order_by(EventRow.occurred_at.desc(), EventRow.id.desc())
                                  .limit(limit + 1))).all()
            page = rows[:limit]
            next_cursor = f"{page[-1].occurred_at}|{page[-1].id}" if len(rows) > limit else None
            return [Event.from_dict(row.payload) for row in page], next_cursor

    async def published_work(self, destination: Destination, limit: int = 30) -> list[str]:
        if destination.kind != 'group':return []
        async with self.session() as session:
            rows = (await session.scalars(select(DecisionRow).join(OutboxRow, OutboxRow.decision_id == DecisionRow.id)
                .where(OutboxRow.conversation == destination.key, OutboxRow.status == 'sent',
                    DecisionRow.snapshot['deliver_work_file'].as_boolean().is_(True))
                .order_by(OutboxRow.sent_at.desc()).limit(limit))).all()
            return list(dict.fromkeys(row.snapshot['work_task_id'] for row in rows
                if row.snapshot.get('deliver_work_file') and row.snapshot.get('work_task_id')))[:limit]

    async def record_decision(self, event: Event, action: str, reason: str, snapshot: dict[str, Any], result: ModelResult | None) -> None:
        async with self._write_lock, self.session.begin() as session:
            if await session.scalar(select(DecisionRow.id).where(DecisionRow.event_id == event.id)):
                return
            decision = DecisionRow(event_id=event.id, action=action, reason=reason, snapshot=snapshot, created_at=utcnow().timestamp())
            session.add(decision)
            await session.flush()
            if result:
                parts = delivery_parts(result)
                for index, part in enumerate(parts):
                    session.add(OutboxRow(
                    event_id=event.id, decision_id=decision.id, conversation=event.destination.key,
                    # The persisted column is NOT NULL.  Empty string means
                    # no quote to OneBot; None would roll back every segment.
                    actor_id=event.actor_id, reply_to=event.message_id if index == 0 else "", text=part,
                    memory_ids=snapshot.get("memory_ids", []), provider=result.provider, model=result.model,
                    resource_kinds=snapshot.get("resources_used", []),
                    attachment_ids=result.attachment_ids if index == len(parts)-1 else [],
                    input_tokens=result.input_tokens if index == 0 else 0, output_tokens=result.output_tokens if index == 0 else 0,
                    created_at=utcnow().timestamp()+index*.0001,
                    expires_at=min(event.occurred_at + snapshot["ttl"], snapshot.get('send_deadline', float('inf'))),
                ))
        logging.getLogger(__name__).info("decision_recorded event_id=%s action=%s reason=%s",event.id,action,reason)

    async def claim_outbox(self) -> dict[str, Any] | None:
        async with self._write_lock, self.session.begin() as session:
            row = await session.scalar(select(OutboxRow).where(OutboxRow.status == "pending")
                .order_by(OutboxRow.created_at).limit(1).with_for_update(skip_locked=True))
            if row is None:
                return None
            row.status = "sending"
            decision = await session.get(DecisionRow, row.decision_id)
            return {**row_dict(row), "dependency_snapshot": decision.snapshot if decision else {}}

    async def finish_outbox(self, outbox_id: str, status: str, message_id: str | None = None, error: str | None = None) -> None:
        async with self._write_lock, self.session.begin() as session:
            row = await session.get(OutboxRow, outbox_id)
            if row:
                row.status, row.platform_message_id, row.error = status, message_id, error
                if status == "sent":
                    row.sent_at = utcnow().timestamp()
                elif status in {'unknown','failed','cancelled'}:
                    await session.execute(update(OutboxRow).where(OutboxRow.decision_id==row.decision_id,
                        OutboxRow.status=='pending').values(status='cancelled',error='previous_part_not_sent'))
        logging.getLogger(__name__).log(logging.WARNING if error else logging.INFO,
            "outbox_finished outbox_id=%s status=%s error=%s",outbox_id,status,error)

    async def consent(self, actor: str) -> Consent:
        async with self.session() as session:
            row = await session.get(ConsentRow, actor)
            return Consent(row.enabled, frozenset(row.groups)) if row else Consent()

    async def set_consent(self, actor: str, enabled: bool, group_id: str | None = None) -> None:
        async with self._write_lock, self.session.begin() as session:
            row = await session.get(ConsentRow, actor)
            if row is None:
                row = ConsentRow(actor_id=actor, groups=[], version=0)
                session.add(row)
            row.enabled, row.version = enabled, row.version + 1
            if not enabled:
                row.groups = []
            elif group_id:
                row.groups = sorted(set(row.groups) | {group_id})
            session.add(AuditRow(action="memory_consent_changed", resource_id=actor,
                detail={"enabled": enabled, "group_id": group_id, "version": row.version}, created_at=utcnow().timestamp()))

    async def visible_memories(self, actor: str, destination: Destination, query: str, limit: int = 8,
                               *, subject_id: str | None = None) -> list[Memory]:
        from osa.application.memory_search import rank_memories, search_terms
        consent, group = await self.consent(actor), await self.policy(destination)
        now = utcnow().timestamp()
        clauses = []
        if destination.kind == "group":
            groups = {destination.id}
            if self.permissions.global_group_memory:
                groups.update(self.permissions.group_memory_sources)
            clauses.append(and_(MemoryRow.origin_group_id.in_(groups),
                MemoryRow.scope.in_([Scope.LONG_GROUP, Scope.DAILY_GROUP, Scope.GROUP_RESOURCE])))
        else:
            clauses.append(and_(MemoryRow.subject_id == actor, MemoryRow.scope == Scope.PRIVATE_USER))
        if consent.enabled and (destination.kind == "private" or (group.cross_memory and destination.id in consent.disclosure_groups)):
            clauses.append(and_(MemoryRow.subject_id == actor, MemoryRow.scope == Scope.PERSONAL_OWNER))
        # Permission predicates and keyword matching run in SQL before content is materialized.
        terms = search_terms(query)
        keyword_clause = or_(*(func.lower(MemoryRow.content).contains(term, autoescape=True)
                               for term in terms)) if terms else None
        filters = [MemoryRow.status == "active",
                   or_(MemoryRow.expires_at.is_(None), MemoryRow.expires_at > now), or_(*clauses)]
        if keyword_clause is not None:
            filters.append(keyword_clause)
        if subject_id is not None:
            filters.append(MemoryRow.subject_id == subject_id)
        async with self.session() as session:
            statement = select(MemoryRow).join(EventRow, EventRow.id == MemoryRow.source_event_id).where(
                *filters, EventRow.collect.is_(True), EventRow.recalled.is_(False)).order_by(
                EventRow.occurred_at.desc(), MemoryRow.id)
            if not terms:
                statement = statement.limit(500)
            rows = (await session.scalars(statement)).all()
        visible = [memory_dto(row) for row in rows if self.permissions.can_disclose(memory_dto(row), actor, destination, consent, group, now)]
        return rank_memories(visible, query, limit)

    async def add_memory(self, memory: Memory) -> Memory:
        from osa.application.memory_dedupe import duplicate_memory
        duplicates = []
        async with self._write_lock, self.session.begin() as session:
            existing = await session.get(MemoryRow, memory.id)
            if existing is not None:
                return memory_dto(existing)
            candidates = (await session.scalars(select(MemoryRow).join(
                EventRow, EventRow.id == MemoryRow.source_event_id).where(
                MemoryRow.status == 'active', MemoryRow.subject_id == memory.subject_id,
                MemoryRow.origin_group_id == memory.origin_group_id,
                MemoryRow.scope == memory.scope).order_by(EventRow.occurred_at, MemoryRow.id)
                .limit(500))).all()
            matches = [row for row in candidates if duplicate_memory(memory.content, row.content)]
            if matches:
                saved = memory_dto(matches[0])
                duplicates = [row.id for row in matches[1:]]
            else:
                session.add(MemoryRow(**asdict(memory)))
                saved = memory
        for duplicate_id in duplicates:
            await self.delete_memory(duplicate_id)
        return saved

    async def own_memories(self, actor: str) -> list[Memory]:
        async with self.session() as session:
            rows = (await session.scalars(select(MemoryRow).where(MemoryRow.subject_id == actor, MemoryRow.status == "active").limit(100))).all()
            return [memory_dto(row) for row in rows]

    async def memories_by_ids(self, ids: list[str]) -> list[Memory]:
        if not ids:
            return []
        async with self.session() as session:
            return [memory_dto(row) for row in (await session.scalars(select(MemoryRow).where(MemoryRow.id.in_(ids)))).all()]

    async def delete_memory(self, memory_id: str, actor: str | None = None) -> bool:
        async with self._write_lock, self.session.begin() as session:
            row = await session.get(MemoryRow, memory_id)
            if not row or (actor is not None and row.subject_id != actor):
                return False
            row.content, row.status = "", "deleted"
            await self._invalidate_snapshots(session, 'memory_ids', {memory_id})
            outboxes = (await session.scalars(select(OutboxRow))).all()
            for outbox in outboxes:
                if memory_id in outbox.memory_ids:
                    outbox.text = ""
                    if outbox.status == "pending":
                        outbox.status = "cancelled"
                    reviews = (await session.scalars(select(ReviewRow).where(ReviewRow.outbox_id == outbox.id))).all()
                    for review in reviews:
                        review.note, review.better_response = None, None
            session.add(AuditRow(action="memory_deleted", resource_id=memory_id, detail={"actor_id": actor or "system-admin"}, created_at=utcnow().timestamp()))
            return True

    async def correct_memory(self, memory_id: str, actor: str, content: str, source_id: str) -> bool:
        async with self._write_lock, self.session.begin() as session:
            row = await session.get(MemoryRow, memory_id)
            if row is None or row.subject_id != actor or row.status != "active":
                return False
            row.content, row.source_event_id = content, source_id
            row.version += 1
            # Invalidate previously generated replies instead of sending an old fact.
            outboxes = (await session.scalars(select(OutboxRow).where(OutboxRow.status == "pending"))).all()
            for outbox in outboxes:
                if memory_id in outbox.memory_ids:
                    outbox.status, outbox.text = "cancelled", ""
            await self._invalidate_snapshots(session, 'memory_ids', {memory_id})
            session.add(AuditRow(action="memory_corrected", resource_id=memory_id,
                detail={"actor_id": actor, "version": row.version}, created_at=utcnow().timestamp()))
            return True

    async def save_resources(self, group_id: str, kind: str, items: list[dict[str, Any]]) -> None:
        async with self._write_lock, self.session.begin() as session:
            rows = (await session.scalars(select(ResourceRow).where(ResourceRow.group_id == group_id, ResourceRow.kind == kind, ResourceRow.current.is_(True)))).all()
            current = {row.platform_id: row for row in rows}
            seen = set()
            for item in items:
                platform_id = item["id"]
                seen.add(platform_id)
                digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                previous = current.get(platform_id)
                if previous and previous.content_hash == digest:
                    previous.observed_at, previous.missing_count = utcnow().timestamp(), 0
                    continue
                if previous:
                    previous.current = False
                latest_version = await session.scalar(select(func.max(ResourceRow.version)).where(
                    ResourceRow.group_id == group_id, ResourceRow.kind == kind, ResourceRow.platform_id == platform_id,
                ))
                session.add(ResourceRow(group_id=group_id, kind=kind, platform_id=platform_id,
                    payload=item, content_hash=digest, version=(latest_version or 0) + 1,
                    observed_at=utcnow().timestamp()))
            for platform_id, previous in current.items():
                if platform_id not in seen:
                    previous.missing_count += 1
                    if previous.missing_count >= 2:
                        previous.current = False

    async def resources(self, group_id: str, kind: str) -> list[dict[str, Any]]:
        async with self.session() as session:
            rows = (await session.scalars(select(ResourceRow).where(ResourceRow.group_id == group_id, ResourceRow.kind == kind, ResourceRow.current.is_(True)).order_by(ResourceRow.observed_at.desc()))).all()
            return [row_dict(row) for row in rows]

    async def messages(self, conversation: str | None, query: str | None, status: str | None, cursor: float | None, limit: int) -> list[dict[str, Any]]:
        statement = select(OutboxRow)
        if conversation:
            statement = statement.where(OutboxRow.conversation == conversation)
        if query:
            statement = statement.where(OutboxRow.text.contains(query, autoescape=True))
        if status:
            statement = statement.where(OutboxRow.status == status)
        if cursor is not None:
            statement = statement.where(OutboxRow.created_at < cursor)
        async with self.session() as session:
            return [row_dict(row) for row in (await session.scalars(statement.order_by(OutboxRow.created_at.desc()).limit(limit))).all()]

    async def message_detail(self, message_id: str) -> dict[str, Any] | None:
        async with self.session() as session:
            outbox = await session.get(OutboxRow, message_id)
            if outbox is None:
                return None
            decision = await session.get(DecisionRow, outbox.decision_id)
            reviews = (await session.scalars(select(ReviewRow).where(ReviewRow.outbox_id == message_id))).all()
            return {**row_dict(outbox), "decision": row_dict(decision), "reviews": [row_dict(row) for row in reviews]}

    async def review(self, message_id: str, tags: list[str], better_response: str | None, note: str | None) -> str:
        async with self._write_lock, self.session.begin() as session:
            if await session.get(OutboxRow, message_id) is None:
                raise KeyError(message_id)
            row = ReviewRow(outbox_id=message_id, tags=tags, better_response=better_response, note=note, created_at=utcnow().timestamp())
            session.add(row)
            await session.flush()
            return row.id

    async def daily(self, group_id: str, local_date: str, timezone: str) -> dict[str, Any]:
        """Idempotent source card, NOT model-verified facts or full consolidation."""
        start = datetime.fromisoformat(local_date).replace(tzinfo=ZoneInfo(timezone))
        end = start + timedelta(days=1)
        async with self._write_lock, self.session.begin() as session:
            previous = await session.scalar(select(DailyRow).where(DailyRow.group_id == group_id, DailyRow.local_date == local_date))
            if previous and previous.status == "active":
                return row_dict(previous)
            sources = (await session.scalars(select(EventRow).where(
                EventRow.conversation == f"group:{group_id}", EventRow.occurred_at >= start.timestamp(),
                EventRow.occurred_at < end.timestamp(), EventRow.recalled.is_(False),
                EventRow.collect.is_(True), EventRow.event_type == "message.created",
            ).order_by(EventRow.occurred_at))).all()
            values = {"source_ids": [row.id for row in sources], "summary": f"来源卡片：{len(sources)} 条消息；尚未进行模型事实巩固。", "status": "active"}
            if previous:
                for key, value in values.items():
                    setattr(previous, key, value)
                row = previous
            else:
                row = DailyRow(group_id=group_id, local_date=local_date, **values)
                session.add(row)
            await session.flush()
            return row_dict(row)

    async def stats(self) -> dict[str, Any]:
        async with self.session() as session:
            events = await session.scalar(select(func.count()).select_from(EventRow))
            states = (await session.execute(select(OutboxRow.status, func.count()).group_by(OutboxRow.status))).all()
            return {"events": events, "outbox": dict(states)}

    async def day_events(self, group_id: str, local_date: str, timezone: str) -> list[Event]:
        start = datetime.fromisoformat(local_date).replace(tzinfo=ZoneInfo(timezone))
        end = start + timedelta(days=1)
        async with self.session() as session:
            rows = (await session.scalars(select(EventRow).where(EventRow.conversation == "group:" + group_id,
                EventRow.event_type == "message.created", EventRow.collect.is_(True), EventRow.recalled.is_(False),
                EventRow.occurred_at >= start.timestamp(), EventRow.occurred_at < end.timestamp())
                .order_by(EventRow.occurred_at).limit(5000))).all()
            return [Event.from_dict(row.payload) for row in rows]

    async def emit_result(self, event: Event, task_id: str, text_value: str, snapshot: dict[str, Any]) -> None:
        from dataclasses import replace
        synthetic_id = hashlib.sha256(f"result:{task_id}".encode()).hexdigest()
        derived = replace(event, id=synthetic_id, dedupe_key=synthetic_id, event_type="task.result",
                          text="", segments=[], occurred_at=utcnow().timestamp())
        async with self._write_lock, self.session.begin() as session:
            if await session.get(EventRow, synthetic_id) is None:
                session.add(EventRow(id=synthetic_id, dedupe_key=synthetic_id, conversation=event.destination.key,
                    message_id=event.message_id, actor_id=event.actor_id, event_type="task.result", payload=derived.to_dict(),
                    status="done", collect=False, recalled=False, occurred_at=derived.occurred_at, received_at=derived.occurred_at))
        await self.record_decision(derived, "TASK", "task_completed", {"ttl": 600, "source_ids": [event.id],
            "memory_ids": [], "memory_versions": {}, "task_id": task_id, **snapshot}, ModelResult(text_value, "workflow", task_id, attachment_ids=snapshot.get("attachment_ids", [])))

    async def purge_raw(self, before: float, actor: str | None = None, except_id: str | None = None) -> list[str]:
        async with self._write_lock, self.session.begin() as session:
            statement = select(EventRow).where(EventRow.occurred_at < before, EventRow.event_type == "message.created")
            if actor:
                statement = statement.where(EventRow.actor_id == actor)
            if except_id:
                statement = statement.where(EventRow.id != except_id)
            rows = (await session.scalars(statement)).all()
            ids = {row.id for row in rows}
            for row in rows:
                row.payload = {**row.payload, "text": "", "segments": []}
                row.collect = False
                if actor:
                    row.recalled = True
                self._transient_events.pop(row.id, None)
            await self._invalidate_snapshots(session, 'source_ids', ids)
            if actor:
                await session.execute(update(MemoryRow).where(or_(MemoryRow.subject_id == actor, MemoryRow.source_event_id.in_(ids)))
                                      .values(status="deleted", content=""))
                consent = await session.get(ConsentRow, actor)
                if consent:
                    consent.enabled, consent.groups = False, []
            return sorted(ids)
