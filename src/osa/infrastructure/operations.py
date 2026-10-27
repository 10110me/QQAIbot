"""Durable console operations without duplicating private conversation bodies."""
from __future__ import annotations

import json
import asyncio
import logging
import os
import re
import threading
from collections import deque
from contextlib import ExitStack, closing
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from osa.infrastructure.log_localization import chinese_message, chinese_source


class Allowlist:
    def __init__(self, root: Path, initial: list[str], memory_only: bool = False):
        self.path = Path(root) / "private" / "group-allowlist.json"
        self.memory_only = memory_only
        self.lock = asyncio.Lock()
        self.value = {"revision": 0, "groups": list(initial)}
        if not memory_only and self.path.exists():
            self.value = json.loads(self.path.read_text(encoding="utf-8"))
            self.validate(self.value["groups"])

    @staticmethod
    def validate(groups):
        if not isinstance(groups, list) or len(groups) > 1000 or any(
            not isinstance(g, str) or not re.fullmatch(r"[0-9]{1,20}", g) for g in groups
        ) or len(set(groups)) != len(groups):
            raise ValueError("INVALID_GROUP_ALLOWLIST")

    def save(self, groups: list[str], revision: int):
        self.validate(groups)
        if revision != self.value["revision"]:
            raise ValueError("ALLOWLIST_REVISION_CONFLICT")
        value = {"revision": revision + 1, "groups": groups}
        if not self.memory_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        self.value = value


class ConsoleLogs(logging.Handler):
    """Bounded JSONL rotation. Only selected namespaces, no tracebacks or bodies."""
    def __init__(self, root: Path, secrets: Callable[[], list[str]], memory_only=False):
        super().__init__(logging.INFO)
        self.secrets = secrets
        self.rows = deque(maxlen=2000)
        self.file = None
        self.detail_file = None
        self.details = {}
        self.guard = threading.RLock()
        if not memory_only:
            folder = Path(root) / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            try:
                folder.chmod(0o700)
            except OSError:
                pass
            self.file = RotatingFileHandler(folder / "console.jsonl", maxBytes=2*1024*1024,
                                            backupCount=4, encoding="utf-8")
            self.detail_file = RotatingFileHandler(folder / "model-calls.jsonl",maxBytes=4*1024*1024,
                                                   backupCount=4,encoding="utf-8")
            try:
                (folder / "console.jsonl").chmod(0o600)
            except OSError:
                pass

    def redact(self, text, limit=4000):
        for secret in sorted(set(self.secrets()), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)((?:api[_-]?key|token|password|secret)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+", r"\1[REDACTED]", text)
        text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", text)
        return text[:limit] + ("\n[内容过长，已截断]" if len(text)>limit else "")

    def safe_detail(self, value, depth=0):
        if depth>7:return "[层级过深已省略]"
        if isinstance(value,str):return self.redact(value,16000)
        if isinstance(value,dict):
            return {str(k):"[REDACTED]" if re.fullmatch(r"(?i)(api[_-]?key|authorization|password|secret|token)",str(k))
                    else self.safe_detail(v,depth+1) for k,v in list(value.items())[:60]}
        if isinstance(value,list):return [self.safe_detail(v,depth+1) for v in value[:40]]
        return value

    @staticmethod
    def _reverse_lines(stream, end):
        remaining = b''
        while end:
            size = min(end, 16384)
            end -= size
            stream.seek(end)
            lines = (stream.read(size) + remaining).split(b'\n')
            remaining = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line
        if remaining:
            yield remaining

    def _file_rows(self, handler, limit):
        with ExitStack() as stack:
            streams = []
            with self.guard:
                path = Path(handler.baseFilename)
                for candidate in [path, *[Path(str(path)+f'.{index}') for index in range(1, 5)]]:
                    try:
                        stream = stack.enter_context(candidate.open('rb'))
                    except FileNotFoundError:
                        continue
                    streams.append((stream, os.fstat(stream.fileno()).st_size))
            for stream, end in streams:
                for index, line in enumerate(self._reverse_lines(stream, end)):
                    if index >= limit:
                        break
                    try:
                        yield json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue

    def model_detail(self, call_id):
        with self.guard:
            value = self.details.get(call_id)
        if value is None and self.detail_file:
            with closing(self._file_rows(self.detail_file, 60000)) as rows:
                value = next((row for row in rows if row.get('id') == call_id), None)
        return self.safe_detail(value) if value is not None else None

    def emit(self, record):
        if not record.name.startswith(("osa", "uvicorn.error")):
            return
        try:
            row = {"ts": record.created, "level": record.levelname, "source": record.name,
                   "message": self.redact(record.getMessage())}
            with self.guard:
                detail=getattr(record,'model_call',None)
                if isinstance(detail,dict) and re.fullmatch(r"[a-f0-9]{32}",str(detail.get('id',''))):
                    detail=self.safe_detail(detail)
                    call_id=detail['id']
                    merged={**self.details.get(call_id,{}),**detail}
                    if len(json.dumps(merged,ensure_ascii=False).encode())>160000:
                        merged={**merged,'messages':[{'role':'system','content':'[请求内容超过记录上限，已省略]'}],'truncated':True}
                    self.details[call_id]=merged
                    while len(self.details)>100:self.details.pop(next(iter(self.details)))
                    row['detail_id']=call_id
                    if self.detail_file:
                        self.detail_file.emit(logging.LogRecord('',20,'',0,json.dumps(merged,ensure_ascii=False),(),None))
                self.rows.append(row)
                if self.file:
                    line = logging.LogRecord("", 20, "", 0, json.dumps(row, ensure_ascii=False), (), None)
                    self.file.emit(line)
        except Exception:
            # Logging must never stop message processing.
            pass

    def query(self, level="", source="", q="", before=None, limit=100):
        result = []
        with ExitStack() as stack:
            if self.file:
                rows = stack.enter_context(closing(self._file_rows(self.file, 15000)))
            else:
                with self.guard:
                    rows = list(reversed(self.rows))
            for row in rows:
                if before is not None and row["ts"] >= before:
                    continue
                source_label = chinese_source(row["source"])
                if level and row["level"] != level or source and source.lower() not in (row["source"]+source_label).lower():
                    continue
                message = self.redact(row["message"])
                display_message = chinese_message(message)
                if q and q.lower() not in (message+display_message).lower():
                    continue
                result.append({**row,"message":message,"display_message":display_message,"source_label":source_label})
                if len(result) > limit:
                    break
        return {"items": result[:limit], "next": result[limit-1]["ts"] if len(result)>limit else None,
                "retention": "最多 5 个文件，每个 2 MiB；达到上限自动轮转"}

    def close(self):
        if self.file:
            self.file.close()
        if self.detail_file:
            self.detail_file.close()
        super().close()
