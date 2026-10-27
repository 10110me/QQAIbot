import json
import time
import uuid
from dataclasses import asdict

from osa.domain.entities import Destination
from osa.domain.features import FeatureRepository, Record
from osa.domain.ports import Store
from osa.sdk.contracts import PluginManifest


class PluginService:
    """Third-party code is only sent to the isolated runner, never imported by the app."""
    ALLOWED = {"group.notices.read", "group.files.read", "conversation.read"}

    def __init__(self, repo: FeatureRepository, store: Store, sandbox) -> None:
        self.repo, self.store, self.sandbox = repo, store, sandbox

    async def install(self, manifest: dict, source: str) -> Record:
        parsed = PluginManifest.model_validate(manifest)
        parsed.validate_compatibility()
        if parsed.isolation != "process" or len(source) > 30000:
            raise PermissionError("Community plugins require isolated process execution")
        if any(permission.capability not in self.ALLOWED for permission in parsed.permissions):
            raise PermissionError("This plugin requests unsupported or privileged permissions")
        if any(row.payload["manifest"]["id"] == parsed.id for row in await self.repo.find("plugin", limit=1000)):
            raise ValueError("Plugin ID already installed; disable/remove before changing its code")
        return await self.repo.put(Record(uuid.uuid4().hex, "plugin", "system-admin", None, "disabled",
            {"manifest": parsed.model_dump(), "source": source, "grants": [], "groups": []}, "plugin:"+parsed.id))

    async def approve(self, record_id: str, grants: list[str], groups: list[str]) -> Record:
        row = await self.repo.get(record_id)
        if not row or row.kind != "plugin":
            raise KeyError(record_id)
        manifest = PluginManifest.model_validate(row.payload["manifest"])
        requested = {value.capability for value in manifest.permissions}
        if not set(grants) <= requested or not set(grants) <= self.ALLOWED:
            raise PermissionError("Cannot grant undeclared permissions")
        return await self.repo.patch(record_id, status="enabled", payload={**row.payload,
            "grants": sorted(set(grants)), "groups": sorted(set(groups)), "approved_at": time.time()})

    async def run(self, record_id: str, destination: Destination, actor: str, arguments: dict, context: dict) -> dict:
        row = await self.repo.get(record_id)
        if not row or row.kind != "plugin" or row.status != "enabled" or destination.kind != "group" or destination.id not in row.payload["groups"]:
            raise PermissionError("Plugin not enabled for this group")
        policy = await self.store.policy(destination)
        if not policy.enabled or await self.store.paused():
            raise PermissionError("Bot paused")
        data = {"group_id": destination.id, "actor_id": actor}
        grants = set(row.payload["grants"])
        if "conversation.read" in grants:
            data["messages"] = context.get("messages", [])[-10:]
        if "group.notices.read" in grants and policy.notice_read:
            data["notices"] = await self.store.resources(destination.id, "notices")
        if "group.files.read" in grants and policy.file_metadata_read:
            data["files"] = await self.store.resources(destination.id, "files")
        # No credentials, host paths, raw global history or broker access enters the runner.
        code = "import json\nnamespace = {}\nexec(" + repr(row.payload["source"]) + ", namespace)\n"
        code += "result = namespace['run'](" + repr(data) + ", " + repr(arguments) + ")\nprint(json.dumps(result, ensure_ascii=False))\n"
        result = await self.sandbox.run("python", {"code": code, "timeout": 10}, uuid.uuid4().hex)
        if result.get("status") != "succeeded":
            raise RuntimeError("Isolated plugin failed")
        return {"plugin_id": row.payload["manifest"]["id"], "output": json.loads(result["stdout"].strip())}
