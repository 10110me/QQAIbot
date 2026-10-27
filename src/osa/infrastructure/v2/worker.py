"""Work process entry and model/provider composition."""
import asyncio
import json
import logging
import os
import sqlite3
import httpx
from osa.application.prompt_catalog import render_prompt
from osa.application.v2.worker import WorkAgent
from osa.infrastructure.v2.registry import Registry
from osa.infrastructure.features.executor import SandboxClient
from osa.domain.work import WorkModelUnavailable

logger = logging.getLogger(__name__)


async def worker_cycle(registry, agent, coding_adapter, work_enabled):
    try:
        if coding_adapter:
            await coding_adapter.maintenance()
        await asyncio.to_thread(registry.reap)
        if work_enabled:
            task = await asyncio.to_thread(registry.claim, 'work-'+str(os.getpid()))
            if task:
                await agent.run(task)
    except (sqlite3.OperationalError, httpx.HTTPError, OSError) as exc:
        logger.warning('work_cycle_retry error_type=%s', type(exc).__name__)

async def planner(task):
    gateway=os.environ.get('OSA_WORK_MODEL_GATEWAY_URL','').rstrip('/')
    if gateway:
        async with httpx.AsyncClient(timeout=600) as client:
            result=await client.post(gateway+'/internal/work-model',
                headers={'Authorization':'Bearer '+os.environ.get('OSA_WORKER_TOKEN','')},
                json={'task_id':task['id'],'generation':task['generation']})
            # An empty web model selection must not silently revive deleted env models.
            if result.status_code >= 400:
                try:code=result.json().get('error',{}).get('code','')
                except ValueError:code=''
                labels={'WORK_MODEL_EMPTY_RESPONSE':'工作模型多次未返回可用正文',
                        'WORK_MODEL_REASONING_WITHOUT_ANSWER':'工作模型只返回推理数据，没有可执行动作',
                        'WORK_MODEL_OUTPUT_TRUNCATED':'工作模型输出被截断，请增加输出上限或选择其他模型',
                        'WORK_MODEL_RATE_LIMITED':'工作模型服务商限流（HTTP 429）',
                        'WORK_MODEL_TIMEOUT':'工作模型请求超时',
                        'WORK_MODEL_PROTOCOL_ERROR':'工作模型未生成有效项目动作，格式纠正后仍未成功',
                        'WORK_MODEL_STREAM_ERROR':'工作模型的流式响应返回服务商错误，重试后仍失败',
                        'WORK_MODEL_NOT_CONFIGURED':'尚未配置 AI 工作模型',
                        'WORK_MODEL_UPSTREAM_UNAVAILABLE':'工作模型请求重试后仍失败，请检查模型服务商'}
                raise WorkModelUnavailable(labels.get(code,'工作模型接口暂时不可用（HTTP '+str(result.status_code)+'）'))
            text=result.json()['data']['text']
            if len(text)>700000:raise ValueError('Model response too large')
            return json.loads(text.removeprefix('```json').removesuffix('```').strip())['files']
    key=os.environ.get('OSA_WORK_MODEL_API_KEY','')
    if not key:raise ValueError('Work model API key is not configured')
    prompt={'requirement':task['requirement'],'changes':task['requirement_log'],'existing_files':task.get('files',{}),'last_error':task.get('blocker')}
    async with httpx.AsyncClient(timeout=90) as client:
        result=await client.post(os.environ.get('OSA_WORK_MODEL_BASE_URL','https://api.openai.com/v1').rstrip('/')+'/chat/completions',
            headers={'Authorization':'Bearer '+key},json={'model':os.environ.get('OSA_WORK_MODEL','configured-work-model'),
            'messages':[{'role':'system','content':render_prompt(None,'work')},
                        {'role':'user','content':json.dumps(prompt,ensure_ascii=False)}], 'max_tokens':int(os.environ.get('OSA_WORK_MAX_TOKENS','8192')),'temperature':0.2})
        result.raise_for_status();text=result.json()['choices'][0]['message']['content']
        if len(text)>700000:raise ValueError('Model response too large')
        return json.loads(text.removeprefix('```json').removesuffix('```').strip())['files']


async def main():
    registry_type=Registry
    if os.environ.get('OSA_V3_ENABLED','false').lower()=='true':
        from osa.infrastructure.v3.registry import Registry as V3Registry
        registry_type=V3Registry
    registry=registry_type(os.environ.get('OSA_V2_DATABASE_URL','/app/control/registry.db'))
    await asyncio.to_thread(registry.reconcile_legacy_failures)
    executor=SandboxClient(os.environ.get('OSA_SANDBOX_WORKER_URL','http://sandbox-worker:8091'),os.environ['OSA_WORKER_TOKEN'])
    plan=planner
    coding_adapter=None
    provider=os.environ.get('OSA_CODING_PROVIDER','builtin')
    if provider in {'opencode','codex'}:
        prefix='OSA_CODEX' if provider=='codex' else 'OSA_OPENCODE'
        endpoint=os.environ.get(prefix+'_ENDPOINT','')
        if os.environ.get('OSA_V3_ENABLED','false').lower()!='true' or '{workspace_id}' not in endpoint:
            raise ValueError('Coding provider requires the V3 registry and a workspace-scoped endpoint')
        managed=os.environ.get(prefix+'_MANAGED_GATEWAY','true').lower()=='true'
        if not managed and os.environ.get(prefix+'_ISOLATION_CONFIRMED','false').lower()!='true':
            raise ValueError('Coding provider requires a separately isolated per-workspace server')
        version='0.147.0' if provider=='codex' else '1.2.27'
        if provider=='codex':
            from osa.infrastructure.v3.codex import CodexAdapter as adapter_type
        else:
            from osa.infrastructure.v3.opencode import OpenCodeAdapter as adapter_type
        coding_adapter=adapter_type(registry,endpoint,os.environ[prefix+'_PASSWORD'],
            os.environ.get(prefix+'_VERSION',version),os.environ.get(prefix+'_USERNAME',provider),managed_gateway=managed)
        plan=coding_adapter.plan
    agent=WorkAgent(registry,plan,executor,os.environ.get('OSA_WORKSPACE_DIR','/app/workspaces'))
    from osa.infrastructure.v2.research_processor import research_loop
    research=asyncio.create_task(research_loop(registry))
    try:
        while True:
            await worker_cycle(registry,agent,coding_adapter,os.environ.get('OSA_WORK_ENABLED','false').lower()=='true')
            await asyncio.sleep(1)
    finally:
        research.cancel();await asyncio.gather(research,return_exceptions=True)


if __name__=='__main__':asyncio.run(main())
