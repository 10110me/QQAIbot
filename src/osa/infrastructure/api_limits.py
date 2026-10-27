"""Per-endpoint concurrency gates shared by model clients in one process."""

import asyncio
import threading
import weakref


MAX_API_REQUESTS = 10
_LOCK = threading.Lock()
_SLOTS = weakref.WeakKeyDictionary()


def api_slot(base_url: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    endpoint = base_url.rstrip("/").casefold()
    with _LOCK:
        slots = _SLOTS.setdefault(loop, {})
        return slots.setdefault(endpoint, asyncio.Semaphore(MAX_API_REQUESTS))
