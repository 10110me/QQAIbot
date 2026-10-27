import asyncio
import json
import time
from dataclasses import asdict

from osa.domain.features import BrowserResearch, FeatureRepository, Record, SearchEngine
from osa.domain.ports import Store
from osa.domain.work import external_question
from osa.application.features.tasks import TaskDeferred


class ResearchService:
    """Every research task passes through the visible DeepSeek browser."""

    def __init__(self, repo: FeatureRepository, store: Store, browser: BrowserResearch,
                 search: SearchEngine | None, daily_limit: int, defer_human_wait: bool = False) -> None:
        self.repo, self.store, self.browser, self.search = repo, store, browser, search
        self.daily_limit = daily_limit
        self.submit_lock = asyncio.Lock()
        self.defer_human_wait = defer_human_wait

    async def run(self, job: Record) -> dict:
        external_question(job.payload["query"])
        current = await self.repo.get(job.id)
        if current is None:
            raise ValueError("Task missing")
        if job.payload.get("source_event_id") and not await self.store.event_active(job.payload["source_event_id"]):
            raise PermissionError("Source recalled")
        remote_id = current.payload.get("remote_id")
        if not remote_id:
            async with self.submit_lock:
                today = time.strftime("%Y-%m-%d", time.gmtime())
                history = await self.repo.find("research", limit=10000)
                calls = sum(row.payload.get("day") == today and row.id != job.id+"-history" for row in history)
                if calls >= self.daily_limit:
                    raise PermissionError("Daily research budget exceeded")
                sources = []
                if self.search:
                    try:
                        sources = await self.search.search(job.payload["query"])
                    except Exception:
                        sources = []
                prompt = job.payload['query']
                # Reserve the daily call persistently before touching the website.
                history_row = await self.repo.put(Record(job.id + "-history", "research", job.owner_id,
                    job.group_id, "running", {"query": job.payload["query"], "prompt": prompt, "sources": sources,
                    "day": today, "started_at": time.time(), "browser_request_id": job.id}, "research:" + job.id))
                remote = await self.browser.ask(prompt, job.id)
                remote_id = remote["id"]
                current = await self.repo.patch(job.id, payload={**current.payload, "remote_id": remote_id})
        while True:
            current = await self.repo.get(job.id)
            if current is None or current.status == "cancelled":
                await self.browser.cancel(remote_id)
                raise asyncio.CancelledError()
            if job.payload.get("source_event_id") and not await self.store.event_active(job.payload["source_event_id"]):
                await self.browser.cancel(remote_id)
                raise PermissionError("Source recalled")
            remote = await self.browser.status(remote_id)
            history = await self.repo.get(job.id + "-history")
            if history:
                await self.repo.patch(history.id, status=remote["status"], payload={**history.payload,
                    "answer": remote.get("answer", ""), "page_url": remote.get("page_url"), "error": remote.get("error")})
            if remote["status"] == "succeeded":
                return {"answer": remote["answer"], "sources": history.payload.get("sources", []) if history else [],
                        "provider": "deepseek_web", "browser_task_id": remote_id, "researched_at": time.time()}
            if remote["status"] in {"failed", "cancelled"}:
                raise RuntimeError("Browser research did not complete")
            await self.repo.patch(job.id, payload={**current.payload,
                "external_status": remote["status"], "needs_user": remote["status"] == "waiting_human"})
            if self.defer_human_wait and remote["status"] == "waiting_human":
                raise TaskDeferred()
            await asyncio.sleep(2)

    async def cancel(self, job: Record) -> None:
        current = await self.repo.get(job.id)
        remote_id = current.payload.get("remote_id") if current else None
        if remote_id:
            await self.browser.cancel(remote_id)
