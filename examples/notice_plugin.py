"""Trusted read-only plugin example; not an automatic third-party loader."""
from dataclasses import asdict
from typing import Any

from osa.domain.entities import Destination
from osa.domain.ports import GroupResourceAdapter
from osa.sdk.contracts import CapabilityContext, PluginManifest

MANIFEST = PluginManifest(
    id="org.example.notice-reader", name="Group Notice Reader", version="0.1.0",
    plugin_type="knowledge_connector", isolation="builtin",
    permissions=[{"capability": "group.notices.read", "resource": "group_scoped"}],
)


async def read_notices(context: CapabilityContext, platform: GroupResourceAdapter,
                       destination: Destination) -> list[dict[str, Any]]:
    context.require("group.notices.read", destination)
    if destination.kind != "group":
        raise PermissionError("Group destination required")
    return [asdict(notice) for notice in await platform.notices(destination.id)]
