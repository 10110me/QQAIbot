"""Owner console: chat flow, runtime allowlist and operational logs."""
import asyncio
import logging
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from osa.domain.entities import Destination, GroupPolicy
from osa.interfaces.api import require_admin, response
from osa.infrastructure.operations import Allowlist
from osa.infrastructure.log_localization import VALUES

router = APIRouter(prefix="/api/v1/operations", dependencies=[Depends(require_admin)])
logger = logging.getLogger(__name__)


async def retained_sources(state, ids):
    ids = list(dict.fromkeys(ids))[:1000]
    if not ids:
        return {}
    if hasattr(state.store, 'session'):
        from sqlalchemy import select
        from osa.infrastructure.schema import EventRow
        async with state.store.session() as session:
            rows = (await session.scalars(select(EventRow).where(EventRow.id.in_(ids), EventRow.collect.is_(True), EventRow.recalled.is_(False)))).all()
            return {r.id:r.payload for r in rows}
    return {key:state.store.events[key]['event'].to_dict() for key in ids
            if key in state.store.events and state.store.events[key]['collect'] and not state.store.events[key]['recalled']}


@router.get('/memories')
async def memory_library(request: Request, q: Annotated[str, Query(max_length=200)] = '',
                         group_id: Annotated[str, Query(max_length=20)] = '',
                         actor_id: Annotated[str, Query(max_length=20)] = '',
                         kind: Annotated[str, Query(pattern=r'^(all|personal|knowledge|shared)$')] = 'all',
                         page: Annotated[int, Query(ge=1)] = 1,
                         page_size: Annotated[int, Query(ge=1, le=60)] = 24):
    """Owner-only, bounded read model for the memory library."""
    state = request.app.state
    if kind in {'all', 'personal'} and hasattr(state.store, 'session'):
        from sqlalchemy import select
        from osa.infrastructure.schema import EventRow, MemoryRow
        from osa.infrastructure.store import memory_dto
        statement = select(MemoryRow).where(MemoryRow.status == 'active')
        if group_id:
            statement = statement.where(MemoryRow.origin_group_id == group_id)
        if actor_id:
            statement = statement.where(MemoryRow.subject_id == actor_id)
        if q:
            statement = statement.where(MemoryRow.content.contains(q, autoescape=True))
        async with state.store.session() as session:
            personal_rows = (await session.execute(statement.join(EventRow, EventRow.id == MemoryRow.source_event_id)
                .with_only_columns(MemoryRow, EventRow.occurred_at).order_by(EventRow.occurred_at.desc(), MemoryRow.id.desc())
                .limit(10000))).all()
            personal = [(memory_dto(row), created_at) for row, created_at in personal_rows]
    elif kind in {'all', 'personal'}:
        personal = [(row, state.store.events.get(row.source_event_id, {}).get('event').occurred_at
                     if state.store.events.get(row.source_event_id, {}).get('event') else 0)
                    for row in state.store.memories.values() if row.status == 'active'
                    and (not group_id or row.origin_group_id == group_id)
                    and (not actor_id or row.subject_id == actor_id) and (not q or q.lower() in row.content.lower())]
    else:
        personal = []
    values = []
    if kind in {'all', 'personal'}:
        values += [{'id': row.id, 'kind': 'personal', 'content': row.content, 'scope': str(row.scope),
                    'visibility': 'shared_groups' if state.store.permissions.shares_group_memory(
                        row.origin_group_id, Destination('group', row.origin_group_id or ''))
                        and str(row.scope) in {'long_group', 'daily_group', 'group_resource'} else 'conversation',
                    'actor_id': row.subject_id, 'group_id': row.origin_group_id, 'source_ids': [row.source_event_id],
                    'version': row.version, 'status': row.status, 'created_at': created_at}
                   for row, created_at in personal]
        if state.features:
            for row in await state.features.repo.find('person_alias', status='active', limit=300):
                if (group_id and row.group_id != group_id) or (actor_id and row.owner_id != actor_id):
                    continue
                if q and q not in row.payload.get('alias',''):
                    continue
                values.append({'id':row.id,'kind':'personal','content':'群内常用称呼：'+row.payload['alias'],
                    'actor_id':row.owner_id,'group_id':row.group_id,'scope':'long_group','version':1,
                    'status':'active','source_ids':row.payload.get('source_ids',[]), 'created_at':row.created_at})
    if state.features and kind != 'personal' and not actor_id:
        for row in await state.features.repo.find('knowledge_candidate', limit=1000):
            if row.status not in {'candidate', 'shared'} or (group_id and row.payload.get('origin_group_id') != group_id):
                continue
            if kind == 'shared' and row.status != 'shared':
                continue
            if kind == 'knowledge' and row.status != 'candidate':
                continue
            if q and q.lower() not in str(row.payload.get('content', '')).lower():
                continue
            values.append({'id': row.id, 'kind': 'shared' if row.status == 'shared' else 'knowledge',
                'content': row.payload.get('content', ''), 'scope': 'global_public_knowledge' if row.status == 'shared' else 'long_group',
                'group_id': row.payload.get('origin_group_id'), 'source_ids': row.payload.get('source_ids', []),
                'learned_at': row.payload.get('learned_at'), 'created_at': row.created_at,
                'status': 'unverified', 'version': 1})
    values.sort(key=lambda item: (item.get('created_at') or 0, item['id']), reverse=True)
    total = len(values)
    visible = values[(page - 1) * page_size:page * page_size]
    evidence = {}
    if state.features:
        for row in await state.features.repo.find('memory_evidence', status='active', limit=2000):
            evidence[row.payload.get('memory_id')] = row.payload.get('source_ids', [])
    for item in visible:
        item['source_ids'] = list(dict.fromkeys(item['source_ids']+evidence.get(item['id'], [])))[:40]
    sources = await retained_sources(state, [sid for item in visible for sid in item['source_ids']])
    for item in visible:
        item['sources'] = [{'id':sid, 'actor_id':sources[sid]['actor_id'], 'actor_name':sources[sid].get('actor_name',''),
            'text':sources[sid].get('text',''), 'ts':sources[sid]['occurred_at']} for sid in item['source_ids'] if sid in sources]
        item['actor_name'] = next((row['actor_name'] for row in item['sources'] if row['actor_id']==item.get('actor_id')), '')
    return JSONResponse(response({'items': visible, 'total': total, 'page': page,
        'page_size': page_size, 'pages': (total + page_size - 1) // page_size,
        'global_group_memory': state.store.permissions.global_group_memory}),
        headers={'Cache-Control': 'no-store'})


@router.get('/memories/{memory_id}/context')
async def memory_context(memory_id: str, request: Request):
    state = request.app.state
    rows = await state.store.memories_by_ids([memory_id])
    alias = await state.features.repo.get(memory_id) if not rows and state.features else None
    if (not rows or rows[0].status != 'active') and not (alias and alias.kind=='person_alias' and alias.status=='active'):
        raise HTTPException(404, 'RESOURCE_NOT_FOUND')
    sources = await retained_sources(state, [rows[0].source_event_id] if rows else alias.payload.get('source_ids',[]))
    if not sources:
        return response({'items':[], 'note':'原始消息已撤回或过期，无法补回上下文。'})
    from osa.domain.entities import Event
    source = Event.from_dict(next(iter(sources.values())))
    context = await state.store.context(source, 8)
    return JSONResponse(response({'items':context}), headers={'Cache-Control':'no-store'})


@router.delete('/memories/{memory_id}')
async def delete_library_memory(memory_id: str, request: Request):
    state = request.app.state
    row = await state.features.repo.get(memory_id) if state.features else None
    if row and row.kind in {'knowledge_candidate','person_alias'}:
        await state.features.repo.patch(memory_id, status='deleted', payload={})
    else:
        if not await state.store.delete_memory(memory_id):
            raise HTTPException(404, 'RESOURCE_NOT_FOUND')
        if state.features:
            await state.features.memory_deleted(memory_id)
    return response({'deleted': True})


class AllowlistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: list[str] = Field(max_length=1000)
    revision: int = Field(ge=0)

    @field_validator("groups")
    @classmethod
    def validate_groups(cls, value):
        Allowlist.validate(value)
        return value


@router.get("/allowlist")
async def get_allowlist(request: Request):
    return response(request.app.state.allowlist.value)


@router.put("/allowlist")
async def put_allowlist(payload: AllowlistInput, request: Request):
    state = request.app.state
    async with state.allowlist.lock:
        if payload.revision != state.allowlist.value["revision"]:
            raise HTTPException(409, "ALLOWLIST_REVISION_CONFLICT")
        old = set(state.settings.allowed_groups)
        new = set(payload.groups)
        # Provision rows without restarting workers or touching in-flight sends.
        for group in new - old:
            await state.store.ensure_group(group)
        await asyncio.to_thread(state.allowlist.save, payload.groups, payload.revision)
        state.settings.allowed_groups = list(payload.groups)
        state.dispatcher.allowed_groups = frozenset(payload.groups)
        # Disable removed groups too: background jobs also consult group policy.
        for group in old - new:
            policy = replace(await state.store.policy(Destination("group", group)), enabled=False)
            await state.store.update_policy(group, policy)
            if state.v2:
                await state.v2.call("set_policy", group, asdict(policy))
        for group in new - old:
            policy = await state.store.policy(Destination("group", group))
            if state.v2:
                await state.v2.call("set_policy", group, asdict(policy))
        logger.info("group_allowlist_updated added=%s removed=%s", sorted(new-old), sorted(old-new))
        return response(state.allowlist.value)


def bounds(day: date):
    start = datetime.combine(day, time(), ZoneInfo("Asia/Shanghai"))
    return start.timestamp(), (start+timedelta(days=1)).timestamp()


@router.get("/streams")
async def streams(request: Request, day: date):
    start, end = bounds(day)
    store = request.app.state.store
    if hasattr(store, "session"):
        from sqlalchemy import select, func
        from osa.infrastructure.schema import EventRow
        async with store.session() as session:
            rows = (await session.execute(select(EventRow.conversation, func.count(), func.max(EventRow.occurred_at))
                .where(EventRow.occurred_at >= start, EventRow.occurred_at < end)
                .group_by(EventRow.conversation).order_by(func.max(EventRow.occurred_at).desc()).limit(500))).all()
        values = [{"conversation": r[0], "count": r[1], "last_ts": r[2]} for r in rows]
    else:
        values = {}
        for row in store.events.values():
            event = row["event"]
            if start <= event.occurred_at < end:
                item = values.setdefault(event.destination.key, {"conversation": event.destination.key,"count":0,"last_ts":0})
                item["count"] += 1
                item["last_ts"] = max(item["last_ts"],event.occurred_at)
        values = sorted(values.values(), key=lambda r:r["last_ts"], reverse=True)[:500]
    names = await request.app.state.platform.group_names()
    for item in values:
        item['name'] = names.get(item['conversation'].split(':',1)[1], '') if item['conversation'].startswith('group:') else ''
    return response(values)


@router.get("/streams/events")
async def stream_events(request: Request, day: date, conversation: Annotated[str, Query(pattern=r"^(group|private):[0-9]{1,20}$")],
                        before: Annotated[str | None,Query(max_length=100)] = None, limit: Annotated[int, Query(ge=1,le=100)] = 40):
    start, end = bounds(day)
    boundary = None
    if before is not None:
        try:
            timestamp, event_id = before.split(":", 1)
            boundary = (float(timestamp), event_id)
        except (ValueError, TypeError):
            raise HTTPException(422, "INVALID_CURSOR") from None
    store = request.app.state.store
    if hasattr(store, "session"):
        from sqlalchemy import select, or_, and_
        from osa.infrastructure.schema import EventRow, DecisionRow, OutboxRow
        from osa.infrastructure.store import row_dict
        async with store.session() as session:
            query = select(EventRow).where(EventRow.conversation==conversation,
                EventRow.occurred_at>=start,EventRow.occurred_at<end)
            if boundary:
                query = query.where(or_(EventRow.occurred_at < boundary[0],
                    and_(EventRow.occurred_at == boundary[0],EventRow.id < boundary[1])))
            raw = (await session.scalars(query.order_by(EventRow.occurred_at.desc(),EventRow.id.desc()).limit(limit+1))).all()
            ids = [r.id for r in raw[:limit]]
            decision_query = select(DecisionRow.event_id, DecisionRow.action, DecisionRow.reason,
                DecisionRow.created_at, DecisionRow.snapshot['anchor_id'].as_string().label('anchor_id'))
            decisions = {r['event_id']:dict(r) for r in (await session.execute(
                decision_query.where(DecisionRow.event_id.in_(ids)))).mappings()}
            reply_rows = [row_dict(r) for r in (await session.scalars(select(OutboxRow).where(OutboxRow.event_id.in_(ids)).order_by(OutboxRow.created_at))).all()]
            rows = [row_dict(r) for r in raw]
    else:
        rows = [{**r,"payload":r["event"].to_dict(),"id":r["event"].id,"occurred_at":r["event"].occurred_at}
                for r in store.events.values() if r["event"].destination.key==conversation and start<=r["event"].occurred_at<end]
        rows.sort(key=lambda r:(r["occurred_at"],r["id"]),reverse=True)
        if boundary:
            rows = [r for r in rows if (r["occurred_at"],r["id"]) < boundary]
        rows = rows[:limit+1]
        decisions, reply_rows = store.decisions, sorted(store.outboxes.values(), key=lambda r:r['created_at'])
    replies = {}
    for reply in reply_rows:
        replies.setdefault(reply['event_id'], []).append(reply)
    journal = getattr(request.app.state, 'flow_journal', None)
    flow = await asyncio.to_thread(journal.query, [r['id'] for r in rows[:limit] if r['collect'] and not r['recalled']]) if journal else {}
    result = []
    for row in rows[:limit]:
        payload = row["payload"]
        hidden = row["recalled"] or not row["collect"]
        decision = decisions.get(row["id"])
        row_replies = replies.get(row['id'], [])
        reply = row_replies[-1] if row_replies else None
        item = {"id":row["id"],"ts":row["occurred_at"],"actor":payload["actor_id"],
                'actor_name':payload.get('actor_name','') if not hidden else '',
                "type":payload["event_type"],"status":row["status"],"error":row.get("error"),
                "text":"[消息已撤回]" if row["recalled"] else "[群策略未开启消息留存]" if hidden else payload.get("text",""),
                "decision": {k:decision.get(k) for k in ["action","reason","created_at"]} if decision else None,
                "reply": {k:reply.get(k) for k in ["status","provider","model","input_tokens","output_tokens","created_at","sent_at","error"]} if reply else None}
        if item["reply"] is not None:
            item["reply"]["text"] = "[内容不保留]" if hidden else reply.get("text","")
        if item["decision"] is not None:
            item["decision"]["reason_label"] = VALUES.get(decision.get("reason"), decision.get("reason"))
        snapshot = (decision or {}).get('snapshot') or {}
        item['batch_anchor_id'] = decision.get('anchor_id', snapshot.get('anchor_id')) if decision and isinstance(snapshot, dict) else None
        item['flow'] = flow.get(row['id'], []) if not hidden else []
        for entry in item['flow']:
            entry['reason_label'] = VALUES.get(entry.get('reason'), entry.get('reason'))
        item['replies'] = [{**{k:r.get(k) for k in ['id','status','provider','model','created_at','sent_at','error']},
                            'text':'[内容不保留]' if hidden else r.get('text','')} for r in row_replies]
        result.append(item)
    return JSONResponse(response({"items":result,"next":str(rows[limit-1]["occurred_at"])+":"+rows[limit-1]["id"] if len(rows)>limit else None}), headers={'Cache-Control':'no-store'})


@router.get("/logs")
async def logs(request: Request, level: Annotated[str, Query(pattern=r"^(|INFO|WARNING|ERROR|CRITICAL)$")] = "",
               source: Annotated[str,Query(max_length=100)]="", q: Annotated[str,Query(max_length=200)]="",
               before: float | None=None, limit: Annotated[int,Query(ge=1,le=200)]=100):
    return response(await asyncio.to_thread(request.app.state.console_logs.query,level,source,q,before,limit))


@router.get('/logs/model-calls/{call_id}')
async def model_call_detail(request: Request,call_id: str):
    import re
    if not re.fullmatch(r'[a-f0-9]{32}',call_id):raise HTTPException(422,'INVALID_CALL_ID')
    value=await asyncio.to_thread(request.app.state.console_logs.model_detail,call_id)
    if value is None:raise HTTPException(404,'MODEL_CALL_DETAIL_EXPIRED')
    if value.get('purpose') == 'tool' and value.get('event_id') and not await retained_sources(request.app.state,[value['event_id']]):
        raise HTTPException(404,'MODEL_CALL_DETAIL_EXPIRED')
    return JSONResponse(response(value),headers={'Cache-Control':'no-store'})
