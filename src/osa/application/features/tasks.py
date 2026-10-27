import asyncio
import time
import uuid
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from osa.domain.features import FeatureRepository, Record

TERMINAL = {"succeeded", "failed", "cancelled", "rejected"}


class TaskDeferred(Exception):
    """Release the execution slot while an external task waits for a person."""


class TaskService:
    def __init__(self, repo: FeatureRepository) -> None:
        self.repo = repo
        self.handlers: dict[str, Callable[[Record], Awaitable[dict[str, Any]]]] = {}
        self.cancel_callbacks: dict[str, Callable[[Record], Awaitable[None]]] = {}
        self.active: dict[str, asyncio.Task] = {}
        self.active_kinds: dict[str, str] = {}
        self.approval_lock = asyncio.Lock()

    async def submit(self, name: str, payload: dict[str, Any], *, actor: str | None,
                     group: str | None, dedupe: str | None = None, approval: bool = False,
                     priority: int = 0) -> Record:
        row = await self.repo.put(Record(uuid.uuid4().hex, "job." + name, actor, group,
            "awaiting_approval" if approval else "queued", {**payload, "attempts": 0, "priority": priority}, dedupe))
        if approval:
            await self.repo.put(Record(uuid.uuid4().hex, "approval", actor, group, "pending",
                {"job_id": row.id, "operation": name, "parameters": payload}, "approval:" + row.id))
        return row

    async def approve(self, approval_id: str, allowed: bool) -> Record:
        async with self.approval_lock:
            row = await self.repo.get(approval_id)
            if row is None or row.kind != "approval" or row.status != "pending":
                raise ValueError("Approval is not pending")
            job = await self.repo.get(row.payload["job_id"])
            if job is None or job.status != "awaiting_approval":
                raise ValueError("Job no longer awaiting approval")
            decision = await self.repo.patch(approval_id, status="approved" if allowed else "rejected")
            await self.repo.patch(job.id, status="queued" if allowed else "rejected",
                                  payload={**job.payload, "approval_id": approval_id})
            return decision

    async def cancel(self, job_id: str) -> Record:
        row = await self.repo.get(job_id)
        if row is None or not row.kind.startswith("job.") or row.status in TERMINAL:
            raise ValueError("Task does not exist or is terminal")
        await self.repo.patch(job_id, status="cancelled")
        callback = self.cancel_callbacks.get(row.kind)
        if callback:
            try:
                await callback(row)
            except Exception:
                pass  # Local cancellation still revokes delivery if the remote worker is down.
        active = self.active.get(job_id)
        if active:
            active.cancel()
        return await self.repo.get(job_id)

    async def retry(self, job_id: str) -> Record:
        row = await self.repo.get(job_id)
        if not row or row.status not in {"failed", "unknown"} or row.kind not in {"job.index", "job.consolidate", "job.sync", "job.research"}:
            raise ValueError("This task cannot be safely retried")
        return await self.repo.patch(row.id, status="queued", payload={**row.payload, "attempts": 0,
            "scheduled_at": time.time(), "manual_retry_at": time.time()})

    async def execute(self, row: Record) -> None:
        try:
            latest = await self.repo.get(row.id)
            if latest is None or latest.status == "cancelled":
                return
            handler = self.handlers[row.kind]
            result = await handler(row)
            latest = await self.repo.get(row.id)
            if latest and latest.status != "cancelled":
                await self.repo.patch(row.id, status="succeeded", payload={**latest.payload, "result": result, "finished_at": time.time()})
        except TaskDeferred:
            latest = await self.repo.get(row.id)
            if latest and latest.status != "cancelled":
                await self.repo.patch(row.id, status="retry", payload={**latest.payload,
                    "scheduled_at": time.time() + 10})
        except asyncio.CancelledError:
            latest = await self.repo.get(row.id)
            if latest and latest.status != "cancelled":
                await self.repo.patch(row.id, status="retry", payload={**latest.payload, "scheduled_at": time.time() + 5})
            raise
        except Exception as exc:
            latest = await self.repo.get(row.id)
            if latest and latest.status != "cancelled":
                attempts = latest.payload.get("attempts", 0) + 1
                # External writes and sandbox code are never blindly retried.
                repeatable = row.kind in {"job.index", "job.consolidate", "job.sync"}
                state = "retry" if repeatable and attempts < 3 else "failed"
                await self.repo.patch(row.id, status=state, payload={**latest.payload, "error": type(exc).__name__,
                    "attempts": attempts, "scheduled_at": time.time() + min(60, 2**attempts)})
        finally:
            self.active.pop(row.id, None)
            self.active_kinds.pop(row.id, None)

    async def loop(self, concurrency: int = 3) -> None:
        while True:
            try:
                if len(self.active) < concurrency:
                    kinds = [kind for kind in self.handlers if kind != "job.research" or "job.research" not in self.active_kinds.values()]
                    row = await self.repo.claim(kinds, time.time())
                    if row:
                        self.active_kinds[row.id] = row.kind
                        self.active[row.id] = asyncio.create_task(self.execute(row))
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                for task in self.active.values():
                    task.cancel()
                await asyncio.gather(*self.active.values(), return_exceptions=True)
                raise
            except Exception:
                await asyncio.sleep(1)

    async def recover(self) -> None:
        for approval in await self.repo.find("approval", status="approved", limit=10000):
            job = await self.repo.get(approval.payload["job_id"])
            if job and job.status == "awaiting_approval":
                await self.repo.patch(job.id, status="queued", payload={**job.payload, "approval_id": approval.id})
        for kind in self.handlers:
            for row in await self.repo.find(kind, status="running", limit=10000):
                # Persisted remote worker IDs permit polling without repeated external writes.
                recoverable = kind in {"job.index", "job.consolidate", "job.sync"} or row.payload.get("remote_id")
                await self.repo.patch(row.id, status="queued" if recoverable else "unknown")

    async def list(self) -> list[dict]:
        values = []
        for kind in self.handlers:
            values.extend(asdict(row) for row in await self.repo.find(kind))
        return sorted(values, key=lambda row: row["created_at"], reverse=True)[:200]
