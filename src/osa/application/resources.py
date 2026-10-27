import asyncio
from dataclasses import asdict
from typing import Any

from osa.domain.entities import Destination
from osa.domain.policy import ToolPolicy
from osa.domain.ports import GroupResourceAdapter, Store


class ResourceService:
    def __init__(self, store: Store, adapter: GroupResourceAdapter) -> None:
        self.store, self.adapter = store, adapter
        self.policy = ToolPolicy()
        self._locks: dict[str, asyncio.Lock] = {}

    async def sync(self, group_id: str, kind: str) -> list[dict[str, Any]]:
        if kind not in {"notices", "files"}:
            raise ValueError("Unknown resource kind")
        destination = Destination("group", group_id)
        self.policy.require("group.notices.read" if kind == "notices" else "group.files.read", destination, await self.store.policy(destination))
        lock = self._locks.setdefault(f"{group_id}:{kind}", asyncio.Lock())
        async with lock:
            values = await (self.adapter.notices(group_id) if kind == "notices" else self.adapter.files(group_id))
            await self.store.save_resources(group_id, kind, [asdict(value) for value in values])
            return await self.store.resources(group_id, kind)

    async def evidence(self, destination: Destination, text: str) -> dict[str, Any]:
        if destination.kind != "group":
            return {}
        selected = []
        if "公告" in text or "群规" in text:
            selected.append("notices")
        if "群文件" in text:
            selected.append("files")
        evidence: dict[str, Any] = {}
        for kind in selected:
            try:
                values = await self.sync(destination.id, kind)
                evidence[kind] = {"fresh": True, "items": values[:30]}
            except PermissionError:
                evidence[kind] = {"error": "当前群未授权读取该资源"}
            except Exception:
                # Current cached evidence may be used only if permission still holds.
                group = await self.store.policy(destination)
                allowed = group.notice_read if kind == "notices" else group.file_metadata_read
                evidence[kind] = {"fresh": False, "error": "同步失败，以下仅为上次同步版本",
                                  "items": (await self.store.resources(destination.id, kind))[:30] if allowed else []}
        return evidence

    async def group_file_url(self, group_id: str, file_id: str, busid: int | None = None) -> str:
        """Return a short lived OneBot download URL after policy checking."""
        destination = Destination("group", group_id)
        self.policy.require("group.files.read", destination, await self.store.policy(destination))
        method = getattr(self.adapter, "file_url", None)
        if method is None:
            raise ValueError("Group file download is not supported by this adapter")
        return str(await method(group_id, file_id, busid or 0))
