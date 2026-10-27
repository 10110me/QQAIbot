"""Pure message addressing helpers shared by adapters and policy decisions."""
from __future__ import annotations

from typing import Any


def at_targets(event: Any) -> list[str]:
    segments = event.segments if hasattr(event, "segments") else event.get("segments", [])
    return [str(item.get("data", {}).get("qq", "")) for item in segments
            if isinstance(item, dict) and item.get("type") == "at"
            and str(item.get("data", {}).get("qq", ""))]
