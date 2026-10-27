import asyncio
import base64
import copy
import hashlib
import logging
import re
import time
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

class QqbotToolClient:
    def __init__(self, api_url: str | None, *, timeout: float = 600.0,
                 max_bytes: int = 5_000_000, cache_seconds: float = 86400, repo=None) -> None:
        self.api_url = (api_url or "").rstrip("/")
        self.timeout = max(30.0, min(float(timeout), 900.0))
        self.max_bytes = max(1024, min(int(max_bytes), 20_000_000))
        self.cache_seconds = max(0, min(float(cache_seconds), 86400))
        self.repo = repo
        self._catalog = None
        self._catalog_lock = asyncio.Lock()
        self._prompt_lock = asyncio.Lock()
        self._retry_after = 0.0
        self._cache_id = hashlib.sha256(("qqbot-tools:" + self.api_url).encode()).hexdigest()

    @property
    def enabled(self) -> bool:
        return self.api_url.startswith(("http://", "https://"))

    async def cached_catalog(self) -> dict | None:
        if not self.enabled or not self.cache_seconds:
            return None
        cached = self._catalog
        if cached is None and self.repo is not None:
            row = await self.repo.get(self._cache_id)
            if row and row.status == "ready":
                cached = row.payload
                self._catalog = cached
        if not cached:
            return None
        expires_at = min(cached["expires_at"], cached["fetched_at"] + self.cache_seconds)
        if time.time() >= expires_at:
            return None
        return copy.deepcopy({**cached, "expires_at": expires_at})

    async def list_tools(self, *, force_refresh: bool = False) -> list[dict]:
        if not self.enabled:
            raise RuntimeError("qqbot 工具接口未配置")
        async with self._catalog_lock:
            cached = None if force_refresh else await self.cached_catalog()
            if cached is not None:
                return cached["tools"]
            tools = await self._fetch_tools()
            now = time.time()
            self._catalog = {"tools": tools, "fetched_at": now, "expires_at": now + self.cache_seconds}
            if self.repo is not None:
                from osa.domain.features import Record
                saved = await self.repo.get(self._cache_id)
                if saved:
                    await self.repo.patch(self._cache_id, status="ready", payload=self._catalog)
                else:
                    await self.repo.put(Record(self._cache_id, "qqbot_tool_catalog", None, None,
                                               "ready", self._catalog))
            return copy.deepcopy(tools)

    async def refresh_loop(self):
        if not self.enabled or not self.cache_seconds:
            return
        while True:
            cached = await self.catalog_for_prompt()
            if cached is not None:
                await asyncio.sleep(max(1, cached['expires_at'] - time.time()))
                continue
            await asyncio.sleep(max(1, self._retry_after - time.time()))

    async def catalog_for_prompt(self, *, force_refresh: bool = False) -> dict | None:
        if not self.enabled:
            return None
        async with self._prompt_lock:
            cached = None if force_refresh else await self.cached_catalog()
            if cached is not None:
                return cached
            if not force_refresh and time.time() < self._retry_after:
                return None
            try:
                await self.list_tools(force_refresh=force_refresh)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._retry_after = time.time() + 300
                logger.warning('qqbot_catalog_refresh_failed error_type=%s', type(exc).__name__)
                return None
            self._retry_after = 0.0
            return await self.cached_catalog()

    async def _fetch_tools(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(self.api_url + "/api/tools")
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("message") or payload.get("error") or "qqbot 工具目录不可用"))
        tools = payload.get("tools")
        if not isinstance(tools, list) or any(not isinstance(item, dict) or not item.get("tool") for item in tools):
            raise ValueError("qqbot 工具目录格式无效")
        return tools

    async def call(self, tool: str, arguments: dict, *, user_id: str,
                   group_id: str, request_id: str, images: list[dict] | None = None) -> dict:
        if not self.enabled:
            raise RuntimeError("qqbot 工具接口未配置")
        body = {"tool": str(tool), "arguments": arguments, "user_id": str(user_id),
                "group_id": str(group_id), "request_id": str(request_id)}
        if images:
            body["images"] = images
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.api_url + "/api/tools/call", json=body)
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("detail") or payload.get("error") or "qqbot 工具调用失败"))
        return payload

    async def image_bytes(self, value: str) -> tuple[bytes, str]:
        if value.startswith("base64://"):
            raw = base64.b64decode(value[9:], validate=True)
            if len(raw) > self.max_bytes:
                raise ValueError("qqbot 返回图片超过大小限制")
            return raw, "image/png"
        if value.startswith("data:image/") and "," in value:
            header, encoded = value.split(",", 1)
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > self.max_bytes:
                raise ValueError("qqbot 返回图片超过大小限制")
            return raw, header.split(";", 1)[0][5:]
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("qqbot 返回了不支持的图片地址")
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            async with client.stream("GET", value) as response:
                response.raise_for_status()
                mime = response.headers.get("content-type", "image/png").split(";", 1)[0]
                chunks, total = [], 0
                async for chunk in response.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ValueError("qqbot 返回图片超过大小限制")
                    chunks.append(chunk)
        return b"".join(chunks), mime if mime.startswith("image/") else "image/png"

    @staticmethod
    def action_text(actions: list[dict]) -> str:
        values = []
        for action in actions:
            if action.get("type") == "send_message":
                values.append(str(action.get("message") or ""))
            elif action.get("type") == "send_message_with_image":
                values.append(str(action.get("message") or ""))
        return re.sub(r"\s+", " ", " ".join(values)).strip()
