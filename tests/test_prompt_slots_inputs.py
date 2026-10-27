import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from osa.application.prompt_catalog import CATALOG,render_prompt,validate_template
from osa.application.prompting import PromptGuard
from osa.application.work_inputs import WorkInputs,attachment_text
from osa.application.v2.service import V2Service
from osa.infrastructure.runtime_config import RuntimeConfiguration,RuntimeConfigurationStore
from osa.infrastructure.features.repository import MemoryFeatureRepository
from osa.infrastructure.features.objects import LocalBlobStore
from osa.infrastructure.memory_store import InMemoryStore
from osa.infrastructure.onebot import normalize
from osa.domain.entities import GroupPolicy
from osa.main import create_app
from test_core import demo_settings,make_event,payload


class SlotTests(unittest.IsolatedAsyncioTestCase):
    async def test_slot_save_preview_validation_and_actual_request_replacement(self):
        app=create_app(demo_settings())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test',
                headers={'Authorization':'Bearer admin-test-token-12345'}) as client:
            response=(await client.get('/api/v1/config/prompt-slots')).json()['data']
            self.assertEqual(len(response['slots']),len(CATALOG))
            content='名字：{机器人名字} 时间：{当前时间} JSON：{"mode":"chat"}'
            result=await client.put('/api/v1/config/prompt-slots/work_decision',json={'content':content,'revision':response['revision']})
            self.assertEqual(result.status_code,200)
            rendered=app.state.prompt_guard.slot('work_decision')
            self.assertNotIn('{当前时间}',rendered)
            self.assertIn('QQ 社交助手',rendered)
            self.assertIn('{"mode":"chat"}',rendered)
            bad=await client.put('/api/v1/config/prompt-slots/work_decision',json={'content':'{未知变量}','revision':response['revision']})
            self.assertEqual(bad.status_code,422)
            preview=await client.post('/api/v1/config/prompt-slots/chat/preview',json={'content':content,'revision':0})
            self.assertEqual(preview.status_code,200)
            messages=app.state.prompt_guard.prepare_messages([{'role':'system','content':CATALOG['work']['default']}],'work')
            self.assertIn('【提示词模板：work】',messages[0]['content'])
            current=app.state.runtime_configuration.snapshot();current.prompt_slots['chat']='仅回复中文';app.state.runtime_configuration.replace(current)
            self.assertIn('不可修改的系统边界',app.state.prompt_guard.system_prompt())

    def test_substitution_is_single_pass_and_json_is_literal(self):
        source=RuntimeConfigurationStore('unused',RuntimeConfiguration(prompt_slots={'work_decision':'{用户请求} {当前日期} {time}'}),memory_only=True)
        result=render_prompt(source,'work_decision',用户请求='{机器人名字}')
        self.assertIn('{机器人名字}',result)
        self.assertNotIn('{当前日期}',result)
        for slot in CATALOG.values():validate_template(slot['default'])
        with self.assertRaises(ValueError):validate_template('{unknown_unsafe}')

    async def test_ai_evaluates_chat_work_and_clarification(self):
        settings=SimpleNamespace(work_enabled=True)
        service=V2Service(None,settings)
        service.model=SimpleNamespace(generate=AsyncMock())
        service.call=AsyncMock(return_value={'id':'task-1','group_id':'10001'})
        event=make_event(text='根据附件帮我实现需求',mention=True)
        for mode in ['chat','work','clarify']:
            service.model.generate.return_value=SimpleNamespace(text=json.dumps({'mode':mode,'requirement':'需求摘要','question':'输出什么格式？'}))
            result=await service.maybe_work(event,GroupPolicy(sandbox=True))
            if mode=='chat':self.assertIsNone(result)
            elif mode=='work':self.assertIn('等待后台审批',result)
            else:self.assertEqual(result,'输出什么格式？')
        service.model.generate.assert_awaited()
        self.assertIn('work_decision',service.model.generate.call_args.args[0][0]['content'])


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    def test_archive_limits_traversal_and_text(self):
        def archive(name,content):
            data=io.BytesIO()
            with zipfile.ZipFile(data,'w') as z:z.writestr(name,content)
            return data.getvalue()
        self.assertEqual(attachment_text(archive('requirements.txt','实现 CSV 汇总'),'input.zip')[0]['text'],'实现 CSV 汇总')
        with self.assertRaises(ValueError):attachment_text(archive('../secret.txt','x'),'input.zip')
        with self.assertRaises(ValueError):attachment_text(archive('bomb.txt','a'*2100000),'input.zip')

    async def test_real_input_pipeline_scope_recall_and_work_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=MemoryFeatureRepository();store=InMemoryStore();await store.initialize(['10001'])
            await store.update_policy('10001',GroupPolicy(sandbox=True))
            features=SimpleNamespace(repo=repo,objects=LocalBlobStore(folder+'/objects'),
                settings=SimpleNamespace(workspace_dir=folder+'/work'),downloader=SimpleNamespace(fetch=AsyncMock(return_value=(b'print(123)','text/plain','https://example.com/input'))))
            platform=SimpleNamespace(call=AsyncMock(return_value={'url':'https://example.com/file'}))
            inputs=WorkInputs(features,platform,store)
            raw=payload(message_id='upload');raw.update(post_type='notice',notice_type='group_upload',file={'id':'file-1','name':'input.py','size':10})
            event=normalize(raw,'99999','test');await store.publish(event,True)
            self.assertEqual(event.segments[-1]['type'],'file')
            await inputs.observe(event)
            request=make_event(message_id='request',text='请修改刚才的脚本',mention=True)
            self.assertEqual(len(await inputs.recent(request)),1)
            task={'id':'a'*32,'owner_id':request.actor_id,'group_id':'10001'}
            await inputs.bind(task,request)
            values=await inputs.for_task(task,1)
            self.assertIn('print(123)',values[0]['entries'][0]['text'])
            self.assertEqual(len(list(Path(folder+'/work').rglob('*.py'))),1)
            from dataclasses import replace
            other=replace(request,actor_id='other')
            self.assertEqual(await inputs.recent(other),[])
            await store.recall(replace(event,event_type='message.recalled'))
            with self.assertRaises(PermissionError):await inputs.for_task(task,1)
