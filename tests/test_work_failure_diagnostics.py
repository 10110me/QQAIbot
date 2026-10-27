import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from osa.application.v2.worker import WorkAgent
from osa.application.v3.workspace import WorkspaceAgent, parse_work_action
from osa.domain.entities import ModelResult, UpstreamUnavailable
from osa.domain.work import WorkModelUnavailable
from osa.infrastructure.configured_models import ConfiguredModelProvider, ModelResponseError
from osa.infrastructure.runtime_config import RuntimeConfiguration, RuntimeConfigurationStore, ProviderConfig, ModelConfig, TaskConfig
from osa.infrastructure.v3.registry import Registry
from osa.interfaces.work_models import work_failure_code


class WorkDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_stream_uses_task_total_deadline_not_network_idle_interval(self):
        config=RuntimeConfiguration(providers=[ProviderConfig(id='p',name='Provider',base_url='https://example.com',
            api_key='synthetic-test-key',timeout=.02,max_retry=0)],
            models=[ModelConfig(model_identifier='m',name='M',api_provider='p',force_stream_mode=True)],
            task_configs={'work':TaskConfig(model_list=['m'],hard_timeout=1)})
        provider=ConfiguredModelProvider(RuntimeConfigurationStore('unused',config,memory_only=True))
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in ['{','"files"',':','{}','}']:
                    await asyncio.sleep(.01)
                    yield ('data: '+json.dumps({'choices':[{'delta':{'content':chunk}}]})+'\n\n').encode()
                yield b'data: [DONE]\n\n'
        async def handler(request):return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream())
        await provider.client.aclose();provider.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result=await provider.generate([{'role':'user','content':'synthetic test program'}],'work')
            self.assertEqual(json.loads(result.text),{'files':{}})
        finally:await provider.close()

    def test_only_unambiguous_missing_container_closures_are_recovered(self):
        original={'tool_call':{'name':'file.write','arguments':{'path':'snake.py',
            'content':'# 中文源码\nprint("hello\\nworld")\n'}}}
        encoded=json.dumps(original,ensure_ascii=False)
        self.assertEqual(parse_work_action(encoded[:-1]),original)
        self.assertEqual(parse_work_action(encoded[:-2]),original)
        self.assertEqual(parse_work_action('```json\n'+encoded+'\n```'),original)
        for broken in ['{"tool_call":{"name":"file.write","arguments":{"content":"unfinished',
                       '{"tool_call":', '{"files":{"main.py":"print(1)",', '{"files":[}']:
            with self.subTest(broken=broken),self.assertRaises(ValueError):parse_work_action(broken)

    async def provider(self, response):
        config=RuntimeConfiguration(providers=[ProviderConfig(id='p',name='p',base_url='https://test.invalid',max_retry=0)],
            models=[ModelConfig(model_identifier='m',name='m',api_provider='p',force_stream_mode=True)],
            task_configs={'work':TaskConfig(model_list=['m'])})
        provider=ConfiguredModelProvider(RuntimeConfigurationStore('.',config,memory_only=True))
        await provider.client.aclose()
        provider.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:response))
        return provider

    async def test_usage_only_chunk_and_json_fallback_are_supported(self):
        for response in [httpx.Response(200,headers={'content-type':'text/event-stream'},text=
            'data: '+json.dumps({'choices':[{'delta':{'content':'{"files":{}}'}}]})+'\n\n'+
            'data: '+json.dumps({'choices':[],'usage':{'completion_tokens':3}})+'\n\ndata: [DONE]\n\n'),
            httpx.Response(200,json={'choices':[{'message':{'content':'{"files":{}}'}}]})]:
            provider=await self.provider(response)
            try:self.assertEqual((await provider.generate([],'work')).text,'{"files":{}}')
            finally:await provider.close()

    async def test_work_fallback_does_not_retry_broken_model_at_every_step(self):
        config=RuntimeConfiguration(providers=[ProviderConfig(id='p',name='p',base_url='https://test.invalid',max_retry=0)],
            models=[ModelConfig(model_identifier=name,name=name,api_provider='p') for name in ['broken','working']],
            task_configs={'work':TaskConfig(model_list=['broken','working'])})
        provider=ConfiguredModelProvider(RuntimeConfigurationStore('.',config,memory_only=True))
        await provider.client.aclose();calls=[]
        def handle(request):
            model=json.loads(request.content)['model'];calls.append(model)
            return httpx.Response(503) if model=='broken' else httpx.Response(200,json={'choices':[{'message':{'content':'valid'}}]})
        provider.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            self.assertEqual((await provider.generate([],'work')).model,'working')
            self.assertEqual((await provider.generate([],'work')).model,'working')
            self.assertEqual(calls,['broken','working','working'])
            provider.cooldowns={key:0 for key in provider.cooldowns}
            await provider.generate([],'work')
            self.assertEqual(calls[-2:],['broken','working'])
            # Saving new configuration immediately retries the configured first model.
            provider.store.replace(provider.store.snapshot())
            await provider.generate([],'work')
            self.assertEqual(calls[-2:],['broken','working'])
        finally:await provider.close()

    async def test_empty_reasoning_only_truncated_and_stream_errors_are_distinct(self):
        for data,code in [({'choices':[]},'MODEL_EMPTY_RESPONSE'),
            ({'choices':[{'delta':{'reasoning_content':'hidden private reasoning'}}]},'MODEL_REASONING_WITHOUT_ANSWER'),
            ({'choices':[{'delta':{'content':'{"partial'},'finish_reason':'length'}]},'MODEL_OUTPUT_TRUNCATED'),
            ({'error':{'message':'private upstream detail'}},'MODEL_STREAM_ERROR')]:
            provider=await self.provider(httpx.Response(200,headers={'content-type':'text/event-stream'},
                text='data: '+json.dumps(data)+'\n\ndata: [DONE]\n\n'))
            try:
                with self.assertRaises(UpstreamUnavailable) as caught:await provider.generate([],'work')
                self.assertIsInstance(caught.exception.__cause__,ModelResponseError)
                cause=caught.exception.__cause__
                self.assertEqual(cause.code,code)
                self.assertNotIn('hidden private',str(cause.diagnostics))
                self.assertNotIn('private upstream',str(cause))
                self.assertEqual(work_failure_code(caught.exception),'WORK_'+code)
            finally:await provider.close()

    async def test_hard_timeout_bounds_the_whole_response_and_is_classified(self):
        provider=await self.provider(httpx.Response(200))
        async def slow(*args,**kwargs):
            await asyncio.sleep(1)
            return 'late',{}
        provider._stream=slow
        task=provider.store.snapshot().task_configs['work'].model_copy(update={'hard_timeout':.01})
        try:
            with self.assertRaises(TimeoutError) as caught:
                await provider._request(provider.store.snapshot().providers[0],provider.store.snapshot().models[0],task,[],'work')
            self.assertEqual(work_failure_code(caught.exception),'WORK_MODEL_TIMEOUT')
        finally:await provider.close()

    async def test_infrastructure_failure_is_failed_not_waiting_for_user(self):
        with tempfile.TemporaryDirectory() as folder:
            registry=Registry(str(Path(folder)/'registry.db'))
            task=registry.create('owner','group','synthetic program','test')
            registry.command(task['id'],'admin',None,'APPROVE',{},1,'approve',admin=True)
            running=registry.claim('worker')
            executor=SimpleNamespace(run=AsyncMock(),cancel=AsyncMock())
            await WorkAgent(registry,AsyncMock(side_effect=WorkModelUnavailable('工作模型服务商限流（HTTP 429）')),executor,folder).run(running)
            final=registry.get(task['id'])
            self.assertEqual(final['status'],'FAILED')
            self.assertIn('限流',final['blocker'])
            self.assertIsNone(final.get('artifact_id'))
            executor.run.assert_not_awaited()
            self.assertIn('模型服务失败',final['current_step'])
            registry.command(task['id'],'admin',None,'RESUME',{},final['version'],'retry-infrastructure',admin=True)
            retried=registry.claim('worker')
            self.assertEqual(retried['generation'],running['generation']+1)
            self.assertEqual(retried['status'],'RUNNING')
            self.assertNotIn('failure_kind',retried)

    async def test_failed_validation_and_cancelled_tasks_cannot_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            registry=Registry(str(Path(folder)/'registry.db'))
            task=registry.create('owner',None,'synthetic','failed-test')
            registry.command(task['id'],'admin',None,'APPROVE',{},1,'a',admin=True)
            running=registry.claim('worker')
            registry.finish(task['id'],running['generation'],'FAILED','测试验证失败')
            final=registry.get(task['id'])
            with self.assertRaises(ValueError):registry.command(task['id'],'admin',None,'RESUME',{},final['version'],'not-retryable',admin=True)

    async def test_workspace_repairs_plain_answer_once_without_running_it(self):
        model=SimpleNamespace(generate=AsyncMock(side_effect=[ModelResult('我直接给你代码','test','m'),
            ModelResult('{"files":{"main.py":"print(1)"}}','test','m')]))
        files,_=await WorkspaceAgent(model).run({'existing_files':{}})
        self.assertEqual(files,{'main.py':'print(1)'})
        self.assertEqual(model.generate.await_count,2)
        broken=SimpleNamespace(generate=AsyncMock(return_value=ModelResult('ordinary text','test','m')))
        with self.assertRaises(ValueError):await WorkspaceAgent(broken).run({})
        self.assertEqual(broken.generate.await_count,2)
