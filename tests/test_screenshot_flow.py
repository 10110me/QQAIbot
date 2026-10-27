import base64,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock

from osa.main import create_app
from osa.domain.entities import GroupPolicy,ModelResult
from osa.domain.features import ScreenshotFailed
from osa.application.v3.thinking import ThinkingLoop
from test_core import demo_settings,make_event
from test_image_context import png


class ScreenshotFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(demo_settings().model_copy(update={'sandbox_enabled':True,'v2_enabled':True,
            'data_dir':self.temp.name,'v2_database_url':str(Path(self.temp.name)/'registry.db')}))
        self.store=self.app.state.store;self.f=self.app.state.features;self.v2=self.app.state.v2
        await self.store.initialize(['10001','10002'])
        await self.store.update_policy('10001',GroupPolicy(media_read=True,sandbox=True))
        self.event=make_event(text='访问 https://example.com 并把截图给我')
        await self.store.publish(self.event,True)
        self.ctx={'actor_id':self.event.actor_id,'group_id':'10001','source_id':self.event.id,
            'source_event':self.event.to_dict(),'evidence_ids':[self.event.id],
            'dependency_snapshot':{'source_ids':[self.event.id]}}
        self.f.screenshots.screenshot=AsyncMock(return_value={'content':base64.b64encode(png()).decode(),
            'http_status':403,'title':'Forbidden','full_page':True})
        self.tools=self.app.state.v3.thinking.tools

    async def asyncTearDown(self):
        await self.f.images.shutdown();await self.f.learning.shutdown();await self.store.close()
        self.temp.cleanup()

    async def invoke(self,url='https://example.com',sid='screenshot'):
        await self.v2.call('begin_thinking',sid,self.ctx)
        return await self.tools.execute('browser.screenshot',{'url':url},
            {**self.ctx,'session_id':sid,'tool_call_id':'shot','deadline':time.time()+60})

    async def test_real_image_can_be_sent_and_receipt_prevents_duplicate_capture(self):
        result=await self.invoke()
        self.assertEqual(result['http_status'],403)
        self.assertEqual(result['_attachment_ids'],[result['asset_id']])
        images=await self.f.attachments(result['_attachment_ids'],self.event.destination,self.event.actor_id)
        self.assertEqual(base64.b64decode(images[0]['content']),png())
        self.assertEqual((await self.invoke())['asset_id'],result['asset_id'])
        self.f.screenshots.screenshot.assert_awaited_once()
        self.assertEqual(await self.v2.call('tasks',self.event.actor_id,'10001'),[])

    async def test_permissions_private_urls_and_revocation_block_capture(self):
        self.assertIn('error',await self.invoke('http://127.0.0.1','private-url'))
        self.f.screenshots.screenshot.assert_not_awaited()
        await self.store.update_policy('10001',GroupPolicy(media_read=True,sandbox=False))
        self.assertIn('error',await self.invoke(sid='denied'))
        self.f.screenshots.screenshot.assert_not_awaited()
        await self.store.update_policy('10001',GroupPolicy(media_read=True,sandbox=True))
        async def revoked(url):
            await self.store.update_policy('10001',GroupPolicy(media_read=True,sandbox=False))
            return {'content':base64.b64encode(png()).decode()}
        self.f.screenshots.screenshot.side_effect=revoked
        self.assertIn('error',await self.invoke(sid='revoked'))
        self.assertEqual(await self.f.repo.find('media'),[])

    async def test_capture_timeout_is_returned_to_thinking_as_an_actionable_receipt(self):
        self.f.screenshots.screenshot.side_effect = ScreenshotFailed(
            'SCREENSHOT_TIMEOUT', '网页在生成截图时超时，尚未取得图片。', True)
        result = await self.invoke()
        self.assertEqual(result['error'], 'SCREENSHOT_TIMEOUT')
        self.assertIn('生成截图时超时', result['detail'])
        self.assertTrue(result['retryable'])
        self.assertNotIn('_attachment_ids', result)
        self.assertEqual(await self.f.repo.find('media'), [])

    async def test_thinking_can_attach_actual_result_and_retains_send_gate(self):
        async def generate(messages,purpose):
            receipts=[m for m in messages if m.get('role')=='user' and 'tool_result' in m.get('content','')]
            if not receipts:
                data={'tool_call':{'tool_call_id':'shot','name':'browser.screenshot','arguments':{'url':'https://example.com'}}}
            else:
                asset=json.loads(receipts[-1]['content'])['tool_result']['asset_id']
                data={'final_action':'REPLY','reply_brief':{'key_points':['网站返回403，这是实际错误页面截图'],
                    'attachment_ids':[asset]}}
            return ModelResult(json.dumps(data,ensure_ascii=False),'test','test')
        model=AsyncMock();model.generate.side_effect=generate
        result=await ThinkingLoop(model,self.tools,self.v2.registry).run([],self.ctx,'end-to-end')
        self.assertEqual(result.final_action,'REPLY')
        self.assertEqual(len(result.brief.attachment_ids),1)
        self.assertEqual(self.ctx['dependency_snapshot']['task_capability'],'sandbox')
        self.assertFalse(await self.f.allowed({'task_capability':'sandbox'},make_event(group='10002').destination,self.event.actor_id))

    async def test_batch_other_requester_receives_actual_attachment_receipt(self):
        other=make_event(message_id='other-request',actor='54321',text='给我截个网页')
        await self.store.publish(other,True)
        await self.v2.call('begin_thinking','batch-root',self.ctx)
        result=await self.tools.execute('browser.screenshot',{'url':'https://example.com','request_id':other.id},
            {**self.ctx,'request_events':[self.event.to_dict(),other.to_dict()],
             'session_id':'batch-root','tool_call_id':'batch-shot','deadline':time.time()+60})
        self.assertEqual(result['_attachment_ids'],[result['asset_id']])
        asset=await self.f.repo.get(result['asset_id'])
        self.assertEqual(asset.owner_id,'54321')
        self.assertEqual(asset.group_id,'10001')
