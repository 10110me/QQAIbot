import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from osa.infrastructure.configured_models import ConfiguredModelProvider,ConfiguredTaskModel
from osa.infrastructure.runtime_config import RuntimeConfigurationStore,RuntimeConfiguration,ProviderConfig,ModelConfig,TaskConfig
from osa.main import create_app
from test_core import demo_settings


class WorkModelTests(unittest.IsolatedAsyncioTestCase):
    def test_natural_python_request_is_work_intent(self):
        from osa.application.v2.service import V2Service
        self.assertTrue(V2Service.is_work_request('请帮我编写一个 Python 程序，处理 CSV'))
        self.assertTrue(V2Service.is_work_request('创建一个代码项目'))
        self.assertTrue(V2Service.is_work_request('帮我写个包装dtpweb的py包，让我可以像调用mcu的绘图api一样调用这些api'))
        self.assertFalse(V2Service.is_work_request('今天 Python 是什么'))
        self.assertFalse(V2Service.is_work_request('解释一下这段代码'))


    async def test_scoped_gateway_routes_work_separately_and_blocks_stale_tasks(self):
        settings=demo_settings();settings.worker_token=__import__('pydantic').SecretStr('work-worker-token-123456789012345')
        app=create_app(settings);await app.state.store.initialize(['10001'])
        task={'id':'task-1','generation':2,'status':'RUNNING','owner_id':'123','group_id':None,
              'requirement':'Build a project','requirement_log':[],'files':{}}
        app.state.v2=SimpleNamespace(call=AsyncMock(return_value=task))
        config=app.state.runtime_configuration
        value=config.snapshot();value.providers=[ProviderConfig(id='one',name='One',base_url='https://example.com/v1')]
        value.models=[ModelConfig(model_identifier='work-only',name='Work',api_provider='one',force_stream_mode=True)]
        value.task_configs['work']=TaskConfig(model_list=['work-only']);config.replace(value)
        provider=ConfiguredModelProvider(config);app.state.work_model=provider
        seen=[]
        async def handler(request):
            body=json.loads(request.content);seen.append(body)
            text=json.dumps({'files':{'main.py':'print(1)'}})
            return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'content':text}}]})+'\n\ndata: [DONE]\n\n')
        await provider.client.aclose();provider.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
                body={'task_id':'task-1','generation':2};headers={'Authorization':'Bearer '+settings.worker_token.get_secret_value()}
                self.assertEqual((await client.post('/internal/work-model',json=body)).status_code,401)
                self.assertEqual((await client.post('/internal/work-model',json={**body,'generation':1},headers=headers)).status_code,409)
                result=await client.post('/internal/work-model',json=body,headers=headers)
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.json()['data']['model'],'work-only')
                self.assertTrue(seen[0]['stream'])
                self.assertTrue(any('Build a project' in message['content'] for message in seen[0]['messages'] if message['role']=='user'))
                task['status']='PAUSED'
                self.assertEqual((await client.post('/internal/work-model',json=body,headers=headers)).status_code,409)
                task['status']='RUNNING';task['group_id']='10001'
                self.assertEqual((await client.post('/internal/work-model',json=body,headers=headers)).status_code,403)
        finally:await provider.close()

    async def test_worker_uses_gateway_without_receiving_provider_credentials(self):
        from osa.infrastructure.v2.worker import planner
        requests=[]
        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'data':{'text':json.dumps({'files':{'main.py':'print(1)'}})}})
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict('os.environ',{'OSA_WORK_MODEL_GATEWAY_URL':'http://app:8080','OSA_WORKER_TOKEN':'worker-token'}),patch('osa.infrastructure.v2.worker.httpx.AsyncClient',return_value=client):
            result=await planner({'id':'task-1','generation':2})
        self.assertEqual(result,{'main.py':'print(1)'})
        self.assertEqual(requests,[{'task_id':'task-1','generation':2}])

    async def test_empty_web_work_model_does_not_revive_legacy_model(self):
        from osa.infrastructure.v2.worker import planner
        requests=[]
        async def handler(request):
            requests.append(str(request.url))
            return httpx.Response(409,json={'error':{'code':'WORK_MODEL_NOT_CONFIGURED'}})
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict('os.environ',{'OSA_WORK_MODEL_GATEWAY_URL':'http://app:8080','OSA_WORKER_TOKEN':'worker-token',
                'OSA_WORK_MODEL_API_KEY':'old-key'}),patch('osa.infrastructure.v2.worker.httpx.AsyncClient',return_value=client):
            from osa.domain.work import WorkModelUnavailable
            with self.assertRaisesRegex(WorkModelUnavailable,'尚未配置 AI 工作模型'):await planner({'id':'task-1','generation':2})
        self.assertEqual(len(requests),1)

    async def test_optional_observer_hot_config_and_legacy_fallback(self):
        config=RuntimeConfigurationStore('unused',RuntimeConfiguration(task_configs={'observer':TaskConfig()}),memory_only=True)
        configured=SimpleNamespace(generate=AsyncMock(return_value='configured'))
        legacy=SimpleNamespace(generate=AsyncMock(return_value='legacy'))
        adapter=ConfiguredTaskModel(configured,config,'observer',legacy)
        self.assertEqual(await adapter.generate([]),'legacy')
        value=config.snapshot();value.providers=[ProviderConfig(id='one',name='One',base_url='https://example.com')]
        value.models=[ModelConfig(model_identifier='review',name='Review',api_provider='one')]
        value.task_configs['observer']=TaskConfig(model_list=['review']);config.replace(value)
        self.assertEqual(await adapter.generate([]),'configured')
        configured.generate.assert_awaited_with([],'observer')

    def test_existing_encrypted_config_gains_work_cards_without_changing_models(self):
        with tempfile.TemporaryDirectory() as folder:
            RuntimeConfigurationStore(folder,RuntimeConfiguration())
            restored=RuntimeConfigurationStore(folder,RuntimeConfiguration()).snapshot()
            self.assertEqual(restored.task_configs['work'].max_tokens,8192)
            self.assertIn('observer',restored.task_configs)
            self.assertEqual(restored.models,[])
