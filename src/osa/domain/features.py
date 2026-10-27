from dataclasses import dataclass, field
from typing import Any, Protocol


class ParserRejected(ValueError):
    """Document input was permanently rejected by the isolated parser."""


class ScreenshotFailed(RuntimeError):
    def __init__(self, code, detail, retryable=False):
        super().__init__(code)
        self.code, self.detail, self.retryable = code, detail, retryable


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    owner_id: str | None
    group_id: str | None
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class FeatureRepository(Protocol):
    async def put(self, record: Record) -> Record: ...
    async def get(self, record_id: str) -> Record | None: ...
    async def find(self, kind: str, *, group_id: str | None = None,
                   owner_id: str | None = None, status: str | None = None,
                   limit: int = 100, before: float | None = None, private_only: bool = False) -> list[Record]: ...
    async def patch(self, record_id: str, *, status: str | None = None,
                    payload: dict[str, Any] | None = None) -> Record: ...
    async def claim(self, kinds: list[str], now: float) -> Record | None: ...
    async def vector_put(self, record_id: str, model: str, vector: list[float]) -> None: ...
    async def vector_scores(self, ids: list[str], model: str, vector: list[float]) -> dict[str, float]: ...
    async def delete(self, record_id: str) -> None: ...


class SecureDownloader(Protocol):
    async def fetch(self, url: str, max_bytes: int | None = None) -> tuple[bytes, str, str]: ...


class BlobStore(Protocol):
    async def put(self, key: str, content: bytes, mime: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class Embedder(Protocol):
    model_id: str
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class BrowserResearch(Protocol):
    async def ask(self, prompt: str, request_id: str, *, session_id: str | None = None,
                  new_conversation: bool = False, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]: ...
    async def status(self, task_id: str) -> dict[str, Any]: ...
    async def cancel(self, task_id: str) -> None: ...


class SearchEngine(Protocol):
    async def search(self, query: str) -> list[dict[str, Any]]: ...


class IsolatedExecutor(Protocol):
    async def run(self, kind: str, arguments: dict[str, Any], request_id: str) -> dict[str, Any]: ...
    async def cancel(self, request_id: str) -> None: ...
