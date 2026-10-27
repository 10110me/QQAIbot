"""V2 contracts: no dependency on a database, model SDK or platform adapter."""
import re
import base64
from pathlib import PurePosixPath
from typing import Protocol

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "DENIED"}
BUSY = {"QUEUED", "RUNNING", "WAITING_INPUT", "PAUSING", "PAUSED", "VALIDATING", "INTERRUPTED", "CANCEL_REQUESTED"}
COMMANDS = {"APPROVE", "REJECT", "ADD_REQUIREMENT", "PAUSE", "RESUME", "CANCEL"}


class VersionConflict(ValueError):
    pass


class StaleLease(PermissionError):
    pass


class WorkModelUnavailable(RuntimeError):
    """Execution infrastructure failed; additional user requirements won't fix it."""
    pass


def clean_text(text: str, limit: int = 12000) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(text))
    text = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|password|token|secret)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[a-zA-Z0-9_-]{12,}\b", "[REDACTED]", text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)[:limit]


def delivery_filename(source: str, requested: str | None = None) -> str:
    """Rename a verified file for QQ without choosing a path or file type."""
    name = requested if requested is not None else source
    if (not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 160
            or name.startswith('.') or any(c in name for c in '/\\:<>"|?*')
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError('发送文件名无效，请使用不含路径的简短文件名')
    if PurePosixPath(name).suffix.lower() != PurePosixPath(source).suffix.lower():
        raise ValueError('重命名不能改变已生成文件的扩展名')
    return name


def external_question(text: str) -> str:
    """Fail closed; automatic research accepts a generic technical question only."""
    if clean_text(text) != text or re.search(r"(?i)-----BEGIN .*PRIVATE KEY|https?://|(?:\d{1,3}\.){3}\d{1,3}|\b\d{5,}\b|(?:/home/|/etc/|/workspace/|[A-Z]:\\)|QQ群|群号|qq[:：]|@", text):
        raise PermissionError("外发内容含身份、凭据、路径或 URL，需去身份化并重新审核")
    if any(word in text for word in ["我的朋友", "个人经历", "家庭住址", "手机号", "身份证"]):
        raise PermissionError("私人经历不可用于外部事实研究")
    return text[:3000]


def validate_files(files: dict[str, str]) -> dict[str, str]:
    if not files or len(files) > 80 or sum(len(v.encode()) for v in files.values()) > 500_000:
        raise ValueError("Project bundle exceeds limits")
    for name, content in files.items():
        path = PurePosixPath(name)
        if (not name or len(name) > 200 or path.is_absolute() or "\\" in name or ":" in name
                or any(p in {"..", ".git", ".env", "__pycache__"} or p.startswith(".") for p in path.parts)
                or str(path) != name or path.suffix not in {".py", ".html", ".js", ".css", ".json", ".md", ".txt"}):
            raise PermissionError("Unsafe project filename")
        if not isinstance(content, str) or len(content.encode()) > 100_000:
            raise ValueError("Project file exceeds limit")
    return files


def decode_input_files(inputs: dict[str, str]) -> dict[str, bytes]:
    if len(inputs) > 20:
        raise ValueError('Too many task inputs')
    total, result = 0, {}
    for name, encoded in inputs.items():
        path = PurePosixPath(name)
        if not name or len(name) > 200 or len(path.parts) != 1 or path.name != name or name.startswith('.') or '\\' in name or ':' in name:
            raise PermissionError('Unsafe input filename')
        if len(encoded) > 14_000_000:
            raise ValueError('Task input exceeds limit')
        raw = base64.b64decode(encoded, validate=True)
        total += len(raw)
        if total > 10 * 1024 * 1024:
            raise ValueError('Task inputs exceed 10 MiB')
        result[name] = raw
    return result


class TaskProgressProvider(Protocol):
    async def snapshot(self, task_id: str, actor: str, group: str | None) -> dict: ...


class EchoStateProvider(Protocol):
    async def echo_active(self, group: str) -> bool: ...


class ClaimVerifier(Protocol):
    async def verify(self, claim: str, source_refs: list[str]) -> dict: ...


def problem_fingerprint(text: str, environment: dict | None = None) -> dict:
    """Keep exact error classes; similar words do not imply the same root cause."""
    errors=re.findall(r'\b([A-Z][A-Za-z]+(?:Error|Exception))\b',text)
    module=re.search(r"No module named ['\"]([A-Za-z0-9_.]+)['\"]",text)
    signature=re.sub(r'File "[^"]+", line \d+','File [path], line [n]',clean_text(text,2000))
    signature=re.sub(r'0x[0-9a-fA-F]+','[address]',signature)
    return {'error_code_exact':errors[-1] if errors else None,'component':module.group(1) if module else None,
            'normalized_error_signature':signature[-800:],'environment':environment or {},'root_cause':'unconfirmed'}
