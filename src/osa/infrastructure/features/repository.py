import asyncio
import math
import time
from dataclasses import replace

from osa.domain.features import Record


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("Embedding dimension mismatch")
    norm = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    return sum(x*y for x, y in zip(left, right, strict=True)) / norm if norm else 0.0


class MemoryFeatureRepository:
    def __init__(self) -> None:
        self.rows: dict[str, Record] = {}
        self.vectors: dict[tuple[str, str], list[float]] = {}
        self.lock = asyncio.Lock()

    async def put(self, record: Record) -> Record:
        async with self.lock:
            if record.dedupe_key:
                for row in self.rows.values():
                    if row.kind == record.kind and row.dedupe_key == record.dedupe_key:
                        return row
            now = time.time()
            saved = replace(record, created_at=record.created_at or now, updated_at=now)
            self.rows.setdefault(record.id, saved)
            return self.rows[record.id]

    async def get(self, record_id: str) -> Record | None:
        return self.rows.get(record_id)

    async def find(self, kind: str, *, group_id: str | None = None,
                   owner_id: str | None = None, status: str | None = None,
                   limit: int = 100, before: float | None = None, private_only: bool = False) -> list[Record]:
        rows = [r for r in self.rows.values() if r.kind == kind
                and (group_id is None or r.group_id == group_id)
                and (owner_id is None or r.owner_id == owner_id)
                and (status is None or r.status == status)
                and (before is None or r.created_at < before)]
        if private_only:
            rows = [r for r in rows if r.group_id is None]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]

    async def patch(self, record_id: str, *, status: str | None = None,
                    payload: dict | None = None) -> Record:
        async with self.lock:
            row = self.rows[record_id]
            row = replace(row, status=status or row.status,
                          payload=payload if payload is not None else row.payload, updated_at=time.time())
            self.rows[record_id] = row
            return row

    async def claim(self, kinds: list[str], now: float) -> Record | None:
        async with self.lock:
            candidates = [r for r in self.rows.values() if r.kind in kinds and r.status in {"queued", "retry"}
                          and r.payload.get("scheduled_at", 0) <= now]
            if not candidates:
                return None
            row = min(candidates, key=lambda r: (-r.payload.get("priority", 0), r.created_at))
            row = replace(row, status="running", updated_at=now)
            self.rows[row.id] = row
            return row

    async def vector_put(self, record_id: str, model: str, vector: list[float]) -> None:
        self.vectors[(record_id, model)] = list(vector)

    async def vector_scores(self, ids: list[str], model: str, vector: list[float]) -> dict[str, float]:
        return {rid: cosine(self.vectors[(rid, model)], vector) for rid in ids if (rid, model) in self.vectors}

    async def delete(self, record_id: str) -> None:
        self.rows.pop(record_id, None)
        for key in list(self.vectors):
            if key[0] == record_id:
                del self.vectors[key]
