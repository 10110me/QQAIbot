"""Owner-authenticated mapping of the live browser desktop and DeepSeek tasks."""
import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket

from osa.interfaces.api import require_admin, response

router = APIRouter()
logger = logging.getLogger(__name__)


def signature(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def authorized_cookie(secret: str, cookie: str) -> bool:
    try:
        expires, nonce, digest = cookie.split(".")
        payload = expires + "." + nonce
        return int(expires) > time.time() and hmac.compare_digest(signature(secret, payload), digest)
    except (ValueError, TypeError):
        return False


def active_cookie(app, cookie: str) -> bool:
    sessions = getattr(app.state, "browser_sessions", {})
    return authorized_cookie(app.state.settings.admin_token.get_secret_value(), cookie) and sessions.get(cookie, 0) > time.time()


@router.post("/api/v1/browser/session", dependencies=[Depends(require_admin)])
async def browser_session(request: Request, result: Response) -> dict:
    settings = request.app.state.settings
    if not settings.deepseek_web_enabled:
        raise HTTPException(409, "DEEPSEEK_BROWSER_DISABLED")
    payload = str(int(time.time()) + 3600) + "." + secrets.token_hex(16)
    value = payload + "." + signature(settings.admin_token.get_secret_value(), payload)
    if not hasattr(request.app.state, "browser_sessions"):
        request.app.state.browser_sessions = {}
    request.app.state.browser_sessions[value] = time.time()+3600
    result.set_cookie("osa_browser", value, httponly=True, secure=settings.browser_cookie_secure,
                      samesite="strict", max_age=3600, path="/browser")
    result.headers["Cache-Control"] = "no-store"
    # noVNC resolves this path relative to /browser/vnc.html. Including the
    # prefix again would incorrectly connect to /browser/browser/websockify.
    return response({"url": "/browser/vnc.html?autoconnect=true&resize=scale&path=websockify",
                     "expires_in": 3600})


@router.delete("/api/v1/browser/session", dependencies=[Depends(require_admin)])
async def close_browser_session(request: Request, result: Response) -> dict:
    if hasattr(request.app.state, "browser_sessions"):
        request.app.state.browser_sessions.clear()
    result.delete_cookie("osa_browser", path="/browser")
    return response({"revoked": True})


@router.get("/api/v1/browser/status", dependencies=[Depends(require_admin)])
async def browser_status(request: Request) -> dict:
    features = request.app.state.features
    if not features or not request.app.state.settings.deepseek_web_enabled:
        return response({"state": "disabled"})
    return response(await features.research.browser.request("GET", "/health"))


@router.post("/api/v1/browser/open-login", dependencies=[Depends(require_admin)])
async def open_login(request: Request) -> dict:
    if not request.app.state.settings.deepseek_web_enabled:
        raise HTTPException(409, "DEEPSEEK_BROWSER_DISABLED")
    return response(await request.app.state.features.research.browser.request("POST", "/open-login"))


@router.get("/browser/{path:path}")
async def browser_proxy(path: str, request: Request) -> Response:
    settings = request.app.state.settings
    if not settings.deepseek_web_enabled or not active_cookie(request.app, request.cookies.get("osa_browser", "")):
        raise HTTPException(401, "BROWSER_SESSION_REQUIRED")
    if ".." in path.split("/") or path.startswith(("/", "\\")) or ":" in path:
        raise HTTPException(400, "INVALID_BROWSER_RESOURCE")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        upstream = await client.get(settings.browser_vnc_url.rstrip("/") + "/" + path, params=request.query_params)
    return Response(upstream.content, status_code=upstream.status_code,
                    headers={"Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Referrer-Policy": "same-origin"})


@router.websocket("/browser/websockify")
async def browser_socket(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    origin = websocket.headers.get("origin", "")
    if not settings.deepseek_web_enabled or not active_cookie(websocket.app, websocket.cookies.get("osa_browser", "")) or urlsplit(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=1008)
        return
    from websockets.asyncio.client import connect
    target = settings.browser_vnc_url.rstrip("/").replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/websockify"
    await websocket.accept(subprotocol="binary" if "binary" in websocket.scope.get("subprotocols", []) else None)
    async with connect(target, subprotocols=["binary"], max_size=16*1024*1024) as upstream:
        async def to_worker() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
                elif message.get("text") is not None:
                    await upstream.send(message["text"])

        async def to_user() -> None:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        async def session_watch() -> None:
            refreshed = 0.0
            while active_cookie(websocket.app, websocket.cookies.get("osa_browser", "")):
                if time.monotonic() - refreshed >= 15:
                    await websocket.app.state.features.research.browser.request("POST", "/view-lease")
                    refreshed = time.monotonic()
                await asyncio.sleep(2)
        pipes = [asyncio.create_task(to_worker()), asyncio.create_task(to_user()), asyncio.create_task(session_watch())]
        try:
            completed, _ = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                if not task.cancelled() and task.exception() is not None:
                    exc = task.exception()
                    logger.warning("browser_relay_failed pipe=%s error=%s", pipes.index(task),
                                   type(exc).__name__)
        finally:
            for task in pipes:
                task.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
            try:
                await websocket.close()
            except RuntimeError:
                pass
