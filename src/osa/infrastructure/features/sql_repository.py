import json
import time
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from osa.domain.features import Record
from osa.infrastructure.features.repository import cosine
from osa.infrastructure.schema import FeatureRow, VectorRow


def dto(row: FeatureRow) -> Record:
    return Record(row.id, row.kind, row.owner_id, row.group_id, row.status, row.payload,
                  row.dedupe_key, row.created_at, row.updated_at)


class SQLFeatureRepository:
    def __init__(self, store: Any) -> None:
        self.store = store

    async def put(self, record: Record) -> Record:
        now = time.time()
        try:
            async with self.store._write_lock, self.store.session.begin() as session:
                existing = await session.get(FeatureRow, record.id)
                if existing:
                    return dto(existing)
                if record.dedupe_key:
                    old = await session.scalar(select(FeatureRow).where(FeatureRow.kind == record.kind, FeatureRow.dedupe_key == record.dedupe_key))
                    if old:
                        return dto(old)
                row = FeatureRow(id=record.id, kind=record.kind, owner_id=record.owner_id,
                                 group_id=record.group_id, status=record.status, payload=record.payload,
                                 dedupe_key=record.dedupe_key, created_at=record.created_at or now, updated_at=now)
                session.add(row)
                await session.flush()
                return dto(row)
        except IntegrityError:
            async with self.store.session() as session:
                row = await session.scalar(select(FeatureRow).where(FeatureRow.kind == record.kind, FeatureRow.dedupe_key == record.dedupe_key))
                if row:
                    return dto(row)
            raise

    async def get(self, record_id: str) -> Record | None:
        async with self.store.session() as session:
            row = await session.get(FeatureRow, record_id)
            return dto(row) if row else None

    async def find(self, kind: str, *, group_id: str | None = None,
                   owner_id: str | None = None, status: str | None = None,
                   limit: int = 100, before: float | None = None, private_only: bool = False) -> list[Record]:
        statement = select(FeatureRow).where(FeatureRow.kind == kind)
        if group_id is not None:
            statement = statement.where(FeatureRow.group_id == group_id)
        if private_only:
            statement = statement.where(FeatureRow.group_id.is_(None))
        if owner_id is not None:
            statement = statement.where(FeatureRow.owner_id == owner_id)
        if status is not None:
            statement = statement.where(FeatureRow.status == status)
        if before is not None:
            statement = statement.where(FeatureRow.created_at < before)
        async with self.store.session() as session:
            rows = (await session.scalars(statement.order_by(FeatureRow.created_at.desc()).limit(limit))).all()
            return [dto(row) for row in rows]

    async def patch(self, record_id: str, *, status: str | None = None,
                    payload: dict | None = None) -> Record:
        async with self.store._write_lock, self.store.session.begin() as session:
            row = await session.get(FeatureRow, record_id)
            if row is None:
                raise KeyError(record_id)
            if status:
                row.status = status
            if payload is not None:
                row.payload = payload
            row.updated_at = time.time()
            return dto(row)

    async def claim(self, kinds: list[str], now: float) -> Record | None:
        async with self.store._write_lock, self.store.session.begin() as session:
            rows = (await session.scalars(select(FeatureRow).where(FeatureRow.kind.in_(kinds), FeatureRow.status.in_(["queued", "retry"]))
                    .order_by(FeatureRow.created_at).limit(100).with_for_update(skip_locked=True))).all()
            candidates = [row for row in rows if row.payload.get("scheduled_at", 0) <= now]
            if not candidates:
                return None
            row = max(candidates, key=lambda r: (r.payload.get("priority", 0), -r.created_at))
            row.status, row.updated_at = "running", now
            return dto(row)

    async def vector_put(self, record_id: str, model: str, vector: list[float]) -> None:
        async with self.store._write_lock, self.store.session.begin() as session:
            if self.store.engine.dialect.name == "postgresql":
                await session.execute(text("""INSERT INTO feature_vectors_pg (record_id, model_id, dimension, embedding)
                    VALUES (:id, :model, :dimension, CAST(:embedding AS vector))
                    ON CONFLICT(record_id, model_id) DO UPDATE SET embedding=EXCLUDED.embedding, dimension=EXCLUDED.dimension"""),
                    {"id": record_id, "model": model, "dimension": len(vector), "embedding": json.dumps(vector)})
            else:
                row = await session.get(VectorRow, (record_id, model))
                if row:
                    row.vector, row.dimension = vector, len(vector)
                else:
                    session.add(VectorRow(record_id=record_id, model_id=model, dimension=len(vector), vector=vector))

    async def vector_scores(self, ids: list[str], model: str, vector: list[float]) -> dict[str, float]:
        if not ids:
            return {}
        async with self.store.session() as session:
            if self.store.engine.dialect.name == "postgresql":
                result = await session.execute(text("""SELECT record_id, 1 - (embedding <=> CAST(:embedding AS vector)) AS score
                    FROM feature_vectors_pg WHERE record_id = ANY(CAST(:ids AS text[]))
                    AND model_id=:model AND dimension=:dimension"""),
                    {"embedding": json.dumps(vector), "ids": ids, "model": model, "dimension": len(vector)})
                return {row.record_id: float(row.score) for row in result}
            # SQLite keeps the same scores without hydrating every JSON vector at once.
            scores = {}
            for start in range(0, len(ids), 64):
                rows = (await session.scalars(select(VectorRow).where(
                    VectorRow.record_id.in_(ids[start:start+64]), VectorRow.model_id == model,
                    VectorRow.dimension == len(vector)))).all()
                scores.update({row.record_id: cosine(row.vector, vector) for row in rows})
            return scores

    async def delete(self, record_id: str) -> None:
        async with self.store._write_lock, self.store.session.begin() as session:
            row = await session.get(FeatureRow, record_id)
            if row:
                await session.delete(row)
