import asyncio
import hashlib
import html
import json
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket

from osa.domain.entities import (
    DeliveryUnknown, Destination, Event, GroupFile, Notice, UpstreamUnavailable, utcnow,
)

logger = logging.getLogger(__name__)


def parse_segments(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, list):
        return [x for x in message if isinstance(x, dict) and isinstance(x.get("data"), dict)]
    if not isinstance(message, str):
        return []
    segments: list[dict[str, Any]] = []
    position = 0
    for match in re.finditer(r"\[CQ:([a-z_]+)((?:,[^\]]*)?)\]", message):
        if match.start() > position:
            segments.append({"type": "text", "data": {"text": html.unescape(message[position:match.start()])}})
        data = {}
        for value in match.group(2).lstrip(",").split(","):
            if "=" in value:
                key, content = value.split("=", 1)
                data[key] = html.unescape(content)
        segments.append({"type": match.group(1), "data": data})
        position = match.end()
    if position < len(message):
        segments.append({"type": "text", "data": {"text": html.unescape(message[position:])}})
    return segments


def normalize(payload: dict[str, Any], bot_id: str, adapter_id: str) -> Event | None:
    if str(payload.get("self_id", "")) != bot_id:
        return None
    post_type = payload.get("post_type")
    notice_type = payload.get("notice_type")
    if post_type == "message" and payload.get("message_type") in {"group", "private"}:
        kind = payload["message_type"]
        event_type = "message.created"
    elif post_type == "notice" and notice_type in {"group_recall", "friend_recall"}:
        kind = "group" if notice_type == "group_recall" else "private"
        event_type = "message.recalled"
    elif post_type == "notice" and notice_type == "group_upload":
        kind, event_type = "group", "group.file_uploaded"
    elif post_type == "notice" and notice_type == "offline_file":
        kind,event_type="private","message.created"
    else:
        return None
    actor = str(payload.get("user_id", ""))
    conversation = str(payload.get("group_id", "")) if kind == "group" else actor
    if not actor.isdecimal() or not conversation.isdecimal() or actor == bot_id:
        return None
    segments = parse_segments(payload.get("message", []))
    # Keep bounded structure so @ targets, quotes and complex-message IDs stay
    # available to the model.  Remote URLs and arbitrary unbounded metadata do
    # not enter retained context; message.view can fetch the source on demand.
    safe_segments = []
    for segment in segments:
        segment_type, data = segment.get("type"), segment["data"]
        keys = {"text": ["text"], "at": ["qq"], "reply": ["id"],
                "forward": ["id", "message_id"], "node": ["id", "message_id", "user_id", "nickname", "name"],
                "image": ["id", "name"], "mface": ["id", "name"],
                "record": ["id", "name"], "video": ["id", "name"],
                "face": ["id"], "file": ["file_id", "id", "name", "size", "busid"],
                "json": ["data", "content", "title", "prompt"],
                "xml": ["data", "content", "title"], "markdown": ["content", "title"]}.get(segment_type, [])
        value = {k: str(data[k])[:4000] for k in keys if k in data}
        safe_segments.append({"type": str(segment_type), "data": value})
    if post_type=='notice' and notice_type in {'group_upload','offline_file'}:
        raw=payload.get('file') or {}
        safe_segments.append({'type':'file','data':{k:str(raw[k]) for k in ['file_id','id','name','size','busid'] if k in raw}})
    message_id = str(payload.get("message_id", ""))
    timestamp = float(payload.get("time", utcnow().timestamp()))
    identity = message_id or hashlib.sha256(json.dumps(
        {"actor": actor, "segments": safe_segments, "file": payload.get("file"), "time": int(timestamp)},
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    dedupe_key = hashlib.sha256(
        f"{adapter_id}|qq_onebot11|{kind}|{conversation}|{event_type}|{identity}".encode()
    ).hexdigest()
    text = "".join(str(s["data"].get("text", "")) for s in safe_segments if s["type"] == "text")
    reply = next((s["data"].get("id") for s in safe_segments if s["type"] == "reply"), None)
    mentioned = any(s["type"] == "at" and s["data"].get("qq") == bot_id for s in safe_segments)
    return Event(
        id=dedupe_key, dedupe_key=dedupe_key, adapter_id=adapter_id, event_type=event_type,
        destination=Destination(kind, conversation), actor_id=actor, message_id=message_id,
        text=text.strip(), segments=safe_segments, occurred_at=timestamp,
        reply_to_id=reply, mentioned=mentioned,
        actor_name=display_name((payload.get('sender') or {}).get('card') or (payload.get('sender') or {}).get('nickname')),
        actor_nickname=display_name((payload.get('sender') or {}).get('nickname')),
        actor_role=str((payload.get('sender') or {}).get('role') or '')[:20],
    )


def display_name(value) -> str:
    if not isinstance(value, str):
        return ''
    # QQ cards can contain combining marks, bidi controls and other invisible
    # formatting characters.  They make model context look like a different
    # person or hide the actual nickname, so retain visible letters/numbers
    # while dropping control, format and standalone combining characters.
    return ''.join(c for c in value
                   if c.isprintable() and unicodedata.category(c) not in {'Cc', 'Cf', 'Cs', 'Co', 'Cn'}
                   and not unicodedata.category(c).startswith('M')).strip()[:80]


class OneBotConnection:
    """One reverse connection, concurrent echo-correlated action futures."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.websocket: WebSocket | None = None
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._group_names: dict[str, str] = {}
        self._group_names_checked = 0.0
        self._group_names_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self.websocket is not None

    async def group_names(self) -> dict[str, str]:
        """Cache QQ display names; an offline adapter must not block console login."""
        if not self.ready:
            return dict(self._group_names)
        async with self._group_names_lock:
            if time.monotonic() - self._group_names_checked >= 60:
                try:
                    raw = await asyncio.wait_for(self.call("get_group_list", {}), timeout=2)
                    if isinstance(raw, list):
                        self._group_names = {
                            str(row["group_id"]): row["group_name"].strip()
                            for row in raw if isinstance(row, dict) and "group_id" in row
                            and isinstance(row.get("group_name"), str) and row["group_name"].strip()
                        }
                except (TimeoutError, UpstreamUnavailable, DeliveryUnknown):
                    pass  # Keep the last known names during temporary adapter failures.
                finally:
                    self._group_names_checked = time.monotonic()
        return dict(self._group_names)

    async def serve(self, websocket: WebSocket, receive: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        if self.websocket is not None:
            await websocket.close(code=1013)
            return
        await websocket.accept()
        self.websocket = websocket
        self._group_names_checked = 0.0
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 256_000:
                    await websocket.close(code=1009)
                    return
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    continue
                echo = str(payload.get("echo", ""))
                if echo in self.pending:
                    future = self.pending[echo]
                    if not future.done():
                        future.set_result(payload)
                elif payload.get("post_type"):
                    # Ingestion only; model processing must not block action responses.
                    await receive(payload)
        finally:
            self.websocket = None
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(DeliveryUnknown("OneBot disconnected during action"))

    async def call(self, action: str, params: dict[str, Any]) -> Any:
        if self.websocket is None:
            raise UpstreamUnavailable("OneBot offline")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            async with self._write_lock:
                if self.websocket is None:
                    raise UpstreamUnavailable("OneBot offline before send")
                try:
                    await self.websocket.send_json({"action": action, "params": params, "echo": echo})
                except Exception as exc:
                    raise DeliveryUnknown("OneBot action write failed") from exc
            try:
                response = await asyncio.wait_for(future, self.timeout)
            except TimeoutError as exc:
                raise DeliveryUnknown("OneBot action timed out") from exc
            if response.get("status") != "ok" or response.get("retcode") != 0:
                logger.warning("onebot_action_rejected action=%s retcode=%s status=%s", action, response.get("retcode"), response.get("status"))
                raise UpstreamUnavailable(f"OneBot rejected action: {response.get('retcode')}")
            return response.get("data") or {}
        finally:
            self.pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume disconnect errors if send itself also failed.

    async def send(self, destination: Destination, text: str, reply_to: str) -> str:
        return await self.send_segments(destination, text, reply_to, [])

    async def send_file(self,destination: Destination,name: str,content: bytes) -> None:
        import base64
        if len(content)>10*1024*1024:
            raise PermissionError("File exceeds delivery limit")
        action="upload_group_file" if destination.kind=="group" else "upload_private_file"
        field="group_id" if destination.kind=="group" else "user_id"
        await self.call(action,{field:int(destination.id),"name":name,"file":"base64://"+base64.b64encode(content).decode('ascii')})

    async def send_segments(self, destination: Destination, text: str, reply_to: str, attachments: list[dict]) -> str:
        message: list[dict[str, Any]] = []
        if reply_to:
            message.append({"type": "reply", "data": {"id": reply_to}})
        if text:
            message.append({"type": "text", "data": {"text": text}})
        for attachment in attachments:
            if attachment['type'] == 'at':
                target = str(attachment.get('qq') or '')
                if destination.kind != 'group' or not re.fullmatch(r'\d{5,20}', target):
                    raise PermissionError('Invalid outgoing mention target')
                message.append({'type': 'at', 'data': {'qq': target}})
                continue
            if attachment["type"] not in {"image", "record"}:
                raise PermissionError("Unsupported outgoing media segment")
            message.append({"type": attachment["type"], "data": {"file": "base64://" + attachment["content"]}})
        action = "send_group_msg" if destination.kind == "group" else "send_private_msg"
        key = "group_id" if destination.kind == "group" else "user_id"
        result = await self.call(action, {key: int(destination.id), "message": message})
        if not isinstance(result, dict) or "message_id" not in result:
            raise DeliveryUnknown("OneBot success without message ID")
        return str(result["message_id"])


class NapCatResources:
    def __init__(self, client: OneBotConnection, max_depth: int = 5, max_files: int = 1000) -> None:
        self.client, self.max_depth, self.max_files = client, max_depth, max_files

    async def notices(self, group_id: str) -> list[Notice]:
        raw = await self.client.call("_get_group_notice", {"group_id": int(group_id)})
        values = raw if isinstance(raw, list) else raw.get("notices", [])
        if not isinstance(values, list):
            raise UpstreamUnavailable("Unsupported notice response schema")
        result = []
        for value in values[:100]:
            message = value.get("message", {})
            body = str(message.get("text", "")) if isinstance(message, dict) else str(message)
            if not body:
                body = str(value.get("content", ""))
            published = value.get("pub_time")
            published_at = float(published) if published is not None else None
            notice_id = str(value.get("notice_id") or value.get("id") or hashlib.sha256(
                f"{group_id}|{body}|{published_at}".encode()).hexdigest())
            result.append(Notice(notice_id, body[:20000], published_at))
        return result

    async def files(self, group_id: str) -> list[GroupFile]:
        result: dict[str, GroupFile] = {}
        seen: set[str] = set()
        queue: list[tuple[str | None, int]] = [(None, 0)]
        while queue:
            folder, depth = queue.pop(0)
            action = "get_group_root_files" if folder is None else "get_group_files_by_folder"
            params: dict[str, Any] = {"group_id": int(group_id)}
            if folder is not None:
                params["folder_id"] = folder
            raw = await self.client.call(action, params)
            if not isinstance(raw, dict):
                raise UpstreamUnavailable("Unsupported file response schema")
            for value in raw.get("files", []):
                file_id = str(value["file_id"])
                result[file_id] = GroupFile(file_id, str(value["file_name"]), int(value.get("file_size", 0)), folder, value.get("busid"))
                if len(result) > self.max_files:
                    raise UpstreamUnavailable("File metadata count exceeded; snapshot not saved")
            for value in raw.get("folders", []):
                folder_id = str(value["folder_id"])
                if folder_id not in seen:
                    if depth >= self.max_depth or len(seen) >= self.max_files:
                        raise UpstreamUnavailable("Folder traversal limit exceeded; snapshot not saved")
                    seen.add(folder_id)
                    queue.append((folder_id, depth + 1))
        return list(result.values())

    async def file_url(self, group_id: str, file_id: str, busid: int = 0) -> str:
        try:
            raw = await self.client.call("get_group_file_url", {
                # Verified against the installed NapCat 4.18.1 handler:
                # it resolves the file UUID from its own in-process cache.
                "group_id": int(group_id), "file_id": str(file_id)
            })
        except UpstreamUnavailable:
            # get_file can return a refreshed URL or a NapCat-local path.
            # Keep the same scoped ID; never guess an ID or search by name
            # outside the authorized group.
            try:
                raw = await self.client.call("get_file", {"file_id": str(file_id)})
            except UpstreamUnavailable:
                raw = await self.client.call("get_file", {"file": str(file_id)})
        url = (raw.get("url") or raw.get("file_url")) if isinstance(raw, dict) else None
        if not url:
            raise UpstreamUnavailable("Group file URL unavailable")
        return str(url)
