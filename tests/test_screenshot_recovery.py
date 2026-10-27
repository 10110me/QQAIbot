import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from playwright.async_api import TimeoutError as PlaywrightTimeout

from osa.domain.features import ScreenshotFailed
from osa.infrastructure.features.executor import ScreenshotClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workers'))
from workers.browser.screenshot import capture_image


class ScreenshotRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def page(self, height=2000):
        return SimpleNamespace(evaluate=AsyncMock(return_value=height),
            screenshot=AsyncMock(return_value=b'test-png'), set_viewport_size=AsyncMock())

    async def test_successful_capture_disables_animation_without_changing_full_page_behavior(self):
        page = self.page()
        result = await capture_image(page)
        self.assertEqual(base64.b64decode(result['content']), b'test-png')
        self.assertTrue(result['full_page'])
        self.assertFalse(result['truncated'])
        self.assertEqual(page.screenshot.await_args.kwargs['animations'], 'disabled')
        self.assertEqual(page.screenshot.await_args.kwargs['timeout'], 20000)
        page.set_viewport_size.assert_not_awaited()

    async def test_timeout_recovers_with_a_labeled_viewport_image(self):
        page = self.page(20000)
        page.screenshot.side_effect = [PlaywrightTimeout('capture timed out'), b'viewport-png']
        result = await capture_image(page)
        self.assertEqual(base64.b64decode(result['content']), b'viewport-png')
        self.assertFalse(result['full_page'])
        self.assertTrue(result['truncated'])
        self.assertIn('当前可见区域', result['capture_warning'])
        self.assertEqual(page.set_viewport_size.await_args.args[0], {'width': 1280, 'height': 900})
        self.assertEqual(page.screenshot.await_args.kwargs['timeout'], 12000)

    async def test_both_capture_attempts_timing_out_have_a_structured_worker_error(self):
        page = self.page()
        page.screenshot.side_effect = PlaywrightTimeout('capture timed out')
        with self.assertRaises(HTTPException) as raised:
            await capture_image(page)
        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(raised.exception.detail['code'], 'SCREENSHOT_TIMEOUT')
        self.assertEqual(raised.exception.detail['stage'], 'capture')
        self.assertEqual(page.screenshot.await_count, 2)

    async def test_worker_failure_is_translated_without_exposing_its_raw_body(self):
        def handler(request):
            self.assertEqual(json.loads(request.content)['url'], 'https://example.com/')
            return httpx.Response(504, json={'detail': {'code': 'SCREENSHOT_TIMEOUT',
                'message': 'unsafe upstream text worker-secret-value'}})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        with patch('osa.infrastructure.features.executor.httpx.AsyncClient', return_value=client):
            with self.assertRaises(ScreenshotFailed) as raised:
                await ScreenshotClient('http://worker', 'internal-worker-key').screenshot('https://example.com/')
        self.assertEqual(raised.exception.code, 'SCREENSHOT_TIMEOUT')
        self.assertIn('生成截图时超时', raised.exception.detail)
        self.assertNotIn('worker-secret-value', raised.exception.detail)
        self.assertTrue(raised.exception.retryable)

    async def test_connect_timeout_is_distinguished_from_a_capture_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout('request timed out', request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('osa.infrastructure.features.executor.httpx.AsyncClient', return_value=client):
            with self.assertRaises(ScreenshotFailed) as raised:
                await ScreenshotClient('http://worker', 'internal-worker-key').screenshot('https://example.com/')
        self.assertIn('服务响应超时', raised.exception.detail)
