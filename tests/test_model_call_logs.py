import json
import logging
import tempfile
import unittest
from pathlib import Path

import httpx

from osa.domain.context import model_context
from osa.infrastructure.configured_models import ConfiguredModelProvider
from osa.infrastructure.flow_journal import FlowJournal
from osa.infrastructure.operations import ConsoleLogs
from osa.infrastructure.runtime_config import RuntimeConfigurationStore,RuntimeConfiguration,ProviderConfig,ModelConfig,TaskConfig
from osa.main import create_app
from test_core import demo_settings


class ModelCallLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_receives_actual_request_model_without_credentials(self):
        config = RuntimeConfiguration(
            providers=[ProviderConfig(id='p', name='provider', base_url='https://example.com',
                                      api_key='private-provider-secret', max_retry=0)],
            models=[ModelConfig(model_identifier='actual-model', name='Display', api_provider='p')],
            task_configs={'planner': TaskConfig(model_list=['actual-model'])})
        provider = ConfiguredModelProvider(RuntimeConfigurationStore('unused', config, memory_only=True))
        payloads = []

        async def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [
                {'message': {'content': '{"final_action":"SILENT"}'}}]})

        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        original = [{'role': 'user', 'content': '后端是什么模型'}]
        try:
            await provider.generate(original, 'thinking')
            metadata = [row['content'] for row in payloads[0]['messages']
                        if row['role'] == 'system' and '已核实的请求元数据' in row['content']]
            self.assertEqual(sum(row['role'] == 'system' for row in payloads[0]['messages']), 1)
            self.assertEqual(len(metadata), 1)
            self.assertIn('"requested_model_id": "actual-model"', metadata[0])
            self.assertIn('可公开的运行事实', metadata[0])
            self.assertEqual(payloads[0]['model'], 'actual-model')
            self.assertNotIn('private-provider-secret', metadata[0])
            self.assertNotIn('https://example.com', metadata[0])
            self.assertNotIn('Display', metadata[0])
            self.assertEqual(original, [{'role': 'user', 'content': '后端是什么模型'}])
        finally:
            await provider.close()

    async def test_retry_count_and_live_flow_stage_follow_successful_retry(self):
        config = RuntimeConfiguration(
            providers=[ProviderConfig(id='p', name='provider', base_url='https://example.com',
                                      api_key='key', max_retry=1, retry_interval=0)],
            models=[ModelConfig(model_identifier='model', name='model', api_provider='p')],
            task_configs={'planner': TaskConfig(model_list=['model'])},
        )
        provider = ConfiguredModelProvider(RuntimeConfigurationStore('unused', config, memory_only=True))
        journal = FlowJournal('.', memory_only=True)
        provider.flow = journal
        log = ConsoleLogs(Path('.'), lambda: [], memory_only=True)
        logger = logging.getLogger('osa')
        level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(log)
        attempts = 0

        async def handler(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(503) if attempts == 1 else httpx.Response(
                200, json={'choices': [{'message': {'content': '{"final_action":"SILENT"}'}}]})

        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        token = model_context.set({'event_id': 'retry-event', 'collect_messages': True,
                                   'thinking_step': 1, 'thinking_session_id': 'session'})
        try:
            await provider.generate([{'role': 'user', 'content': 'hello'}], 'thinking')
            self.assertEqual(attempts, 2)
            calls = [log.model_detail(row['detail_id']) for row in log.query()['items'] if row.get('detail_id')]
            completed = next(call for call in calls if call['status'] == 'completed')
            self.assertEqual(completed['retry_count'], 1)
            self.assertEqual([(row['kind'], row['retry_count']) for row in journal.query(['retry-event'])['retry-event']],
                             [('model.retry', 1)])
        finally:
            model_context.reset(token)
            logger.removeHandler(log)
            logger.setLevel(level)
            log.close()
            journal.close()
            await provider.close()

    async def test_actual_request_response_redaction_privacy_failure_and_owner_detail_api(self):
        config=RuntimeConfiguration(providers=[ProviderConfig(id='p',name='服务商',base_url='https://example.com',api_key='sensitive-provider-key',max_retry=0)],
            models=[ModelConfig(model_identifier='actual-model',name='Model',api_provider='p')],
            task_configs={'replyer':TaskConfig(model_list=['actual-model'])})
        provider=ConfiguredModelProvider(RuntimeConfigurationStore('unused',config,memory_only=True))
        log=ConsoleLogs(Path('.'),lambda:['sensitive-provider-key'],memory_only=True)
        logger=logging.getLogger('osa');level=logger.level;logger.setLevel(logging.INFO);logger.addHandler(log)
        fail=False
        async def handler(request):
            if fail:return httpx.Response(503)
            return httpx.Response(200,json={'choices':[{'message':{'content':'模型返回正文 sensitive-provider-key','reasoning_content':'never-record-this'}}]})
        await provider.client.aclose();provider.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        context=model_context.set({'event_id':'event-1','collect_messages':True})
        try:
            await provider.generate([{'role':'system','content':'管理员人设 sensitive-provider-key'},{'role':'user','content':'用户请求'}],'reply')
            row=next(r for r in log.query()['items'] if r.get('detail_id'))
            call=log.model_detail(row['detail_id'])
            self.assertEqual(call['model'],'actual-model')
            self.assertEqual(call['provider_name'],'服务商')
            self.assertNotIn('messages',call)
            self.assertIn('模型返回正文',call['response'])
            self.assertNotIn('sensitive-provider-key',str(call))
            self.assertNotIn('never-record-this',str(call))
            self.assertEqual(call['status'],'completed')
            self.assertGreaterEqual(call['first_response_ms'],0)
            self.assertGreater(call['output_tokens_per_second'],0)
            self.assertEqual(call['retry_count'],0)
            app=create_app(demo_settings());app.state.console_logs=log
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
                path='/api/v1/operations/logs/model-calls/'+call['id']
                self.assertEqual((await client.get(path)).status_code,401)
                response=await client.get(path,headers={'Authorization':'Bearer admin-test-token-12345'})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['data']['model'],'actual-model')
            model_context.set({'collect_messages':False})
            await provider.generate([{'role':'user','content':'do-not-retain-user-message'}],'reply')
            private_call=log.model_detail(next(r['detail_id'] for r in log.query()['items'] if r.get('detail_id')))
            self.assertNotIn('do-not-retain-user-message',str(private_call))
            self.assertNotIn('模型返回正文',str(private_call))
            fail=True
            with self.assertRaises(Exception):await provider.generate([{'role':'user','content':'failed request'}],'reply')
            failed=log.model_detail(next(r['detail_id'] for r in log.query()['items'] if r.get('detail_id')))
            self.assertEqual(failed['status'],'failed')
            self.assertEqual(failed['http_status'],503)
        finally:
            model_context.reset(context);logger.removeHandler(log);logger.setLevel(level)
            log.close();await provider.close()

    def test_model_details_survive_restart_and_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            log=ConsoleLogs(Path(directory),lambda:['private-key'])
            call_id='a'*32
            log.emit(logging.makeLogRecord({'name':'osa.test','levelno':20,'levelname':'INFO','msg':'model_request',
                'model_call':{'id':call_id,'messages':[{'role':'system','content':'prompt private-key'}],'status':'requesting'}}))
            log.emit(logging.makeLogRecord({'name':'osa.test','levelno':20,'levelname':'INFO','msg':'model_response',
                'model_call':{'id':call_id,'response':'x'*50000,'status':'completed'}}))
            log.close()
            restarted=ConsoleLogs(Path(directory),lambda:['private-key'])
            value=restarted.model_detail(call_id)
            self.assertIn('prompt',str(value['messages']))
            self.assertNotIn('private-key',str(value))
            self.assertIn('截断',value['response'])
            self.assertLess(len(value['response']),17000)
            restarted.close()
