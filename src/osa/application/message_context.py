"""Safe rendering and bounded expansion of OneBot messages.

The adapter deliberately stores only bounded, non-credential message metadata.
Complex messages stay as a preview until the thinking agent explicitly asks to
view a message in the current conversation.
"""
from __future__ import annotations

import json
import html
import re
from datetime import datetime, timezone
from typing import Any

from osa.domain.entities import Event
from osa.domain.message import at_targets


def _clean(value: Any, limit: int = 1200) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            value = str(value)
    return " ".join(str(value).split())[:limit]


def mentions_other_user(event: Event, bot_id: str | None = None) -> bool:
    """True when a group message explicitly addresses someone other than bot."""
    targets = at_targets(event)
    if not targets:
        return False
    if bot_id and bot_id in targets:
        return False
    return True


def card_preview(data: dict[str, Any]) -> str:
    raw = data.get('data') or data.get('content') or data
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return '复杂卡片，可调用 message.view 展开'
    if not isinstance(raw, dict):
        return '复杂卡片，可调用 message.view 展开'
    texts = []
    candidates = [raw]
    meta = raw.get('meta')
    if isinstance(meta, dict):
        candidates.extend(value for value in meta.values() if isinstance(value, dict))
    for candidate in candidates:
        for key in ('prompt', 'title', 'desc', 'summary', 'brief'):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip() and value not in texts:
                texts.append(value)
    return _clean(' / '.join(texts), 300) or '复杂卡片，可调用 message.view 展开'


def render_segments(segments: list[dict[str, Any]] | None, *, bot_id: str | None = None,
                    max_chars: int = 4000) -> str:
    """Render segments for a model without preserving remote URLs or secrets."""
    parts: list[str] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get("type", ""))
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "at":
            target = str(data.get("qq", "")).strip()
            parts.append("@机器人" if bot_id and target == bot_id else f"@{target or '未知用户'}")
        elif kind == "reply":
            parts.append(f"[引用消息:{_clean(data.get('id'), 100)}]")
        elif kind in {"forward", "node"}:
            ident = data.get("id") or data.get("message_id")
            label = "合并转发" if kind == "forward" else "转发节点"
            parts.append(f"[{label}{(': ' + _clean(ident, 100)) if ident else ''}，可调用 message.view 展开]")
        elif kind in {"image", "mface"}:
            parts.append("[图片/表情]")
        elif kind == "file":
            parts.append(f"[文件:{_clean(data.get('name') or data.get('file'), 160) or '未命名'}]")
        elif kind in {"record", "video", "audio"}:
            parts.append(f"[{kind}]")
        elif kind == "face":
            parts.append(f"[QQ表情:{_clean(data.get('id'), 40)}]")
        elif kind == 'json':
            parts.append(f"[分享卡片:{card_preview(data)}]")
        elif kind in {"xml", "markdown", "contact", "share", "location"}:
            payload = data.get("data") or data.get("content") or data.get("title") or data.get("prompt")
            parts.append(f"[{kind}:{_clean(payload, 500) or '复杂消息'}]")
        elif kind:
            parts.append(f"[{kind}]")
    return _clean("".join(parts), max_chars)


def render_event(event: Event | dict[str, Any], *, bot_id: str | None = None,
                 include_id: bool = False, max_chars: int = 4000) -> str:
    actor = event.actor_name if isinstance(event, Event) else event.get("actor_name", "")
    actor_id = event.actor_id if isinstance(event, Event) else event.get("actor_id", "")
    actor_role = event.actor_role if isinstance(event, Event) else event.get("actor_role", "")
    message_id = event.message_id if isinstance(event, Event) else event.get("message_id", "")
    occurred_at = event.occurred_at if isinstance(event, Event) else event.get("occurred_at")
    reply_id = event.reply_to_id if isinstance(event, Event) else event.get("reply_to_id")
    segments = event.segments if isinstance(event, Event) else event.get("segments", [])
    body = render_segments(segments, bot_id=bot_id, max_chars=max_chars)
    if not body and isinstance(event, Event):
        body = _clean(event.text, max_chars)
    prefix = f"[消息:{message_id}] " if include_id and message_id else ""
    role = {"owner": "群主", "admin": "群管理员", "member": "群成员"}.get(actor_role)
    who = f"{actor or '未知用户'}(QQ:{actor_id or '未知'}{',发言时' + role if role else ''})"
    try:
        from zoneinfo import ZoneInfo
        when = datetime.fromtimestamp(float(occurred_at), timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OverflowError):
        when = '时间未知'
    quoted = f" [引用:{reply_id}]" if reply_id else ""
    return f"{prefix}[时间:{when}] {who}{quoted}: {body}".strip()


def render_raw_message(raw: dict[str, Any], *, bot_id: str | None = None,
                       include_id: bool = True) -> str:
    """Render a get_msg/get_forward_msg item without retaining its raw payload."""
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    actor = str(sender.get("card") or sender.get("nickname") or "未知用户")[:80]
    actor_id = str(raw.get("user_id") or sender.get("user_id") or "")
    segments = _parse_segments(raw.get("message", raw.get("content", [])))
    event = {"actor_name": actor, "actor_id": actor_id, "message_id": str(raw.get("message_id") or ""),
             "occurred_at": raw.get("time"),
             "reply_to_id": None, "segments": segments}
    return render_event(event, bot_id=bot_id, include_id=include_id)


def _parse_segments(message: Any) -> list[dict[str, Any]]:
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


def raw_message_destination(raw: dict[str, Any]) -> str:
    return str(raw.get("group_id") or raw.get("discuss_id") or raw.get("user_id") or "")

