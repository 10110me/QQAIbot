import httpx

from osa.domain.features import ScreenshotFailed


class SandboxClient:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip('/'), token

    async def run(self, kind: str, arguments: dict, request_id: str) -> dict:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(self.url + '/run', headers={'Authorization': 'Bearer ' + self.token},
                json={'kind': kind, 'request_id': request_id, **arguments})
            response.raise_for_status()
            return response.json()

    async def cancel(self, request_id: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(self.url + '/tasks/' + request_id + '/cancel',
                headers={'Authorization': 'Bearer ' + self.token})
            response.raise_for_status()


class ScreenshotClient:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip('/'), token

    async def screenshot(self, url: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=240) as client:
                response = await client.post(self.url + '/screenshot', headers={'Authorization': 'Bearer ' + self.token}, json={'url': url})
        except httpx.TimeoutException as exc:
            raise ScreenshotFailed('SCREENSHOT_TIMEOUT', '截图服务响应超时，尚未取得截图。', True) from exc
        except httpx.RequestError as exc:
            raise ScreenshotFailed('SCREENSHOT_UNAVAILABLE', '无法连接截图服务，尚未取得截图。', True) from exc
        errors = {
            'SCREENSHOT_TIMEOUT': ('网页在生成截图时超时，尚未取得图片。', True),
            'SCREENSHOT_NAVIGATION_FAILED': ('网页未能加载，尚未生成截图。', True),
            'SCREENSHOT_WORKER_ERROR': ('截图工作服务内部错误，尚未取得图片。', True),
        }
        if response.is_error:
            code = {502: 'SCREENSHOT_NAVIGATION_FAILED', 504: 'SCREENSHOT_TIMEOUT'}.get(response.status_code, 'SCREENSHOT_WORKER_ERROR')
            try:
                detail = response.json().get('detail')
                if isinstance(detail, dict) and detail.get('code') in errors:
                    code = detail['code']
            except (ValueError, AttributeError):
                pass
            message, retryable = errors[code]
            raise ScreenshotFailed(code, message, retryable)
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError('Invalid screenshot response')
            return result
        except ValueError as exc:
            raise ScreenshotFailed('SCREENSHOT_INVALID_RESPONSE', '截图服务返回了无效数据，尚未取得图片。') from exc

    async def browse(self, url: str, actions: list[dict]) -> dict:
        async with httpx.AsyncClient(timeout=240) as client:
            response = await client.post(self.url + '/browse', headers={'Authorization': 'Bearer ' + self.token},
                                        json={'url': url, 'actions': actions})
            response.raise_for_status()
            return response.json()
