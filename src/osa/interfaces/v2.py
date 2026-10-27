"""V2 control plane. Console owner only; QQ commands enforce task owner + scope."""
import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Header, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from osa.domain.entities import Destination
from osa.domain.work import VersionConflict
from osa.interfaces.api import require_admin, response

router=APIRouter(prefix='/api/v1',dependencies=[Depends(require_admin)])
echo_router=APIRouter(prefix='/api/v1')


def service(request):
    value=request.app.state.v2
    if value is None:raise HTTPException(409,'V2_DISABLED')
    return value


class CreateWork(BaseModel):
    model_config=ConfigDict(extra='forbid')
    requirement:str=Field(min_length=1,max_length=8000)
    owner_id:str=Field(default='console-owner',max_length=100)
    group_id:str|None=None
    idempotency_key:str=Field(min_length=8,max_length=128)


class WorkCommand(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['APPROVE','REJECT','ADD_REQUIREMENT','PAUSE','RESUME','CANCEL']
    expected_version:int=Field(ge=1)
    idempotency_key:str=Field(min_length=8,max_length=128)
    payload:dict=Field(default_factory=dict)


async def task(request,tid):
    try:return await service(request).call('get',tid)
    except KeyError:raise HTTPException(404,'WORK_TASK_NOT_FOUND')


@router.get('/agent/presence')
async def presence(request:Request):
    return response(await service(request).call('presence'))


@router.post('/work/tasks')
async def create_work(body:CreateWork,request:Request):
    v2=service(request)
    if not v2.settings.work_enabled:raise HTTPException(409,'WORK_RUNTIME_DISABLED')
    if body.group_id:
        if body.group_id not in request.app.state.settings.allowed_groups:raise HTTPException(403,'GROUP_NOT_ALLOWED')
        if not (await request.app.state.store.policy(Destination('group',body.group_id))).sandbox:raise HTTPException(403,'GROUP_WORK_DENIED')
    row=await v2.call('create',body.owner_id,body.group_id,body.requirement,body.idempotency_key)
    return response(v2.registry.public(row))


@router.get('/work/tasks')
async def work_tasks(request:Request):
    rows=await service(request).call('tasks',admin=True)
    return response([{**service(request).registry.public(r),'requirement':r['requirement']} for r in rows])


@router.get('/work/tasks/{tid}')
async def work_task(tid:str,request:Request):
    row=await task(request,tid)
    return response({**service(request).registry.public(row),'requirement':row['requirement'],'requirement_log':row['requirement_log']})


@router.delete('/work/tasks/{tid}')
async def delete_work_task(tid:str,request:Request,expected_version:int|None=Query(default=None,ge=1)):
    try:
        return response(await service(request).call('archive_task',tid,expected_version))
    except KeyError:
        raise HTTPException(404,'WORK_TASK_NOT_FOUND')
    except VersionConflict:
        raise HTTPException(409,'任务已更新，请刷新后重试')
    except ValueError as exc:
        raise HTTPException(409,str(exc))


@router.post('/work/tasks/{tid}/commands')
async def work_command(tid:str,body:WorkCommand,request:Request):
    row=await task(request,tid)
    if body.kind in {'APPROVE','RESUME'}:
        if not service(request).settings.work_enabled:raise HTTPException(409,'WORK_RUNTIME_DISABLED')
        if row['group_id'] and not (await request.app.state.store.policy(Destination('group',row['group_id']))).sandbox:
            raise HTTPException(403,'GROUP_WORK_DENIED')
    try:
        value=await service(request).call('command',tid,request.state.principal['name'],None,body.kind,body.payload,
            body.expected_version,body.idempotency_key,admin=True)
        return response(value)
    except VersionConflict:raise HTTPException(409,'TASK_VERSION_CONFLICT')
    except (ValueError,PermissionError):raise HTTPException(400,'INVALID_TASK_COMMAND')


@router.get('/work/tasks/{tid}/events')
async def work_events(tid:str,request:Request):
    await task(request,tid)
    try:after=max(0,int(request.headers.get('last-event-id','0')))
    except ValueError:raise HTTPException(400,'INVALID_EVENT_CURSOR')
    async def stream():
        nonlocal after
        while not await request.is_disconnected():
            rows=await service(request).call('events',tid,after)
            for row in rows:
                after=row['seq']
                yield f"id: {after}\nevent: progress\ndata: {json.dumps(row,ensure_ascii=False)}\n\n"
            yield ': heartbeat\n\n'
            await asyncio.sleep(2)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})


@router.get('/work/tasks/{tid}/evidence')
async def work_evidence(tid:str,request:Request):
    await task(request,tid)
    return response(await service(request).call('events',tid,0))


@router.get('/work/tasks/{tid}/artifacts')
async def work_artifacts(tid:str,request:Request):
    row=await task(request,tid)
    return response([{'id':row['artifact_id'],'download_url':f'/api/v1/work/tasks/{tid}/download'}] if row.get('artifact_id') else [])


@router.get('/work/tasks/{tid}/download')
async def download_work(tid:str,request:Request):
    row=await task(request,tid)
    if row['status']!='COMPLETED':raise HTTPException(409,'ARTIFACT_NOT_FINAL')
    if not row.get('artifact_id'):raise HTTPException(404,'ARTIFACT_NOT_PUBLISHED')
    generation,digest=row['artifact_id'].split(':')
    path=Path(service(request).settings.workspace_dir)/row['id']/f'generation-{int(generation)}'/'project.zip'
    if not path.is_file() or await asyncio.to_thread(lambda:hashlib.sha256(path.read_bytes()).hexdigest())!=digest:
        raise HTTPException(409,'ARTIFACT_INTEGRITY_FAILED')
    return FileResponse(path,filename=f'project-{tid}.zip',media_type='application/zip')


@router.get('/experiences')
async def experiences(request:Request,q:str='',group_id:str|None=None):
    rows=await service(request).call('records','experience',admin=True)
    return response([r for r in rows if (not group_id or r['group_id']==group_id) and q.lower() in json.dumps(r['payload'],ensure_ascii=False).lower()])


class Review(BaseModel):
    model_config=ConfigDict(extra='forbid')
    state:Literal['share','disputed','outdated','revoked','proposed']
    note:str=Field(min_length=1,max_length=2000)
    shareable_text:str|None=Field(default=None,max_length=3000)
    public_question:str|None=Field(default=None,max_length=3000)
    solution:str|None=Field(default=None,max_length=3000)


@router.post('/experiences/{rid}/reviews')
async def review_experience(rid:str,body:Review,request:Request):
    try:await service(request).call('review',rid,request.state.principal['name'],body.state,body.note,body.shareable_text,body.public_question,body.solution)
    except KeyError:raise HTTPException(404,'EXPERIENCE_NOT_FOUND')
    except (PermissionError,ValueError):raise HTTPException(400,'REVIEW_REQUIRES_EVIDENCE_OR_REDACTION')
    return response({'reviewed':True})


@router.get('/research/jobs')
async def research_jobs(request:Request):
    return response(await service(request).call('records','night_research',admin=True))


@router.post('/research/jobs/{rid}/retry')
async def retry_research(rid:str,request:Request):
    rows=await service(request).call('records','night_research',admin=True)
    row=next((r for r in rows if r['id']==rid),None)
    if not row:raise HTTPException(404,'RESEARCH_NOT_FOUND')
    if row['state']!='DEFERRED':raise HTTPException(409,'RESEARCH_NOT_RETRYABLE')
    await service(request).call('update_record',rid,'QUEUED',{'manual_retry_at':time.time()})
    return response({'queued':True,'remote_id_preserved':True})


@router.get('/consolidations')
async def consolidations(request:Request):
    return response(await service(request).call('records','consolidation_audit',admin=True))


class Echo(BaseModel):
    model_config=ConfigDict(extra='forbid')
    group_id:str
    event_id:str=Field(min_length=1,max_length=120)
    detected_at:float
    ttl_seconds:int=Field(default=20,ge=0,le=40)


@echo_router.post('/groups/echo-state')
async def echo_state(body:Echo,request:Request,authorization:str=Header(default='')):
    token=request.app.state.settings.echo_token.get_secret_value()
    if not (len(token)>=24 and secrets.compare_digest(authorization,'Bearer '+token)):
        await require_admin(request,authorization)
    if body.group_id not in request.app.state.settings.allowed_groups:raise HTTPException(403,'GROUP_NOT_ALLOWED')
    try:await service(request).call('echo',body.group_id,body.event_id,body.detected_at,body.ttl_seconds)
    except ValueError:raise HTTPException(400,'STALE_ECHO_EVENT')
    return response({'accepted':True,'read_only':True})


@router.post('/admin/kill-switch')
async def kill_switch(request:Request):
    v2=service(request)
    await v2.call('set_kill',True)
    await request.app.state.store.set_paused(True)
    requested=0
    for row in await v2.call('tasks',admin=True):
        if row['status'] not in {'COMPLETED','FAILED','CANCELLED','DENIED'}:
            try:
                await v2.call('command',row['id'],'console-owner',None,'CANCEL',{},row['version'],'kill:'+row['id']+':'+str(row['version']),admin=True)
                requested+=1
            except ValueError:pass
    return response({'work_blocked':True,'social_paused':True,'cancellation_requests':requested})


@router.delete('/admin/kill-switch')
async def clear_kill(request:Request):
    await service(request).call('set_kill',False)
    return response({'work_blocked':False,'social':'use the existing resume control explicitly'})


@router.get('/work/tasks/{tid}/changes')
async def work_changes(tid:str,request:Request):
    row=await task(request,tid)
    if not row.get('git_commit'):return response({'diff':None,'reason':'Git checkpoint not available'})
    generation=int(row['artifact_id'].split(':')[0]) if row.get('artifact_id') else row['generation']
    repo=Path(service(request).settings.workspace_dir)/row['id']/f"generation-{generation}"/'repo'
    process=await asyncio.create_subprocess_exec('git','-C',str(repo),'show','--no-ext-diff','--format=medium',row['git_commit'],
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        raw,_=await asyncio.wait_for(process.communicate(),10)
    except (TimeoutError,asyncio.CancelledError):
        process.kill();await process.wait();raise
    if process.returncode:return response({'diff':None,'reason':'Git checkpoint is not readable in this generation'})
    from osa.domain.work import clean_text
    return response({'diff':clean_text(raw.decode(errors='replace'),64000),'git_commit':row['git_commit']})


class ClaimReview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    quote:str=Field(min_length=20,max_length=2000)
    note:str=Field(min_length=1,max_length=1000)
    publish:bool=False


@router.post('/research/jobs/{rid}/claims/{index}/review')
async def review_claim(rid:str,index:int,body:ClaimReview,request:Request):
    try:
        result=await service(request).call('review_research',rid,index,request.state.principal['name'],body.source_sha256,body.quote,body.note,body.publish)
        return response(result)
    except KeyError:raise HTTPException(404,'CLAIM_NOT_FOUND_OR_REVOKED')
    except (ValueError,PermissionError):raise HTTPException(400,'QUOTE_OR_PUBLIC_DISCLOSURE_INVALID')
