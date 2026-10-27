"""Separate browser worker with no access to the DeepSeek profile."""
import asyncio
import base64
import os
import secrets
import logging

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from resource_slot import HeavySlot

from osa.infrastructure.features.downloader import PublicDownloader, validate_url

logger = logging.getLogger(__name__)


class ScreenshotInput(BaseModel):
    url: str = Field(max_length=2000)


class BrowseAction(BaseModel):
    type: str = Field(pattern=r'^(click|fill|press|read)$')
    selector: str = Field(min_length=1, max_length=500)
    value: str = Field(default='', max_length=4000)


class BrowseInput(ScreenshotInput):
    actions: list[BrowseAction] = Field(default_factory=list, max_length=10)


async def capture_image(page):
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    height = await page.evaluate('Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)')
    clipped = height > 15000
    if clipped:
        await page.set_viewport_size({'width': 1280, 'height': 15000})
    warning = ''
    try:
        raw = await page.screenshot(type='png', full_page=not clipped,
                                    animations='disabled', caret='hide', timeout=20000)
    except PlaywrightTimeout:
        logger.warning('screenshot_capture_fallback reason=full_page_timeout')
        await page.set_viewport_size({'width': 1280, 'height': 900})
        try:
            raw = await page.screenshot(type='png', full_page=False,
                                        animations='disabled', caret='hide', timeout=12000)
        except PlaywrightTimeout as exc:
            raise HTTPException(504, {'code': 'SCREENSHOT_TIMEOUT', 'stage': 'capture',
                                     'message': '整页与可见区域截图均超时，未生成图片'}) from exc
        clipped = True
        warning = '整页截图超时，已返回当前可见区域，未包含整页内容。'
    return {'mime': 'image/png', 'content': base64.b64encode(raw).decode(),
            'full_page': not clipped, 'truncated': clipped, 'capture_warning': warning}


def create_app() -> FastAPI:
    token = os.environ.get("OSA_WORKER_TOKEN", "")
    if len(token) < 24:
        raise RuntimeError("Worker token required")
    lock = asyncio.Lock()

    async def auth(authorization: str = Header(default="")) -> None:
        if not secrets.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "Unauthorized")

    app = FastAPI(dependencies=[Depends(auth)])

    @app.post("/screenshot")
    async def screenshot(payload: ScreenshotInput) -> dict:
        validate_url(payload.url)
        from playwright.async_api import async_playwright
        async with lock, HeavySlot(), async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--disable-dev-shm-usage", "--renderer-process-limit=2"])
            try:
                return await capture(browser, payload.url)
            finally:
                await browser.close()

    @app.post('/browse')
    async def browse(payload: BrowseInput) -> dict:
        validate_url(payload.url)
        from playwright.async_api import async_playwright
        async with lock, HeavySlot(), async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=['--disable-dev-shm-usage', '--renderer-process-limit=2'])
            try:
                return await capture(browser, payload.url, payload.actions)
            finally:
                await browser.close()

    async def capture(browser, url: str, actions=None) -> dict:
        context = await browser.new_context(viewport={"width": 1280, "height": 900},
            accept_downloads=False, service_workers="block")
        downloader = PublicDownloader(5_000_000)
        requests = 0
        main_status = None

        async def route(request):
            nonlocal requests, main_status
            requests += 1
            if requests > 60 or request.request.method != "GET":
                await request.abort()
                return
            try:
                content, mime, _, status = await downloader.fetch_response(request.request.url,
                    render_http_errors=True, browser_headers=request.request.headers)
                if request.request.is_navigation_request() and request.request.frame == page.main_frame:
                    main_status = status
                await request.fulfill(status=status, body=content, content_type=mime)
            except Exception as exc:
                logger.warning('网页资源读取失败 type=%s', type(exc).__name__)
                await request.abort()

        async def block_socket(socket):
            await socket.close()
        await context.route("**/*", route)
        await context.route_web_socket("**/*", block_socket)
        try:
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            except Exception as exc:
                logger.warning('网页截图导航失败 type=%s', type(exc).__name__)
                raise HTTPException(502, {'code': 'SCREENSHOT_NAVIGATION_FAILED', 'stage': 'navigation',
                                         'message': '网页未能加载，尚未生成截图'}) from exc
            if actions is not None:
                observations = []
                for action in actions:
                    target = page.locator(action.selector).first
                    if action.type == 'click':
                        await target.click(timeout=5000)
                    elif action.type == 'fill':
                        await target.fill(action.value, timeout=5000)
                    elif action.type == 'press':
                        await target.press(action.value, timeout=5000)
                    else:
                        observations.append((await target.inner_text(timeout=5000))[:8000])
                return {'url': page.url, 'title': await page.title(),
                        'text': (await page.locator('body').inner_text(timeout=5000))[:16000],
                        'observations': observations, 'actions_completed': len(actions)}
            from playwright.async_api import TimeoutError as PlaywrightTimeout
            try:
                await page.wait_for_load_state('networkidle', timeout=5000)
            except PlaywrightTimeout:
                pass  # Live status pages may poll continuously.
            await asyncio.sleep(1)
            image = await capture_image(page)
            return {**image, 'url':page.url, 'title':await page.title(), 'http_status':main_status}
        finally:
            await context.close()
    return app
