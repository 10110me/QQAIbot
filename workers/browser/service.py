"""Persistent, visible DeepSeek browser. Captchas/login are completed by the owner."""
from __future__ import annotations
import asyncio
import json
import os
import secrets
import sqlite3
import time
import base64
import mimetypes
import logging
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from resource_slot import HeavySlot

logger = logging.getLogger("deepseek.browser")


class AskInput(BaseModel):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    prompt: str = Field(min_length=1, max_length=24000)
    session_id: str | None = Field(default=None, max_length=120)
    new_conversation: bool = False
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=4)


class BrowserDatabase:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS tasks_status ON tasks(json_extract(payload, '$.status'))")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, task: dict[str, Any]) -> dict[str, Any]:
        task["updated_at"] = time.time()
        self.db.execute("INSERT INTO tasks(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                        (task["id"], json.dumps(task, ensure_ascii=False)))
        self.db.commit()
        return task

    def list(self) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM tasks ORDER BY rowid DESC LIMIT 500")]

    def pending(self) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM tasks WHERE json_extract(payload, '$.status') "
            "IN ('queued', 'preparing', 'uploading', 'submitting', 'running', 'waiting_human') ORDER BY rowid LIMIT 500")]

    def conversation_url(self, session_id: str) -> str | None:
        rows = self.db.execute("SELECT payload FROM tasks ORDER BY rowid DESC LIMIT 500").fetchall()
        for row in rows:
            task = json.loads(row[0])
            if task.get('session_id') == session_id and task.get('page_url'):
                return task['page_url']
        return None

    def last_url(self, url: str | None = None) -> str | None:
        if url is not None:
            self.db.execute("INSERT INTO metadata VALUES('last_url', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (url,))
            self.db.commit()
        row = self.db.execute("SELECT value FROM metadata WHERE key='last_url'").fetchone()
        return row[0] if row else None


class VisibleBrowser:
    def __init__(self, database: BrowserDatabase) -> None:
        self.database = database
        self.page: Any = None
        self.context: Any = None
        self.playwright: Any = None
        self.state = "starting"
        self.active_task_id: str | None = None
        self.base_url = os.environ.get("DEEPSEEK_URL", "https://chat.deepseek.com")
        self.input_selector = os.environ.get("DEEPSEEK_INPUT_SELECTOR", "textarea, [contenteditable='true']")
        self.answer_selector = os.environ.get("DEEPSEEK_ANSWER_SELECTOR", ".ds-markdown")
        self.send_selector = os.environ.get("DEEPSEEK_SEND_SELECTOR", "")
        self.stop_selector = os.environ.get("DEEPSEEK_STOP_SELECTOR", "button[aria-label*='Stop'], button[aria-label*='停止']")
        self.chat_selector = os.environ.get("DEEPSEEK_NEW_CHAT_SELECTOR", "")
        self.file_selector = os.environ.get("DEEPSEEK_FILE_SELECTOR", "input[type='file']")
        self.search_toggle = os.environ.get("DEEPSEEK_SEARCH_TOGGLE_SELECTOR", "")
        self.search_enabled = os.environ.get("DEEPSEEK_SEARCH_ENABLED_SELECTOR", "")
        self.timeout = int(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "300"))
        self.stable: dict[str, tuple[str, float]] = {}
        self.lazy = os.environ.get("BROWSER_LAZY", "false").lower() == "true"
        self.idle_seconds = float(os.environ.get("BROWSER_IDLE_SECONDS", "60"))
        self.last_used = time.monotonic()
        self.view_until = 0.0
        self.login_requested = False
        self.slot = HeavySlot()
        self.lifecycle_lock = asyncio.Lock()

    def view_lease(self) -> None:
        self.view_until = time.monotonic() + 45

    async def ensure_started(self) -> None:
        async with self.lifecycle_lock:
            if self.context and self.page and not self.page.is_closed():
                return
            await self.sleep()
            self.state = "waiting_memory_slot"
            await self.slot.acquire()
            try:
                await self._launch()
            except BaseException:
                await self.sleep()
                raise

    async def start(self) -> None:
        if self.lazy:
            self.state = "sleeping"
        else:
            await self.ensure_started()

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright
        self.state = "starting"
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            os.environ.get("BROWSER_PROFILE", "/data/profile"), headless=False,
            viewport={"width": 1000, "height": 640}, accept_downloads=False,
            args=["--disable-dev-shm-usage", "--disable-extensions", "--disable-sync",
                  "--disable-background-mode", "--renderer-process-limit=2",
                  "--window-size=1024,768", "--window-position=0,0"],
        )
        self.context.set_default_timeout(10000)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        if self.lazy:
            for page in self.context.pages[1:]:
                await page.close()
        active = [row for row in self.database.pending() if row.get("submitted")]
        restore_url = (active[0].get("page_url") if active else None) or self.database.last_url()
        # Worker-controlled host; never navigate to a URL submitted by a group member.
        if restore_url and not restore_url.startswith(self.base_url.rstrip("/") + "/"):
            restore_url = None
        await self.page.goto(restore_url or self.base_url, wait_until="domcontentloaded", timeout=60000)
        self.last_used = time.monotonic()
        self.state = "waiting_login"

    async def sleep(self) -> None:
        context, playwright, page = self.context, self.playwright, self.page
        self.context = self.playwright = self.page = None
        try:
            if page and not page.is_closed() and page.url.startswith(self.base_url.rstrip("/") + "/"):
                self.database.last_url(page.url)
            if context:
                await context.close()
        finally:
            try:
                if playwright:
                    await playwright.stop()
            finally:
                self.slot.release()
                self.state = "sleeping"

    async def close(self) -> None:
        try:
            await self.sleep()
        finally:
            self.database.db.close()

    async def challenge(self) -> bool:
        selectors = "iframe[src*='captcha'], iframe[src*='challenge'], iframe[title*='验证'], .captcha-container"
        candidates = self.page.locator(selectors)
        for i in range(await candidates.count()):
            if await candidates.nth(i).is_visible():
                return True
        return False

    async def editable(self) -> Any:
        candidates = self.page.locator(self.input_selector)
        for i in range(await candidates.count()):
            item = candidates.nth(i)
            if await item.is_visible() and await item.is_enabled():
                return item
        return None

    def fail(self, task: dict, code: str, message: str) -> None:
        task.update(status="failed", error_code=code, error=message, finished_at=time.time())
        self.database.save(task)
        self.stable.pop(task["id"], None)
        logger.warning("deepseek_task_failed request_id=%s code=%s", task["id"], code)

    async def editor_value(self, editor: Any) -> str:
        return await editor.evaluate("el => 'value' in el ? el.value : el.innerText || ''")

    async def prompt_count(self, prompt: str) -> int:
        # get_by_text excludes the textarea's value, so a filled draft cannot
        # be mistaken for a user message rendered in the conversation.
        return await self.page.get_by_text(prompt, exact=True).count()

    async def confirm_submission(self, task: dict, editor: Any) -> bool:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (self.database.get(task["id"]) or {}).get("status") in {"cancelled", "deleted"}:
                return False
            value = (await self.editor_value(editor)).strip()
            count = await self.prompt_count(task["prompt"])
            if not value and count > task.get("baseline_prompt_count", 0):
                return True
            await asyncio.sleep(.3)
        return False

    async def wait_uploads(self, task: dict) -> bool:
        """Observe rendered file chips and upload progress, not input.files.

        A file input containing a blob only means selection happened. It does
        not prove that DeepSeek finished uploading or parsing the attachment.
        """
        names = [item["name"] for item in task["attachments"]]
        deadline, settled = time.monotonic() + 90, None
        while time.monotonic() < deadline:
            if (self.database.get(task["id"]) or {}).get("status") in {"cancelled", "deleted"}:
                return False
            state = await self.page.evaluate("""names => {
                const visible = el => {
                    if (!el.getClientRects().length) return false;
                    for (let node=el; node; node=node.parentElement) {
                        const css=getComputedStyle(node);
                        if (css.display==='none' || css.visibility==='hidden' || css.visibility==='collapse' || Number(css.opacity)===0) return false;
                    }
                    return true;
                };
                const candidates=[...document.querySelectorAll('img[alt], [title], [data-file-name], div, span')];
                const matches=names.map(name => candidates.find(el => visible(el) &&
                    (el.getAttribute('alt') === name || el.getAttribute('title') === name || el.getAttribute('data-file-name') === name || el.textContent.trim() === name)));
                const areas=matches.filter(Boolean).map(el => el.closest('[data-file-name], [data-file-id]') || el.parentElement?.parentElement || el);
                const busy = areas.some(area => [...area.querySelectorAll("[role='progressbar'], [aria-busy='true'], [data-uploading='true'], .uploading, .file-uploading, .ds-loading")].some(visible));
                // DeepSeek retains image hover overlays/spinners in the DOM
                // with opacity:0. Also ignore words in an earlier chat reply.
                const text=areas.flatMap(area => [...area.querySelectorAll('div,span,p')].filter(el => !el.children.length && visible(el)).map(el => el.innerText)).join(' ');
                return {chips: matches.every(Boolean), busy: busy || /上传中|解析中|正在上传|正在解析|uploading|parsing/i.test(text),
                    failed: /上传失败|解析失败|文件过大|upload failed|unsupported file/i.test(text)};
            }""", names)
            if task.get('upload_observation') != state:
                task['upload_observation'] = state
                self.database.save(task)
            if state["failed"]:
                self.fail(task, "ATTACHMENT_UPLOAD_FAILED", "DeepSeek 提示附件上传或解析失败，未发送问题")
                return False
            if state["chips"] and not state["busy"]:
                settled = settled or time.monotonic()
                if time.monotonic() - settled >= 2:
                    return True
            else:
                settled = None
            await asyncio.sleep(.4)
        self.fail(task, "ATTACHMENT_UPLOAD_TIMEOUT", "DeepSeek 附件上传未确认完成，未发送问题")
        return False

    async def tick(self) -> None:
        if not self.page or self.page.is_closed():
            self.state = "browser_closed"
            return
        if (urlsplit(self.page.url).scheme, urlsplit(self.page.url).netloc) != (urlsplit(self.base_url).scheme, urlsplit(self.base_url).netloc):
            self.state = "waiting_login"
            for task in self.database.pending():
                if task["status"] in {"queued", "running"}:
                    task.update(status="waiting_human", error="请在实时浏览器中完成登录")
                    self.database.save(task)
            return
        editor = await self.editable()
        challenge = await self.challenge()
        self.state = "waiting_human" if challenge else ("ready" if editor else "waiting_login")
        tasks = self.database.pending()
        for old in tasks:
            if old.get("submitted") and time.time() - old.get("submitted_at", old.get("created_at", time.time())) > self.timeout:
                self.fail(old, "ANSWER_TIMEOUT", "等待 DeepSeek 网页回复超时，已释放队列；不会自动重发")
            elif old.get("send_attempted_at") and old["status"] != "running":
                self.fail(old, "SEND_STATE_UNKNOWN", "上次发送结果尚未确认，已停止自动重发并释放队列")
            elif old["status"] in {"preparing", "uploading"}:
                # A restart before any send attempt can safely prepare again.
                old.update(status="queued", conversation_opened=False)
                self.database.save(old)
        tasks = self.database.pending()
        if not tasks:
            self.active_task_id = None
            return
        tasks.sort(key=lambda r: (not r.get("submitted", False), r["created_at"]))
        task = tasks[0]
        self.active_task_id = task["id"]
        if task["status"] in {"queued", "running"} and not challenge and editor:
            self.last_used = time.monotonic()
        if challenge or (not editor and not task.get("submitted")):
            if task["status"] != "waiting_human":
                task.update(status="waiting_human", error_code="LOGIN_OR_CHALLENGE", error="请在实时浏览器中完成登录或人机验证")
                self.database.save(task)
            return
        if not task.get("submitted"):
            conversation_url = self.database.conversation_url(task['session_id']) if task.get('session_id') else None
            if task.get('new_conversation') or not conversation_url:
                if not task.get('conversation_opened'):
                    await self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=60000)
                    task['conversation_opened'] = True
                    self.database.save(task)
                    editor = await self.editable()
            elif not task.get('conversation_opened'):
                target, base = urlsplit(conversation_url), urlsplit(self.base_url)
                if (target.scheme, target.netloc) != (base.scheme, base.netloc) or not target.path.startswith('/a/chat/'):
                    task.update(status='failed', error='保存的网页会话地址无效')
                    self.database.save(task)
                    return
                await self.page.goto(conversation_url, wait_until='domcontentloaded', timeout=60000)
                task['conversation_opened'] = True
                self.database.save(task)
                editor = await self.editable()
            if self.chat_selector and not conversation_url:
                new_chat = self.page.locator(self.chat_selector).first
                if await new_chat.is_visible():
                    await new_chat.click()
                    editor = await self.editable()
            if editor is None:
                return
            task.update(status="preparing", error=None)
            self.database.save(task)
            if self.search_toggle and self.search_enabled:
                enabled = self.page.locator(self.search_enabled)
                if await enabled.count() == 0 or not await enabled.first.is_visible():
                    await self.page.locator(self.search_toggle).first.click()
                    enabled = self.page.locator(self.search_enabled)
                    if await enabled.count() == 0 or not await enabled.first.is_visible():
                        self.fail(task, "SEARCH_MODE_UNAVAILABLE", "网页未确认联网搜索已开启，已释放队列")
                        return
            task["baseline_count"] = await self.page.locator(self.answer_selector).count()
            task["baseline_prompt_count"] = await self.prompt_count(task["prompt"])
            # Filling the prompt makes the send button's upload-disabled state
            # meaningful. No key is pressed until attachments have settled.
            await editor.fill(task["prompt"])
            if task.get("attachments"):
                selector = self.page.locator(self.file_selector)
                if await selector.count() == 0:
                    self.fail(task, "ATTACHMENT_INPUT_MISSING", "网页未提供文件上传入口，未发送问题，已释放队列")
                    return
                upload_started = time.monotonic()
                # Internal disk names include request IDs; expose only the
                # original user filename to DeepSeek's attachment chips.
                files = [{"name": item["name"], "mimeType": item.get("mime_type") or mimetypes.guess_type(item["name"])[0] or "application/octet-stream",
                          "buffer": await asyncio.to_thread(Path(item["path"]).read_bytes)} for item in task["attachments"]]
                task.update(status="uploading", upload_started_at=time.time(), uploaded_attachments=[item.get("name") for item in task["attachments"]])
                self.database.save(task)
                await selector.first.set_input_files(files)
                if not await self.wait_uploads(task):
                    return
                task.update(upload_completed_at=time.time(), upload_wait_ms=round((time.monotonic()-upload_started)*1000))
                self.database.save(task)
                logger.info("deepseek_upload_completed request_id=%s files=%s wait_ms=%s", task["id"], task.get("uploaded_attachments"), task["upload_wait_ms"])
            # This is ordinary UI interaction; no captcha bypass or anti-detection code.
            task.update(status="submitting", submitted=False, send_attempted_at=time.time(), error=None)
            self.database.save(task)  # Persist intent, never a false success.
            self.last_used = time.monotonic()
            try:
                if self.send_selector:
                    await self.page.locator(self.send_selector).first.click()
                else:
                    await editor.press("Enter")
                confirmed = await self.confirm_submission(task, editor)
            except Exception:
                self.fail(task, "SEND_STATE_UNKNOWN", "网页发送未能确认，已停止自动重发并释放队列")
                return
            if (self.database.get(task["id"]) or {}).get("status") in {"cancelled", "deleted"}:
                return
            if not confirmed:
                self.fail(task, "SEND_NOT_CONFIRMED", "网页未确认收到问题，已释放队列；不会自动重发")
                return
            task.update(status="running", submitted=True, submitted_at=time.time(), error=None, error_code=None)
            logger.info("deepseek_message_submitted request_id=%s files=%s", task["id"], task.get("uploaded_attachments", []))
            task["page_url"] = self.page.url
            self.database.save(task)
            return
        answer_nodes = self.page.locator(self.answer_selector)
        count = await answer_nodes.count()
        answer = (await answer_nodes.last.inner_text()).strip() if count > task.get("baseline_count", 0) else ""
        if answer:
            previous, changed_at = self.stable.get(task["id"], ("", time.monotonic()))
            if answer != previous:
                self.stable[task["id"]] = (answer, time.monotonic())
            stop = self.page.locator(self.stop_selector)
            generating = await stop.count() > 0 and await stop.first.is_visible()
            task.update(answer=answer[:80000], page_url=self.page.url, status="running", error=None)
            if answer == previous and not generating and time.monotonic() - changed_at >= 5:
                task.update(status="succeeded", finished_at=time.time())
                self.stable.pop(task["id"], None)
            self.database.save(task)
        if task["status"] != "succeeded" and time.time() - task.get("submitted_at", time.time()) > self.timeout:
            self.fail(task, "ANSWER_TIMEOUT", "等待 DeepSeek 网页回复超时，已释放队列；不会自动重发")

    async def loop(self) -> None:
        errors = 0
        while True:
            try:
                if self.page and self.page.is_closed():
                    await self.sleep()
                if not self.context:
                    pending = self.database.pending()
                    if self.lazy and time.monotonic() >= self.view_until and not any(
                        row["status"] in {"queued", "preparing", "uploading", "submitting", "running"} for row in pending
                    ):
                        await asyncio.sleep(2)
                        continue
                    await self.ensure_started()
                if self.login_requested:
                    await self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
                    self.login_requested = False
                await self.tick()
                if (self.lazy and time.monotonic() >= self.view_until
                        and time.monotonic() - self.last_used >= self.idle_seconds):
                    await self.sleep()
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors += 1
                if self.lazy and errors >= 3:
                    # A crashed page must not retain the only heavy-job slot forever.
                    row = self.database.get(self.active_task_id) if self.active_task_id else None
                    if row and row["status"] in {"queued", "preparing", "uploading", "submitting", "running"}:
                        self.fail(row, "BROWSER_TASK_ERROR", "浏览器连续异常，已释放队列：" + type(exc).__name__)
                    await self.sleep()
                    errors = 0
                self.state = "needs_attention:" + type(exc).__name__
            await asyncio.sleep(1)


def create_app() -> FastAPI:
    worker_token = os.environ.get("OSA_WORKER_TOKEN", "")
    if len(worker_token) < 24:
        raise RuntimeError("OSA_WORKER_TOKEN must have at least 24 characters")
    database = BrowserDatabase(os.environ.get("BROWSER_TASK_DB", "/data/browser.db"))
    browser = VisibleBrowser(database)

    async def authorize(authorization: str = Header(default="")) -> None:
        if not secrets.compare_digest(authorization, "Bearer " + worker_token):
            raise HTTPException(401, "Unauthorized")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await browser.start()
        task = asyncio.create_task(browser.loop())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await browser.close()

    app = FastAPI(title="Isolated DeepSeek Browser", lifespan=lifespan, dependencies=[Depends(authorize)])

    @app.get("/health")
    async def health() -> dict:
        return {"state": browser.state, "page_url": browser.page.url if browser.page else None,
                "active_task_id": browser.active_task_id}

    @app.get("/page-state")
    async def page_state() -> dict:
        """Authenticated diagnostics for the visible page, without cookies/storage."""
        if not browser.page or browser.page.is_closed():
            return {"state": browser.state, "page_url": None}
        return {"state": browser.state, "page_url": browser.page.url,
                **await browser.page.evaluate("""() => ({
                    text: document.body.innerText.slice(-6000),
                    inputs: [...document.querySelectorAll('textarea, [contenteditable=true]')].map(el => ({tag:el.tagName, length:('value' in el ? el.value : el.innerText || '').length})),
                    buttons: [...document.querySelectorAll('button, [role=button]')].filter(el => el.getClientRects().length).slice(-30).map(el => ({text:el.innerText, label:el.getAttribute('aria-label'), disabled:el.getAttribute('aria-disabled') || el.disabled, html:el.outerHTML.slice(0,900)}))
                })""")}

    @app.post("/ask")
    async def ask(payload: AskInput) -> dict:
        existing = database.get(payload.request_id)
        if existing:
            return existing
        attachments, seen = [], set()
        upload_dir = Path(os.environ.get("BROWSER_UPLOAD_DIR", "/data/uploads"))
        upload_dir.mkdir(parents=True, exist_ok=True)
        for index, item in enumerate(payload.attachments):
            if not isinstance(item, dict):
                raise HTTPException(400, "INVALID_ATTACHMENT")
            name = Path(str(item.get("name") or f"attachment-{index}")).name
            mime_type=str(item.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream")
            original_image=mime_type.startswith('image/')
            raw = str(item.get("data_base64") or "")
            if not raw or (not original_image and len(raw) > 8_000_000):
                raise HTTPException(413, "ATTACHMENT_TOO_LARGE")
            try:
                data = base64.b64decode(raw, validate=True)
            except Exception as exc:
                raise HTTPException(400, "INVALID_ATTACHMENT_ENCODING") from exc
            if not original_image and len(data) > 5_000_000:
                raise HTTPException(413, "ATTACHMENT_TOO_LARGE")
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            path = upload_dir / (payload.request_id + "-" + str(index) + "-" + name)
            path.write_bytes(data)
            attachments.append({"name": name, "mime_type": mime_type,
                                "path": str(path), "size": len(data), "sha256": digest})
        return database.save({"id": payload.request_id, "prompt": payload.prompt,
            "session_id": payload.request_id if payload.new_conversation else (payload.session_id or payload.request_id),
            "new_conversation": payload.new_conversation,
            "attachments": attachments, "answer": "",
            "status": "queued", "submitted": False, "created_at": time.time(), "error": None})

    @app.get("/tasks")
    async def tasks() -> list:
        return database.list()

    @app.get("/tasks/{task_id}")
    async def task_status(task_id: str) -> dict:
        row = database.get(task_id)
        if not row:
            raise HTTPException(404, "Task not found")
        return row

    @app.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: str) -> dict:
        row = database.get(task_id)
        if not row:
            raise HTTPException(404, "Task not found")
        row.update(status="cancelled")
        database.save(row)
        if browser.page and not browser.page.is_closed():
            stop = browser.page.locator(browser.stop_selector)
            if browser.active_task_id == task_id and await stop.count() and await stop.first.is_visible():
                await stop.first.click()
        return row

    @app.post("/view-lease")
    async def view_lease() -> dict:
        browser.view_lease()
        return {"state": browser.state, "lease_seconds": 45}

    @app.delete("/tasks/{task_id}")
    async def forget_task(task_id: str) -> dict:
        row = database.get(task_id)
        if not row:
            return {"deleted": True}
        database.save({"id": task_id, "prompt": "", "answer": "", "status": "deleted",
                       "created_at": row["created_at"], "submitted": True})
        return {"deleted": True, "website_history": "Manage separately in the DeepSeek account"}

    @app.post("/open-login")
    async def open_login() -> dict:
        if any(r["status"] in {"preparing", "uploading", "submitting", "running"} for r in database.pending()):
            raise HTTPException(409, "A question is currently running")
        browser.login_requested = True
        browser.view_lease()
        return {"state": "waiting_human", "message": "请使用实时桌面手动扫码和验证"}

    return app
