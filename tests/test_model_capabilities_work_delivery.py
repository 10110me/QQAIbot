import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from osa.application.v2.service import V2Service
from osa.domain.entities import Destination, GroupPolicy
from osa.infrastructure.onebot import OneBotConnection
from osa.main import create_app
from test_core import demo_settings,make_event


class CapabilityWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_model_id_can_be_created_edited_and_deleted(self):
        app=create_app(demo_settings())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test',
                headers={'Authorization':'Bearer admin-test-token-12345'}) as client:
            config=app.state.runtime_configuration.snapshot()
            from osa.infrastructure.runtime_config import ProviderConfig
            config.providers=[ProviderConfig(id='p',name='Provider',base_url='https://example.com')]
            app.state.runtime_configuration.replace(config)
            body={'model_identifier':'z-ai/glm-5.3-flash','name':'GLM','api_provider':'p',
                  'capabilities':['chat','image','video','embedding']}
            result=await client.put('/api/v1/config/models/z-ai%2Fglm-5.3-flash',json=body)
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.json()['data']['capabilities'],body['capabilities'])
            result=await client.delete('/api/v1/config/models/z-ai%2Fglm-5.3-flash')
            self.assertEqual(result.status_code,200)
            self.assertEqual(app.state.runtime_configuration.snapshot().models,[])
            self.assertEqual(len(app.state.runtime_configuration.snapshot().providers),1)

    async def test_natural_work_request_creates_task_and_preserves_approval(self):
        service=V2Service(None,SimpleNamespace(v3_enabled=False,work_enabled=True))
        service.call=AsyncMock(return_value={'id':'task-1'})
        event=make_event(text='请帮我编写一个 Python 程序',mention=True)
        result=await service.route(event,GroupPolicy(sandbox=True))
        self.assertIn('等待后台审批',result)
        self.assertEqual(service.call.call_args.args[0],'create')
        service.call.reset_mock()
        result=await service.route(event,GroupPolicy(sandbox=False))
        self.assertIn('未授权',result);service.call.assert_not_awaited()

    async def test_file_delivery_uses_verified_artifact_and_upload_api(self):
        with tempfile.TemporaryDirectory() as folder:
            task_id='a'*32;content=b'zip-project';digest=hashlib.sha256(content).hexdigest()
            path=Path(folder)/task_id/'generation-1'/'project.zip'
            path.parent.mkdir(parents=True);path.write_bytes(content)
            service=V2Service(None,SimpleNamespace(workspace_dir=folder))
            service.call=AsyncMock(return_value={'status':'COMPLETED','artifact_id':'1:'+digest})
            destination=Destination('group','10001')
            self.assertEqual(await service.artifact_bytes(task_id,'12345',destination),content)
            platform=OneBotConnection();platform.call=AsyncMock(return_value={})
            await platform.send_file(destination,'project.zip',content)
            action,args=platform.call.call_args.args
            self.assertEqual(action,'upload_group_file');self.assertEqual(args['group_id'],10001)
            self.assertTrue(args['file'].startswith('base64://'))
            path.write_bytes(b'changed')
            with self.assertRaises(PermissionError):await service.artifact_bytes(task_id,'12345',destination)
