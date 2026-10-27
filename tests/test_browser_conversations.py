"""Browser worker session routing tested with page doubles, no browser process."""
import importlib.util
import base64
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock,patch

import httpx

from test_v2 import V2AsyncCase


class BrowserConversationTests(V2AsyncCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        workers = Path(__file__).resolve().parents[1]/'workers'
        sys.path.insert(0, str(workers))
        try:
            spec = importlib.util.spec_from_file_location('osa_test_browser', workers/'browser/service.py')
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        finally:
            sys.path.remove(str(workers))
        self.db = self.module.BrowserDatabase(str(Path(self.temp.name)/'browser.db'))
        self.browser = self.module.VisibleBrowser(self.db)
        self.browser.base_url = 'https://chat.deepseek.com'
        self.editor = SimpleNamespace(fill=AsyncMock(), press=AsyncMock())
        self.browser.editable = AsyncMock(return_value=self.editor)
        self.browser.challenge = AsyncMock(return_value=False)
        node = SimpleNamespace(count=AsyncMock(return_value=0), is_visible=AsyncMock(return_value=False))
        node.first = node
        self.page = SimpleNamespace(url='https://chat.deepseek.com/a/chat/B', is_closed=lambda: False,
            goto=AsyncMock(), locator=Mock(return_value=node))
        async def navigate(url, **kwargs):
            self.page.url = url
        self.page.goto.side_effect = navigate
        self.browser.page = self.page
        self.browser.chat_selector = ''
        self.browser.search_toggle = ''
        self.browser.send_selector = ''
        self.browser.prompt_count = AsyncMock(side_effect=[0, 1])
        self.browser.editor_value = AsyncMock(return_value='')

    async def asyncTearDown(self):
        self.db.db.close()
        self.temp.cleanup()

    async def test_followup_reopens_requested_conversation_after_another_group(self):
        self.db.save({'id': 'old-a', 'status': 'succeeded', 'session_id': 'session-a',
            'page_url': 'https://chat.deepseek.com/a/chat/A'})
        self.db.save({'id': 'old-b', 'status': 'succeeded', 'session_id': 'session-b',
            'page_url': 'https://chat.deepseek.com/a/chat/B'})
        self.db.save({'id': 'followup', 'session_id': 'session-a', 'status': 'queued',
            'created_at': 1, 'prompt': '继续解释', 'submitted': False})
        await self.browser.tick()
        self.assertEqual(self.page.goto.call_args.args[0], 'https://chat.deepseek.com/a/chat/A')
        self.editor.fill.assert_awaited_once_with('继续解释')
        self.assertTrue(self.db.get('followup')['submitted'])

    async def test_new_conversation_resets_page_without_reusing_last_group_context(self):
        self.db.save({'id': 'new', 'session_id': 'new', 'new_conversation': True,
            'status': 'queued', 'created_at': 1, 'prompt': '新的咨询', 'submitted': False})
        await self.browser.tick()
        self.assertEqual(self.page.goto.call_args.args[0], 'https://chat.deepseek.com')
        self.editor.press.assert_awaited_once_with('Enter')

    async def test_saved_session_cannot_navigate_to_a_different_origin(self):
        self.db.save({'id': 'bad-source', 'status': 'succeeded', 'session_id': 'bad',
            'page_url': 'https://example.test/a/chat/leak'})
        self.db.save({'id': 'bad-next', 'session_id': 'bad', 'status': 'queued',
            'created_at': 1, 'prompt': 'private question', 'submitted': False})
        await self.browser.tick()
        self.page.goto.assert_not_awaited()
        self.editor.fill.assert_not_awaited()
        self.assertEqual(self.db.get('bad-next')['status'], 'failed')

    async def test_expired_submitted_task_releases_queue_without_replaying_it(self):
        self.db.save({'id':'stuck', 'status':'waiting_human', 'submitted':True,
            'created_at':1, 'submitted_at':1, 'prompt':'不要重发'})
        self.db.save({'id':'next', 'status':'queued', 'submitted':False,
            'created_at':time.time(), 'prompt':'新的咨询'})
        await self.browser.tick()
        self.assertEqual(self.db.get('stuck')['error_code'], 'ANSWER_TIMEOUT')
        self.assertTrue(self.db.get('next')['submitted'])
        self.editor.fill.assert_awaited_once_with('新的咨询')

    async def test_submission_is_only_true_after_page_confirmation(self):
        self.db.save({'id':'new', 'status':'queued', 'submitted':False,
            'created_at':time.time(), 'prompt':'新的咨询'})
        async def press(key):
            persisted=self.db.get('new')
            self.assertEqual(persisted['status'],'submitting')
            self.assertFalse(persisted['submitted'])
            self.assertIsNone(persisted.get('submitted_at'))
        self.editor.press.side_effect=press
        await self.browser.tick()
        self.assertTrue(self.db.get('new')['submitted'])

    async def test_unconfirmed_send_fails_and_is_never_automatically_repeated(self):
        self.db.save({'id':'new', 'status':'queued', 'submitted':False,
            'created_at':time.time(), 'prompt':'新的咨询'})
        self.browser.confirm_submission=AsyncMock(return_value=False)
        await self.browser.tick()
        self.assertFalse(self.db.get('new')['submitted'])
        self.assertEqual(self.db.get('new')['error_code'],'SEND_NOT_CONFIRMED')
        await self.browser.tick()
        self.editor.press.assert_awaited_once()

    async def test_crash_after_send_intent_does_not_replay(self):
        self.db.save({'id':'unknown', 'status':'submitting', 'submitted':False,
            'send_attempted_at':time.time(), 'created_at':1, 'prompt':'发送结果不明'})
        await self.browser.tick()
        self.assertEqual(self.db.get('unknown')['error_code'],'SEND_STATE_UNKNOWN')
        self.editor.press.assert_not_awaited()

    async def test_upload_failure_does_not_send_question(self):
        image=Path(self.temp.name)/'test.png'
        image.write_bytes(b'test-image')
        self.db.save({'id':'upload', 'status':'queued', 'submitted':False,
            'created_at':time.time(), 'prompt':'查看附件',
            'attachments':[{'path':str(image),'name':'test.png'}]})
        self.page.locator.return_value.count=AsyncMock(return_value=1)
        self.page.locator.return_value.set_input_files=AsyncMock()
        self.browser.wait_uploads=AsyncMock(return_value=False)
        await self.browser.tick()
        self.editor.press.assert_not_awaited()
        self.assertFalse(self.db.get('upload')['submitted'])

    async def test_large_image_api_retains_original_bytes_for_website_upload(self):
        raw=b'original-image-data'*(1_200_000)  # Above both former 5 MB and 20 MB caps.
        token='browser-original-image-test-token'
        with patch.dict('os.environ',{'OSA_WORKER_TOKEN':token,'BROWSER_UPLOAD_DIR':self.temp.name}), \
             patch.dict(sys.modules,{'osa_test_browser':self.module}), \
             patch.object(self.module,'BrowserDatabase',return_value=self.db):
            app=self.module.create_app()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                headers={'Authorization':'Bearer '+token}) as client:
                response=await client.post('/ask',json={'request_id':'large-image','prompt':'这是谁？',
                    'attachments':[{'name':'原图.png','mime_type':'image/png','data_base64':base64.b64encode(raw).decode()}]})
                self.assertEqual(response.status_code,200)
                attachment=response.json()['attachments'][0]
                self.assertEqual(attachment['size'],len(raw))
                self.assertEqual(Path(attachment['path']).read_bytes(),raw)
                # Generic file handling keeps its existing limit.
                file_response=await client.post('/ask',json={'request_id':'large-file','prompt':'读取文件',
                    'attachments':[{'name':'test.txt','data_base64':base64.b64encode(raw).decode()}]})
                self.assertEqual(file_response.status_code,413)
