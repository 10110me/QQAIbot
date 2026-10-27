import asyncio,base64,io,json,tempfile,time,unittest
from dataclasses import replace
from unittest.mock import AsyncMock,MagicMock,patch
from types import SimpleNamespace

import httpx
from PIL import Image

from osa.domain.entities import Destination,GroupPolicy,ModelResult
from osa.main import create_app
from osa.application.features.image_context import ImageContext,vision_image
from osa.application.message_context import render_event,render_raw_message
from osa.infrastructure.features.media_provider import RoutedMediaProvider
from osa.infrastructure.configured_models import ConfiguredModelProvider
from osa.infrastructure.runtime_config import RuntimeConfigurationStore,RuntimeConfiguration,ProviderConfig,ModelConfig,TaskConfig
from test_core import demo_settings,make_event,payload
from osa.infrastructure.onebot import normalize


def png():
    out=io.BytesIO();Image.new('RGB',(32,32),'red').save(out,format='PNG');return out.getvalue()


class ImageFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(demo_settings().model_copy(update={'media_enabled':True,'data_dir':self.temp.name}))
        self.store=self.app.state.store;self.f=self.app.state.features
        await self.store.initialize(['10001','10002'])
        await self.store.update_policy('10001',GroupPolicy(media_read=True,sandbox=True))
        self.f.downloader.fetch=AsyncMock(return_value=(png(),'image/png','https://example.test/pic'))
        self.f.media.provider.analyze_image=AsyncMock(return_value='一张红色方块图片。')

    async def asyncTearDown(self):
        await self.f.images.shutdown();await self.f.learning.shutdown();await self.store.close()
        self.temp.cleanup()

    async def image(self,number='image',actor='12345'):
        event=replace(make_event(message_id=number,actor=actor,text=''),segments=[{'type':'image','data':{}}])
        await self.store.publish(event,True)
        self.f.images.register(event,[{'kind':'image','url':'https://example.test/pic'}])
        return event

    async def test_pending_image_is_visible_before_recognition_finishes(self):
        gate=asyncio.Event()
        async def describe(*args):
            await gate.wait()
            return '识别完成'
        self.f.media.provider.analyze_image.side_effect=describe
        image=await self.image('pending-image')
        rows=await self.f.images.context(image,wait_seconds=0)
        self.assertEqual(rows[0]['status'],'pending')
        self.assertIn('不能猜测',rows[0]['note'])
        gate.set()
        self.assertEqual((await self.f.images.context(image,wait_seconds=2))[0]['status'],'ready')

    async def test_later_mention_sees_earlier_image_as_pending(self):
        gate=asyncio.Event()
        async def describe(*args):
            await gate.wait()
            return '识别完成'
        self.f.media.provider.analyze_image.side_effect=describe
        picture=await self.image('pending-before-mention')
        mention=make_event(message_id='early-mention',text='nova 看看刚才的图')
        await self.store.publish(mention,True)
        rows=await self.f.images.context(mention,[picture.id],wait_seconds=0)
        self.assertEqual(rows[0]['source_id'],picture.id)
        self.assertEqual(rows[0]['status'],'pending')
        gate.set()
        self.assertEqual((await self.f.images.context(mention,[picture.id],wait_seconds=2))[0]['status'],'ready')

    async def test_prompt_message_has_time_name_and_safe_missing_qq(self):
        event=await self.image('dated-image')
        rendered=render_event(event,include_id=True)
        self.assertIn('QQ:12345',rendered)
        self.assertIn('[时间:',rendered)
        forwarded=render_raw_message({'sender':{'nickname':'转发者'},'message_id':'forwarded',
            'time':event.occurred_at,'message':[{'type':'text','data':{'text':'你好'}}]})
        self.assertIn('转发者(QQ:未知)',forwarded)

    async def test_concurrent_images_share_one_vlm_request(self):
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult(
            '["第一张红色方块","第二张蓝色方块"]','test','vision')))
        provider=RoutedMediaProvider(model,self.f.media.provider)
        from osa.domain.context import model_context
        model_context.set({'group_id':'10001','actor_id':'12345'})
        results=await asyncio.gather(
            provider.analyze_image(png(),'image/png','描述第一张'),
            provider.analyze_image(png(),'image/png','描述第二张'))
        self.assertEqual(results,['第一张红色方块','第二张蓝色方块'])
        model.generate.assert_awaited_once()
        content=model.generate.await_args.args[0][1]['content']
        self.assertEqual(sum(item['type']=='image_url' for item in content),2)

    async def test_ingest_recognizes_image_without_reply_or_external_consultation(self):
        raw=payload(message_id='automatic-image',mention=False,text='')
        raw['message']=[{'type':'image','data':{'url':'https://example.test/pic'}}]
        event=normalize(raw,'99999',self.app.state.settings.adapter_id)
        await self.app.state.ingest(raw)
        recognized=await self.f.images.context(event,wait_seconds=2)
        self.assertEqual(recognized[0]['status'],'ready')
        self.assertEqual(recognized[0]['text'],'一张红色方块图片。')
        self.f.media.provider.analyze_image.assert_awaited_once()
        self.assertFalse(self.store.outboxes)

    async def test_qq_stickers_are_collected_once_and_require_review(self):
        self.f.media.provider.analyze_image.return_value=json.dumps({
            'is_sticker':True,'description':'红色方块开心表情'})
        events=[]
        for message_id in ('sticker-one','sticker-two'):
            raw=payload(message_id=message_id,mention=False,text='')
            raw['message']=[{'type':'image','data':{'url':'https://example.test/pic','sub_type':'1'}}]
            event=normalize(raw,'99999',self.app.state.settings.adapter_id)
            events.append(event)
            await self.app.state.ingest(raw)
            await self.f.images.context(event,wait_seconds=2)
        rows=await self.f.repo.find('sticker',private_only=True)
        self.assertEqual(len(rows),1)
        self.assertIsNone(rows[0].group_id)
        self.assertEqual(rows[0].status,'candidate')
        self.assertIn('红色',rows[0].payload['description'])
        self.assertIsNone(await self.f.media.select_sticker('10001','红色'))
        await self.f.repo.patch(rows[0].id,status='active')
        self.assertEqual((await self.f.media.select_sticker('10001','红色')).id,rows[0].id)
        self.assertEqual((await self.f.media.select_sticker('10002','红色')).id,rows[0].id)
        await self.store.update_policy('10002',GroupPolicy(media_read=True))
        attached=await self.f.attachments([rows[0].payload['asset_id']],Destination('group','10002'),'54321')
        self.assertEqual(attached[0]['type'],'image')
        self.assertIsNone(await self.f.media.select_sticker('10001','伤心'))
        await self.store.recall(replace(events[0],event_type='message.recalled'))
        await self.f.recalled(replace(events[0],event_type='message.recalled'))
        self.assertIsNone(await self.f.repo.get(rows[0].id))

    async def test_plain_picture_with_emoji_subtype_is_not_collected(self):
        self.f.media.provider.analyze_image.return_value=json.dumps({
            'is_sticker':False,'description':'一张普通照片'})
        raw=payload(message_id='ordinary-photo',mention=False,text='')
        raw['message']=[{'type':'image','data':{'url':'https://example.test/pic','sub_type':'1'}}]
        event=normalize(raw,'99999',self.app.state.settings.adapter_id)
        await self.app.state.ingest(raw)
        result=await self.f.images.context(event,wait_seconds=2)
        self.assertEqual(result[0]['text'],'一张普通照片')
        self.assertFalse(result[0]['emoji'])
        self.assertEqual(await self.f.repo.find('sticker'),[])

    async def test_sticker_review_and_thumbnail_api(self):
        asset=await self.f.media.save(png(),'image/png','image','10001','12345')
        row=await self.f.media.add_sticker(asset.id,['开心'])
        self.assertIsNone(row.group_id)
        self.assertEqual((await self.f.media.select_sticker('10002','开心')).id,row.id)
        headers={'Authorization':'Bearer '+self.app.state.settings.admin_token.get_secret_value()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test') as client:
            preview=await client.get('/api/v1/stickers/'+row.id+'/thumbnail',headers=headers)
            self.assertEqual(preview.status_code,200)
            self.assertEqual(preview.headers['content-type'],'image/webp')
            update=await client.patch('/api/v1/stickers/'+row.id,headers=headers,json={
                'status':'rejected','description':'红色开心方块','tags':['开心','方块']})
            self.assertEqual(update.status_code,200)
            self.assertIsNone(await self.f.media.select_sticker('10001','开心'))
            listed=await client.get('/api/v1/stickers?group_id=10001',headers=headers)
            self.assertEqual(listed.json()['data'][0]['status'],'rejected')

    async def test_real_bytes_are_described_cached_and_available_after_service_restart(self):
        event=await self.image()
        first=await self.f.images.context(event,wait_seconds=2)
        self.assertEqual(first[0]['status'],'ready')
        self.assertIn('红色',first[0]['text'])
        second=await self.image('same-picture','54321')
        await self.f.images.context(second,wait_seconds=2)
        self.f.media.provider.analyze_image.assert_awaited_once()
        restarted=ImageContext(self.f)
        self.assertEqual((await restarted.context(event))[0]['text'],first[0]['text'])
        await restarted.shutdown()

    async def test_quoted_other_members_image_is_bound_exactly_and_recall_revokes(self):
        image=await self.image(actor='54321');await self.f.images.context(image,wait_seconds=2)
        question=replace(make_event(message_id='question',text='看看这张图是什么'),reply_to_id=image.message_id)
        await self.store.publish(question,True)
        rows=await self.f.images.context(question)
        self.assertEqual(rows[0]['source_id'],image.id)
        attachments=await self.f.deepseek_attachments(question)
        self.assertEqual(len(attachments),1)
        self.assertEqual(base64.b64decode(attachments[0]['data_base64']),png())
        work=await self.f.work_inputs.capture_media(question)
        self.assertEqual(work[0].owner_id,question.actor_id)
        self.assertEqual(work[0].payload['source_id'],image.id)
        foreign=replace(question,destination=make_event(group='10002').destination)
        await self.store.publish(foreign,True)
        self.assertEqual(await self.f.images.attachments(foreign),[])
        await self.store.recall(replace(image,event_type='message.recalled'))
        self.assertEqual(await self.f.images.attachments(question),[])

    async def test_unrelated_images_are_not_uploaded_and_failures_are_not_descriptions(self):
        image=await self.image();await self.f.images.context(image,wait_seconds=2)
        question=make_event(message_id='unrelated',text='Python 怎么安装')
        await self.store.publish(question,True)
        self.assertEqual(await self.f.deepseek_attachments(question),[])
        self.f.downloader.fetch.return_value=(b'not an image','text/plain','https://example.test/fail')
        broken=await self.image('broken')
        rows=await self.f.images.context(broken,wait_seconds=2)
        self.assertEqual(rows[0]['status'],'failed');self.assertEqual(rows[0]['text'],'')

    async def test_vague_image_followup_uses_only_same_senders_recent_images(self):
        own_first=await self.image('own-first',actor='12345')
        own_second=await self.image('own-second',actor='12345')
        foreign=await self.image('foreign-image',actor='54321')
        for image in (own_first,own_second,foreign):
            await self.f.images.context(image,wait_seconds=2)
        question=make_event(message_id='evaluate-images',actor='12345',text='如何评价')
        await self.store.publish(question,True)
        images=await self.f.images.context(question)
        self.assertEqual({item['source_id'] for item in images},{own_first.id,own_second.id})

    async def test_missing_transient_url_is_recovered_from_exact_qq_message(self):
        event=replace(make_event(message_id='recover-original',text=''),segments=[{'type':'image','data':{}}])
        await self.store.publish(event,True)
        self.f.platform.call=AsyncMock(return_value={'group_id':'10001','message':[{'type':'image','data':{'url':'https://example.test/pic'}}]})
        result=await self.f.images.context(event,wait_seconds=2)
        self.assertEqual(result[0]['status'],'ready')
        self.f.platform.call.assert_awaited_once_with('get_msg',{'message_id':event.message_id})

    async def test_large_quoted_image_is_downloaded_once_and_uploaded_unchanged(self):
        original=png()+b'\0'*(21_000_001-len(png()))
        async def download(url,*,max_bytes):
            if max_bytes and len(original)>max_bytes:raise ValueError('Download exceeds configured size')
            return original,'image/png',url
        self.f.downloader.fetch.side_effect=download
        image=await self.image('large-original')
        await self.f.images.context(image,wait_seconds=2)
        question=replace(make_event(message_id='large-question',text='这个是谁？'),reply_to_id=image.message_id)
        await self.store.publish(question,True)
        attachments=await self.f.deepseek_attachments(question)
        self.assertEqual(len(attachments),1)
        uploaded=base64.b64decode(attachments[0]['data_base64'])
        self.assertEqual(uploaded,original)
        with Image.open(io.BytesIO(uploaded)) as decoded:
            self.assertEqual(decoded.getpixel((0,0)),(255,0,0))
        self.f.downloader.fetch.assert_awaited_once()
        rows=await self.f.work_inputs.recent(question)
        self.assertEqual(await self.f.objects.get(rows[0].payload['object_key']),original)
        self.assertEqual(self.f.downloader.fetch.await_args.kwargs['max_bytes'],0)

    async def test_local_vision_failure_still_allows_original_deepseek_upload(self):
        self.f.media.provider.analyze_image.side_effect=ValueError('Local vision unavailable')
        image=await self.image('vision-failed-original')
        self.assertEqual((await self.f.images.context(image,wait_seconds=2))[0]['status'],'failed')
        self.assertEqual(base64.b64decode((await self.f.deepseek_attachments(image))[0]['data_base64']),png())

    async def test_explicit_image_download_bypasses_only_byte_limit(self):
        from osa.infrastructure.features.downloader import PublicDownloader
        async def chunks():yield png()
        response=SimpleNamespace(status=200,headers={'Content-Type':'image/png'},content_length=len(png()),
            raise_for_status=lambda:None,content=SimpleNamespace(iter_chunked=lambda size:chunks()))
        client=MagicMock();client.get.return_value.__aenter__.return_value=response
        session=MagicMock();session.__aenter__.return_value=client
        with patch('osa.infrastructure.features.downloader.aiohttp.ClientSession',return_value=session), \
             patch('osa.infrastructure.features.downloader.aiohttp.TCPConnector'):
            downloader=PublicDownloader(limit=10)
            with self.assertRaises(ValueError):await downloader.fetch('https://example.test/image')
            self.assertEqual((await downloader.fetch('https://example.test/image',max_bytes=0))[0],png())
            with self.assertRaises(PermissionError):await downloader.fetch('http://127.0.0.1/image',max_bytes=0)

    async def test_failed_quoted_image_does_not_redownload_through_file_path(self):
        self.f.media_inputs={}
        image=replace(make_event(message_id='oversize',text=''),segments=[{'type':'image','data':{}}])
        await self.store.publish(image,True)
        self.f.downloader.fetch.side_effect=ValueError('Download exceeds configured size')
        await self.f.observe(image,[{'kind':'image','url':'https://example.test/large'}])
        await self.f.images.context(image,wait_seconds=2)
        question=replace(make_event(message_id='oversize-question',text='这个是谁？'),reply_to_id=image.message_id)
        await self.store.publish(question,True)
        self.assertEqual(await self.f.deepseek_attachments(question),[])
        self.f.downloader.fetch.assert_awaited_once()
        rows=await self.f.images.context(question)
        self.assertEqual(rows[0]['status'],'failed')
        # A failed source must be retryable after the cooldown, rather than
        # being permanently blocked by the in-process source map.
        self.f.images.fetch_attempts[image.id]=0
        self.f.downloader.fetch.side_effect=None
        self.assertEqual((await self.f.images.context(question,wait_seconds=2))[0]['status'],'ready')

    async def test_direct_quoted_image_reaches_thinking_and_queues_correct_deepseek_upload(self):
        from osa.application.v2.service import V2Service
        from osa.application.v3.service import V3Service
        from osa.infrastructure.v3.registry import Registry
        settings=self.app.state.settings.model_copy(update={'v2_enabled':True,'v3_enabled':True,'deepseek_web_enabled':True})
        v2=V2Service(Registry(self.temp.name+'/registry.db'),settings)
        v2.store=self.store;v2.work_inputs=self.f.work_inputs
        runtime=self.app.state.runtime;runtime.v2=v2
        runtime.v3=V3Service(settings,runtime.model,v2,self.store,features=self.f,browser=self.f.research.browser)
        await self.store.update_policy('10001',GroupPolicy(media_read=True,research_external=True))
        image=await self.image('character-image');await self.f.images.context(image,wait_seconds=2)
        question=replace(make_event(message_id='who-question',text='这个是谁？'),reply_to_id=image.message_id)
        await self.store.publish(question,True)
        self.assertEqual(await self.f.deepseek_attachments(question,include_images=False),[])
        gates={'session_id':'image-gates','tool_call_id':'unrequested','actor_id':question.actor_id,
               'group_id':'10001','source_id':question.id,'source_event':question.to_dict(),
               'image_source_ids':[image.id],'bot_actions_requested':False,'deadline':time.time()+120}
        await v2.call('begin_thinking','image-gates',gates)
        denied=await runtime.v3.thinking.tools.execute('deepseek.consult',
            {'question':'这个是谁？','image_source_id':image.id,'vision_gap':'未认出角色'},gates)
        self.assertEqual(denied['error'],'DIRECT_REQUEST_REQUIRED')
        gates['tool_call_id']='ready-without-gap';gates['bot_actions_requested']=True
        denied=await runtime.v3.thinking.tools.execute('deepseek.consult',
            {'question':'这个是谁？','image_source_id':image.id},gates)
        self.assertEqual(denied['error'],'LOCAL_VISION_AVAILABLE')
        self.assertEqual(await v2.call('records','night_research',question.actor_id,'10001'),[])
        async def generate(messages,purpose='chat'):
            if 'tool_result' in messages[-1]['content']:
                task=json.loads(messages[-1]['content'])['tool_result']['task_id']
                value={'final_action':'TASK_DELEGATED','delegated_task_ids':[task]}
            else:
                value={'tool_call':{'tool_call_id':'identify-character','name':'deepseek.consult',
                    'arguments':{'question':'这个是谁？','image_source_id':image.id,
                                 'vision_gap':'本地图片模型只看到红色方块，未能确认角色'}}}
            return ModelResult(json.dumps(value),'test','test')
        runtime.model.generate=AsyncMock(side_effect=generate)
        await runtime.process(question)
        rows=await v2.call('records','night_research',question.actor_id,'10001')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['state'],'QUEUED')
        self.assertEqual(rows[0]['payload']['question'],'这个是谁？')
        upload=rows[0]['payload']['attachments']
        self.assertEqual(base64.b64decode(upload[0]['data_base64']),png())
        self.assertEqual([c.args[1] for c in runtime.model.generate.await_args_list],['thinking','thinking'])
        self.assertEqual(self.store.decisions[question.id]['reason'],'thinking_task_delegated')

    async def test_failed_local_vision_allows_requested_original_image_fallback(self):
        from osa.application.v2.service import V2Service
        from osa.application.v3.service import V3Service
        from osa.infrastructure.v3.registry import Registry
        settings=self.app.state.settings.model_copy(update={'v2_enabled':True,'v3_enabled':True,'deepseek_web_enabled':True})
        v2=V2Service(Registry(self.temp.name+'/fallback-registry.db'),settings)
        runtime=self.app.state.runtime
        runtime.v3=V3Service(settings,runtime.model,v2,self.store,features=self.f,browser=self.f.research.browser)
        await self.store.update_policy('10001',GroupPolicy(media_read=True,research_external=True))
        self.f.media.provider.analyze_image.side_effect=ValueError('vision unavailable')
        image=await self.image('failed-local-image')
        self.assertEqual((await self.f.images.context(image,wait_seconds=2))[0]['status'],'failed')
        question=replace(make_event(message_id='failed-local-question',text='这张图里有什么？'),
                         reply_to_id=image.message_id)
        await self.store.publish(question,True)
        context={'session_id':'failed-local-session','tool_call_id':'fallback','actor_id':question.actor_id,
                 'group_id':'10001','source_id':question.id,'source_event':question.to_dict(),
                 'image_source_ids':[image.id],'bot_actions_requested':True,'deadline':time.time()+120}
        await v2.call('begin_thinking',context['session_id'],context)
        result=await runtime.v3.thinking.tools.execute('deepseek.consult',
            {'question':'这张图里有什么？','image_source_id':image.id},context)
        self.assertEqual(result['status'],'QUEUED')
        row=(await v2.call('records','night_research',question.actor_id,'10001'))[0]
        self.assertEqual(base64.b64decode(row['payload']['attachments'][0]['data_base64']),png())

    async def test_deepseek_template_endpoint_returns_only_the_question(self):
        self.app.state.settings.worker_token=__import__('pydantic').SecretStr('image-test-worker-token-123456')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test') as client:
            response=await client.post('/internal/prompt-template/deepseek_consult',
                headers={'Authorization':'Bearer image-test-worker-token-123456'},json={'question':'这张图里的角色是谁？'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['data']['text'],'这张图里的角色是谁？')

    async def test_ingest_runs_image_work_in_background_and_disabled_policy_never_calls_vlm(self):
        gate=asyncio.Event()
        async def describe(*args):await gate.wait();return '完成'
        self.f.media.provider.analyze_image.side_effect=describe
        event=await self.image();await asyncio.sleep(.01)
        self.assertTrue(self.f.images.pending)
        gate.set();await self.f.images.context(event,wait_seconds=2)
        await self.store.update_policy('10001',GroupPolicy(media_read=False))
        other=await self.image('blocked')
        self.assertEqual(await self.f.images.context(other),[])
        self.assertEqual(self.f.media.provider.analyze_image.await_count,1)

    async def test_configured_vlm_receives_image_bytes_and_uses_its_provider(self):
        config=RuntimeConfiguration(providers=[ProviderConfig(id='v',name='视觉厂商',base_url='https://vision.example/v1',max_retry=0)],
            models=[ModelConfig(model_identifier='vision-model',name='视觉模型',api_provider='v',capabilities=['image'])],
            task_configs={'vlm':TaskConfig(model_list=['vision-model'])})
        provider=ConfiguredModelProvider(RuntimeConfigurationStore(self.temp.name,config,memory_only=True))
        seen=[]
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':'红色方块'},'finish_reason':'stop'}]})
        await provider.client.aclose();provider.client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        routed=RoutedMediaProvider(provider,self.f.media.provider)
        try:
            self.assertEqual(await routed.analyze_image(png(),'image/png'),'红色方块')
            sent=seen[0]['messages'][-1]['content'][1]['image_url']['url']
            self.assertEqual(base64.b64decode(sent.split(',',1)[1]),png())
            self.assertEqual(seen[0]['model'],'vision-model')
        finally:await provider.close()

    async def test_complete_video_uses_video_route_without_extracting_frames_or_audio(self):
        model=SimpleNamespace(generate=AsyncMock(return_value=ModelResult('视频画面','test','video')))
        self.f.media.provider=RoutedMediaProvider(model,self.f.media.provider)
        self.f.media.video_worker=SimpleNamespace(frames=AsyncMock(return_value=[png()]),
                                                  audio=AsyncMock(return_value=None))
        asset=await self.f.media.save(b'video fixture','video/mp4','video','10001','12345')
        result=await self.f.media.analyze(asset.id,'描述内容')
        self.assertIn('视频画面',result['text'])
        self.assertEqual(result['input_mode'], 'direct_video')
        self.assertNotIn('关键帧', result['text'])
        self.assertEqual(model.generate.await_args.args[1],'video')
        parts = model.generate.await_args.args[0][-1]['content']
        self.assertEqual(parts[1]['type'], 'video_url')
        self.assertEqual(base64.b64decode(parts[1]['video_url']['url'].split(',', 1)[1]), b'video fixture')
        self.f.media.video_worker.frames.assert_not_awaited()
        self.f.media.video_worker.audio.assert_not_awaited()

    async def test_direct_video_does_not_require_a_local_video_worker(self):
        model = SimpleNamespace(generate=AsyncMock(return_value=ModelResult('视频回答', 'test', 'video')))
        self.f.media.provider = RoutedMediaProvider(model, self.f.media.provider)
        self.f.media.video_worker = None
        asset = await self.f.media.save(b'video fixture', 'video/mp4', 'video', '10001', '12345')
        self.assertEqual((await self.f.media.analyze(asset.id, '按顺序描述'))['text'], '视频回答')

    async def test_video_provider_failure_is_not_saved_as_successful_frame_analysis(self):
        model = SimpleNamespace(generate=AsyncMock(side_effect=ValueError('Video input rejected')))
        self.f.media.provider = RoutedMediaProvider(model, self.f.media.provider)
        self.f.media.video_worker = SimpleNamespace(frames=AsyncMock(), audio=AsyncMock())
        asset = await self.f.media.save(b'video fixture', 'video/mp4', 'video', '10001', '12345')
        with self.assertRaisesRegex(ValueError, 'Video input rejected'):
            await self.f.media.analyze(asset.id, '描述内容')
        self.assertEqual(await self.f.repo.find('media_analysis'), [])
        self.f.media.video_worker.frames.assert_not_awaited()
        self.f.media.video_worker.audio.assert_not_awaited()

    async def test_native_video_request_reaches_the_configured_openai_transport(self):
        config = RuntimeConfiguration(providers=[ProviderConfig(id='v', name='V', base_url='https://video.example/v1', max_retry=0)],
            models=[ModelConfig(model_identifier='video-model', name='Video', api_provider='v', capabilities=['video'])],
            task_configs={'video': TaskConfig(model_list=['video-model'], max_tokens=4096)})
        provider = ConfiguredModelProvider(RuntimeConfigurationStore('', config, memory_only=True))
        seen = []
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': '红蓝绿'}, 'finish_reason': 'stop'}]})
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            routed = RoutedMediaProvider(provider, self.f.media.provider)
            raw = b'\x00\x00\x00\x18ftyp' + b'x' * 200000
            self.assertEqual(await routed.analyze_video(raw, 'application/octet-stream', '描述变化'), '红蓝绿')
            self.assertEqual(seen[0]['model'], 'video-model')
            url = seen[0]['messages'][-1]['content'][1]['video_url']['url']
            self.assertTrue(url.startswith('data:video/mp4;base64,'))
            self.assertEqual(base64.b64decode(url.split(',', 1)[1]), raw)
            with self.assertRaises(ValueError):
                await routed.analyze_video(b'', 'video/mp4')
            with self.assertRaises(ValueError):
                await routed.analyze_video(b'not video', 'image/png')
            self.assertEqual(len(seen), 1)
        finally:
            await provider.close()

    async def test_broken_vlm_fails_over_before_repeated_slow_retries(self):
        config=RuntimeConfiguration(providers=[ProviderConfig(id='v',name='V',base_url='https://vision.example/v1',max_retry=2,retry_interval=10)],
            models=[ModelConfig(model_identifier=name,name=name,api_provider='v',capabilities=['image']) for name in ['broken','healthy']],
            task_configs={'vlm':TaskConfig(model_list=['broken','healthy'])})
        provider=ConfiguredModelProvider(RuntimeConfigurationStore(self.temp.name,config,memory_only=True))
        seen=[]
        def respond(request):
            model=json.loads(request.content)['model'];seen.append(model)
            if model=='broken':return httpx.Response(500,json={'error':{'message':'unavailable'}})
            return httpx.Response(200,json={'choices':[{'message':{'content':'红色方块'},'finish_reason':'stop'}]})
        await provider.client.aclose();provider.client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            self.assertEqual((await provider.generate([{'role':'user','content':'test'}],'vision')).text,'红色方块')
            self.assertEqual(seen,['broken','healthy'])
        finally:await provider.close()

    def test_animated_gif_uses_actual_frames(self):
        out=io.BytesIO();frames=[Image.new('RGB',(20,20),c) for c in ['red','blue']]
        frames[0].save(out,format='GIF',save_all=True,append_images=frames[1:],duration=100)
        content,mime,animated=vision_image(out.getvalue())
        self.assertTrue(animated);self.assertEqual(mime,'image/png')
        with Image.open(io.BytesIO(content)) as image:self.assertEqual(image.size,(40,20))
