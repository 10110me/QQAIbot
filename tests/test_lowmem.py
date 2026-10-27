"""Resource lifecycle and recovery checks; no external account or Chromium needed."""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
from resource_slot import HeavySlot
from workers.browser.service import BrowserDatabase, VisibleBrowser
from osa.application.features.research import ResearchService
from osa.application.features.tasks import TaskService
from osa.domain.features import Record
from osa.infrastructure.features.repository import MemoryFeatureRepository
from osa.infrastructure.memory_store import InMemoryStore


@unittest.skipIf(os.name == "nt", "flock is exercised in the Linux worker container")
class HeavySlotTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_excludes_another_process_and_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heavy.lock"
            path.touch()
            with patch.dict(os.environ, {"OSA_HEAVY_SLOT": str(path)}):
                slot = HeavySlot()
                await slot.acquire()
                # A separate process must see the same kernel lock, not an asyncio-only lock.
                code = "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                result = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(path),
                    stderr=asyncio.subprocess.DEVNULL)
                self.assertNotEqual(await result.wait(), 0)
                slot.release()
                result = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(path))
                self.assertEqual(await result.wait(), 0)

    async def test_cancelling_waiter_does_not_unlock_owner_or_leak_fd(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heavy.lock"
            path.touch()
            with patch.dict(os.environ, {"OSA_HEAVY_SLOT": str(path)}):
                async with HeavySlot():
                    waiter = HeavySlot()
                    task = asyncio.create_task(waiter.acquire())
                    await asyncio.sleep(0.01)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIsNone(waiter.fd)
                async with HeavySlot():
                    pass

    async def test_timeout_releases_waiter_and_unconfigured_mode_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heavy.lock"
            path.touch()
            with patch.dict(os.environ, {"OSA_HEAVY_SLOT": str(path), "OSA_HEAVY_WAIT_SECONDS": "0"}):
                async with HeavySlot():
                    waiter = HeavySlot()
                    with self.assertRaises(TimeoutError):
                        await waiter.acquire()
                    self.assertIsNone(waiter.fd)
        with patch.dict(os.environ, {}, clear=True):
            async with HeavySlot() as slot:
                self.assertIsNone(slot.fd)


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = BrowserDatabase(str(Path(self.directory.name) / "browser.db"))
        with patch.dict(os.environ, {"BROWSER_LAZY": "true", "BROWSER_IDLE_SECONDS": "0"}):
            self.browser = VisibleBrowser(self.database)

    async def asyncTearDown(self):
        await self.browser.close()
        self.directory.cleanup()

    async def test_idle_start_does_not_start_playwright_or_load_old_answers(self):
        self.database.save({"id": "done", "status": "succeeded", "answer": "a"*80000})
        self.browser._launch = AsyncMock()
        await self.browser.start()
        await asyncio.wait_for(self.run_loop_briefly(), 1)
        self.browser._launch.assert_not_awaited()
        self.assertEqual(self.database.pending(), [])
        self.assertEqual(self.browser.state, "sleeping")

    async def run_loop_briefly(self):
        task = asyncio.create_task(self.browser.loop())
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_sleep_closes_both_processes_but_keeps_database_and_profile_url(self):
        context, playwright = AsyncMock(), AsyncMock()
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://chat.deepseek.com/a/session"
        self.browser.context, self.browser.playwright, self.browser.page = context, playwright, page
        self.database.save({"id": "done", "status": "succeeded", "answer": "保留问答"})
        await self.browser.sleep()
        context.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()
        self.assertIsNone(self.browser.context)
        self.assertIsNone(self.browser.page)
        self.assertEqual(self.database.get("done")["answer"], "保留问答")
        self.assertEqual(self.database.last_url(), page.url)

    async def test_launch_failure_releases_resources_and_does_not_mark_question_unsubmitted(self):
        self.database.save({"id": "running", "status": "running", "submitted": True})
        self.browser._launch = AsyncMock(side_effect=RuntimeError("Chromium unavailable"))
        with self.assertRaises(RuntimeError):
            await self.browser.ensure_started()
        self.assertIsNone(self.browser.slot.fd)
        self.assertTrue(self.database.get("running")["submitted"])

    async def test_existing_submitted_task_is_polled_without_sending_again(self):
        row = {"id": "running", "status": "running", "submitted": True, "baseline_count": 0,
               "created_at": time.time(), "submitted_at": time.time(), "prompt": "不要重发"}
        self.database.save(row)
        editor = AsyncMock()
        page = MagicMock()
        page.url = "https://chat.deepseek.com/a/session"
        page.is_closed.return_value = False
        page.locator.return_value.count = AsyncMock(return_value=0)
        self.browser.page = page
        self.browser.editable = AsyncMock(return_value=editor)
        self.browser.challenge = AsyncMock(return_value=False)
        await self.browser.tick()
        editor.fill.assert_not_awaited()
        editor.press.assert_not_awaited()

    async def test_view_lease_wakes_lazy_browser_and_idle_loop_closes_it(self):
        async def launch():
            self.browser.context = AsyncMock()
            self.browser.page = MagicMock()
            self.browser.page.is_closed.return_value = False
            self.browser.page.url = "https://chat.deepseek.com/"
            self.browser.state = "ready"
        self.browser._launch = AsyncMock(side_effect=launch)
        self.browser.tick = AsyncMock()
        self.browser.view_lease()
        await self.run_loop_briefly()
        self.browser._launch.assert_awaited_once()
        self.assertIsNotNone(self.browser.context)
        context = self.browser.context
        self.browser.view_until = 0
        await self.run_loop_briefly()
        context.close.assert_awaited_once()
        self.assertIsNone(self.browser.context)


class LowMemoryWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_for_login_frees_task_slot_and_reuses_remote_id(self):
        repo = MemoryFeatureRepository()
        browser = AsyncMock()
        browser.ask.return_value = {"id": "remote"}
        browser.status.return_value = {"status": "waiting_human", "answer": ""}
        research = ResearchService(repo, InMemoryStore(), browser, None, 10, defer_human_wait=True)
        tasks = TaskService(repo)
        tasks.handlers["job.research"] = research.run
        row = await tasks.submit("research", {"query": "最新消息"}, actor="1", group="10001")
        claimed = await repo.claim(["job.research"], time.time())
        await tasks.execute(claimed)
        saved = await repo.get(row.id)
        self.assertEqual(saved.status, "retry")
        self.assertEqual(saved.payload["attempts"], 0)
        self.assertTrue(saved.payload["needs_user"])
        self.assertEqual(saved.payload["remote_id"], "remote")
        browser.status.return_value = {"status": "succeeded", "answer": "已登录"}
        await tasks.execute(saved)
        browser.ask.assert_awaited_once()
        self.assertEqual((await repo.get(row.id)).status, "succeeded")

    async def test_screenshot_service_start_and_validation_never_launch_chromium(self):
        from workers.browser.screenshot import create_app
        with patch.dict(os.environ, {"OSA_WORKER_TOKEN": "worker-token-for-test-123456789"}):
            app = create_app()
        with patch("playwright.async_api.async_playwright") as playwright:
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
                result = await client.post("/screenshot", json={"url": "https://example.com"})
                self.assertEqual(result.status_code, 401)
            playwright.assert_not_called()

    @unittest.skipIf(os.name == "nt", "flock is exercised in the Linux worker container")
    async def test_failed_screenshot_closes_browser_and_releases_slot(self):
        from workers.browser.screenshot import create_app
        browser = AsyncMock()
        browser.new_context.side_effect = RuntimeError("Page unavailable")
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        manager = AsyncMock()
        manager.__aenter__.return_value = playwright
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "heavy.lock"
            lock_path.touch()
            with patch.dict(os.environ, {"OSA_WORKER_TOKEN": "worker-token-for-test-123456789",
                 "OSA_HEAVY_SLOT": str(lock_path)}), patch(
                 "playwright.async_api.async_playwright", return_value=manager):
                app = create_app()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url="http://test", headers={"Authorization": "Bearer worker-token-for-test-123456789"}) as client:
                    result = await client.post("/screenshot", json={"url": "https://example.com"})
                self.assertEqual(result.status_code, 500)
                browser.close.assert_awaited_once()
                async with HeavySlot():
                    pass

    async def test_sleeping_worker_accepts_login_and_cancel_without_a_browser(self):
        from workers.browser.service import create_app
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "OSA_WORKER_TOKEN": "worker-token-for-test-123456789", "BROWSER_LAZY": "true",
            "BROWSER_TASK_DB": str(Path(directory) / "worker.db")
        }):
            app = create_app()
            with patch.object(VisibleBrowser, "loop", new=AsyncMock()):
                async with app.router.lifespan_context(app), httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                    headers={"Authorization": "Bearer worker-token-for-test-123456789"}) as client:
                    self.assertEqual((await client.get("/health")).json()["state"], "sleeping")
                    await client.post("/ask", json={"request_id": "a", "prompt": "问题"})
                    response = await client.post("/tasks/a/cancel")
                    self.assertEqual(response.json()["status"], "cancelled")
                    self.assertEqual((await client.post("/open-login")).status_code, 200)
                    self.assertEqual((await client.post("/view-lease")).status_code, 200)
