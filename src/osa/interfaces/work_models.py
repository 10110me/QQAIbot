"""Work worker uses an authenticated, task-scoped model gateway, never owner keys."""
import json
import asyncio
import base64
import hashlib
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from osa.domain.entities import Destination, UpstreamUnavailable
from osa.interfaces.api import response
from osa.application.v3.workspace import WorkspaceAgent

router = APIRouter()


def work_failure_code(exc):
    """Preserve safe technical categories across the worker HTTP boundary."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        code = getattr(exc, 'code', None)
        if code in {'MODEL_EMPTY_RESPONSE','MODEL_REASONING_WITHOUT_ANSWER','MODEL_OUTPUT_TRUNCATED','MODEL_STREAM_ERROR'}:
            return 'WORK_' + code
        if getattr(getattr(exc, 'response', None), 'status_code', None) == 429:
            return 'WORK_MODEL_RATE_LIMITED'
        if isinstance(exc, TimeoutError) or type(exc).__name__ in {'ReadTimeout','ConnectTimeout'}:
            return 'WORK_MODEL_TIMEOUT'
        exc = exc.__cause__ or exc.__context__
    return 'WORK_MODEL_UPSTREAM_UNAVAILABLE'



class WorkModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=100)
    generation: int = Field(ge=1)


class NightPromptInput(BaseModel):
    question: str = Field(min_length=1,max_length=12000)


@router.get('/internal/opencode/model')
@router.get('/internal/codex/model')
async def opencode_model(request: Request):
    """Only the trusted gateway receives the selected dedicated coding key."""
    state = request.app.state
    key = state.settings.worker_token.get_secret_value()
    if not key or not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer '+key):
        raise HTTPException(401, 'UNAUTHORIZED')
    config = state.runtime_configuration.snapshot()
    route = config.task_configs.get('work')
    models = {m.model_identifier: m for m in config.models if m.enabled}
    providers = {p.id: p for p in config.providers if p.enabled}
    for name in route.model_list if route else []:
        model = models.get(name)
        provider = providers.get(model.api_provider) if model else None
        if provider:
            if provider.auth_type != 'bearer' or provider.default_query:
                raise HTTPException(409, 'OPENCODE_PROVIDER_REQUIRES_BEARER_AUTH')
            return response({'base_url': provider.base_url, 'model': model.model_identifier,
                'api_key': provider.api_key.get_secret_value(), 'headers': provider.default_headers,
                'options': model.extra_params})
    raise HTTPException(409, 'WORK_MODEL_NOT_CONFIGURED')


@router.post('/internal/prompt-template/night_research')
@router.post('/internal/prompt-template/deepseek_consult')
async def night_prompt(body: NightPromptInput,request: Request):
    key=request.app.state.settings.worker_token.get_secret_value()
    if not key or not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+key):raise HTTPException(401,'UNAUTHORIZED')
    from osa.domain.work import external_question
    slot='deepseek_consult' if request.url.path.endswith('deepseek_consult') else 'night_research'
    question=body.question if slot=='deepseek_consult' else external_question(body.question)
    return response({'text':question})


@router.post('/internal/work-model')
async def work_model(body: WorkModelInput, request: Request):
    state = request.app.state
    key = state.settings.worker_token.get_secret_value()
    if not key or not secrets.compare_digest(request.headers.get('authorization',''), 'Bearer '+key):
        raise HTTPException(401,'UNAUTHORIZED')
    if not state.v2:
        raise HTTPException(409,'WORK_RUNTIME_DISABLED')
    try:
        task = await state.v2.call('get',body.task_id)
    except KeyError:
        raise HTTPException(404,'WORK_TASK_NOT_FOUND') from None
    if task['generation'] != body.generation or task['status'] != 'RUNNING':
        raise HTTPException(409,'WORK_TASK_NOT_RUNNING')
    group = task.get('group_id')
    if group and (group not in state.settings.allowed_groups or not (await state.store.policy(Destination('group',group))).sandbox):
        raise HTTPException(403,'WORK_GROUP_NOT_AUTHORIZED')
    config = state.runtime_configuration.snapshot()
    route = config.task_configs.get('work')
    if not route or not route.model_list:
        raise HTTPException(409,'WORK_MODEL_NOT_CONFIGURED')
    if state.work_model is None:
        raise HTTPException(409,'WORK_MODEL_PROVIDER_UNAVAILABLE')
    prompt = {'task_id': task['id'], 'generation': body.generation,
              'requirement':task['requirement'],'changes':task['requirement_log'],
              'existing_files':task.get('files',{}),'last_error':task.get('blocker')}
    if state.features and getattr(state.features,'work_inputs',None):
        prompt['input_attachments']=await state.features.work_inputs.for_task(task,body.generation)
    from osa.domain.context import model_context
    model_context.set({'actor_id':task['owner_id'],'group_id':group,'task_id':task['id'],
        'request_text':task['requirement'],'attachment_manifest':json.dumps(prompt.get('input_attachments',[]),ensure_ascii=False),
        'collect_messages':not group or (await state.store.policy(Destination('group',group))).collect_messages})
    execution_inputs = {}
    input_paths = {}
    root = Path(state.settings.workspace_dir) / task['id'] / f'generation-{body.generation}'
    for item in prompt.get('input_attachments', []):
        path = root / item['workspace_input']
        raw = await asyncio.to_thread(path.read_bytes)
        if hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise HTTPException(409, 'WORK_INPUT_INTEGRITY_FAILED')
        execution_inputs[path.name] = base64.b64encode(raw).decode('ascii')
        input_paths[item['workspace_input']] = (raw, item)

    async def read_input(path):
        if path not in input_paths:
            raise PermissionError('Only explicitly attached task inputs may be read')
        raw, item = input_paths[path]
        return {'name': item['name'], 'bytes': len(raw), 'entries': item.get('entries', []),
                'text': raw.decode('utf-8', errors='replace')[:20000] if b'\0' not in raw[:8192] else '[二进制附件，可通过 DeepSeek 图片/文件输入分析]'}

    async def before_step(step, description):
        current = await state.v2.call('get', task['id'])
        if current['generation'] != body.generation or current['status'] != 'RUNNING' or await state.store.paused():
            raise PermissionError('Work task is paused, cancelled or replaced')
        if group and not (await state.store.policy(Destination('group', group))).sandbox:
            raise PermissionError('Workspace access was revoked')
        await state.v2.call('checkpoint', task['id'], body.generation, 'workspace-tool',
            {'current_step': description}, {'tool_step': step})

    async def consult(args, step):
        if group and not (await state.store.policy(Destination('group', group))).research_external:
            raise PermissionError('DeepSeek external consultation is disabled for this group')
        question = str(args.get('question') or '').strip()
        if not question or len(question) > 12000:
            raise ValueError('Invalid DeepSeek question')
        rid = hashlib.sha256(f"work-consult:{task['id']}:{body.generation}:{step}".encode()).hexdigest()
        attachments = [{'name': item['name'], 'data_base64': base64.b64encode(raw).decode('ascii')}
                       for raw, item in input_paths.values() if len(raw) <= 5_000_000][:4]
        browser = state.features.research.browser
        pending = await browser.ask(question, rid, session_id=args.get('session_id'),
            new_conversation=bool(args.get('new_conversation')), attachments=attachments)
        for _ in range(60):
            await before_step(step, '等待 DeepSeek 网页咨询结果')
            value = await browser.status(pending['id'])
            if value['status'] == 'succeeded':
                return {'answer': value.get('answer', '')[:18000], 'session_id': value.get('session_id', rid),
                        'trust_status': 'unverified'}
            if value['status'] in {'waiting_human', 'failed', 'cancelled'}:
                return {'status': value['status'], 'note': '请在 DeepSeek 连接投屏中检查登录或验证'}
            await asyncio.sleep(2)
        return {'status': 'waiting', 'consultation_id': pending['id'], 'session_id': pending.get('session_id', rid)}

    features = state.features
    async def save_files(files):
        await before_step(0, '保存工作文件检查点')
        await state.v2.call('checkpoint', task['id'], body.generation, 'workspace-files',
            {'files': files}, {'type': 'FILES_SAVED', 'count': len(files)})

    async def save_artifacts(artifacts):
        await before_step(0, '保存程序输出文件')
        target = root / 'artifacts'
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
        output, total = [], 0
        for item in artifacts[:10]:
            name = str(item.get('name', ''))
            if not name or name.startswith('.') or any(c in name for c in '/\\:') or len(name) > 200:
                raise PermissionError('Invalid program artifact name')
            if name == 'test-report.json':
                continue
            raw = base64.b64decode(item['content'], validate=True)
            total += len(raw)
            if len(raw) > 5_000_000 or total > 10_000_000:
                raise ValueError('Program artifacts exceed limit')
            digest = hashlib.sha256(raw).hexdigest()
            if item.get('sha256') and item['sha256'] != digest:
                raise PermissionError('Program artifact integrity mismatch')
            await asyncio.to_thread((target / name).write_bytes, raw)
            output.append({'name': name, 'bytes': len(raw), 'sha256': digest, 'status': 'stored_for_delivery'})
        return output
    agent = WorkspaceAgent(state.work_model, executor=features.sandbox if features and state.settings.work_enabled else None,
        downloader=features.downloader if features else None,
        browser=features.screenshots if features and state.settings.work_enabled else None,
        consultant=consult if features and state.settings.deepseek_web_enabled else None, before_step=before_step,
        save_files=save_files, artifact_sink=save_artifacts)
    try:
        async with asyncio.timeout(480):
            files, result = await agent.run(prompt, read_input, execution_inputs)
    except (UpstreamUnavailable, TimeoutError) as exc:
        raise HTTPException(503, work_failure_code(exc)) from None
    except ValueError:
        raise HTTPException(502, 'WORK_MODEL_PROTOCOL_ERROR') from None
    execution_result = agent.last_execution_result
    output_artifacts = list((execution_result.get('last') or {}).get('artifacts') or [])
    if execution_result:
        await state.v2.call('checkpoint', task['id'], body.generation, 'execution-result',
            {'execution_result': execution_result, 'output_artifacts': output_artifacts},
            {'type': 'EXECUTION_RESULT', 'artifact_count': len(output_artifacts)})
    text = json.dumps({'files': files}, ensure_ascii=False)
    if len(text)>700000:
        raise HTTPException(502,'WORK_MODEL_RESPONSE_TOO_LARGE')
    return response({'text':text,'provider':result.provider,'model':result.model,
                     'execution_result': execution_result, 'output_artifacts': output_artifacts})
