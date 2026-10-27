"""Apply Nova's group-chat persona card through the owner configuration API."""

import argparse
import json
from pathlib import Path

import httpx

from osa.config import Settings
from osa.infrastructure.runtime_config import PersonaProfile
from osa.application.nova_v4 import IDENTITY, PERSONALITY, RELATIONSHIPS


RELATIONSHIP_RULES = RELATIONSHIPS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("card", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--owner-qq", action="append")
    parser.add_argument("--enable-self-update", action="store_true")
    parser.add_argument("--card-only", action="store_true")
    args = parser.parse_args()

    card = args.card.read_text(encoding="utf-8").strip()
    settings = Settings()
    headers = {"Authorization": f"Bearer {settings.admin_token.get_secret_value()}"}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=20) as client:
        result = client.get("/api/v1/config")
        result.raise_for_status()
        current = result.json()["data"]
        persona = current["persona"]
        updated = PersonaProfile.model_validate({
            **persona,
            "identity": IDENTITY,
            "personality": PERSONALITY,
            "reply_style": card,
            "relationship_rules": RELATIONSHIP_RULES,
            "owner_qq_ids": args.owner_qq if args.owner_qq is not None else persona.get("owner_qq_ids", []),
            "self_update_enabled": args.enable_self_update or persona.get("self_update_enabled", False),
        })
        if args.card_only:
            updated = PersonaProfile.model_validate({**persona, "reply_style": card})

        if args.verify:
            from osa.application.prompt_catalog import CATALOG

            assert PersonaProfile.model_validate(persona) == updated
            assert "先满足实际目标，再处理语气" in CATALOG[
                "agent_thinking"
            ]["default"]
            print(f"Nova persona and thinking prompt verified at revision {current['revision']}")
            return

        backup = Path(settings.data_dir) / "private" / f"persona-before-card-rev-{current['revision']}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("x", encoding="utf-8") as output:
            json.dump(persona, output, ensure_ascii=False, indent=2)

        result = client.put("/api/v1/config/persona", json={
            **updated.model_dump(mode="json"),
            "revision": current["revision"],
        })
        result.raise_for_status()
        print(f"Nova persona updated from revision {current['revision']}; backup: {backup}")


if __name__ == "__main__":
    main()
