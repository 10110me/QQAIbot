"""Optional host-wide heavy-job lock shared only by trusted worker containers.

No dependency on the main application, and no lock when OSA_HEAVY_SLOT is unset.
The low-memory Compose file mounts the same lock inode in every worker. The
kernel releases flock automatically when a worker dies; no stale lease survives.
"""
import asyncio
import errno
import os
import time


class HeavySlot:
    def __init__(self) -> None:
        self.fd: int | None = None

    async def acquire(self) -> None:
        path = os.environ.get("OSA_HEAVY_SLOT")
        if not path:
            return
        import fcntl

        deadline = time.monotonic() + float(os.environ.get("OSA_HEAVY_WAIT_SECONDS", "180"))
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        try:
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except OSError as exc:
                    if exc.errno not in {errno.EAGAIN, errno.EACCES}:
                        raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another heavy job or live browser is using the memory slot")
                await asyncio.sleep(0.2)
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()
