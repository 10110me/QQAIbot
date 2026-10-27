"""Codex CLI adapter using the isolated workspace bridge."""
from osa.infrastructure.v3.opencode import OpenCodeAdapter, PINNED_CODEX_VERSION


class CodexAdapter(OpenCodeAdapter):
    def __init__(self, registry, endpoint, password, version=PINNED_CODEX_VERSION,
                 username="codex", client=None, managed_gateway=True):
        super().__init__(registry, endpoint, password, version, username, client,
                         managed_gateway, provider="codex")
