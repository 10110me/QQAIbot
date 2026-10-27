from dataclasses import dataclass
from typing import Any, Literal, Protocol

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

from osa import __version__
from osa.domain.entities import Destination

API_VERSION = "1.0"


class PermissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str = Field(min_length=1, max_length=100)
    resource: Literal["group_scoped", "private_scoped"]


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,99}$")
    name: str = Field(min_length=1, max_length=100)
    version: str
    api_version: str = API_VERSION
    requires_core: str = ">=0.1,<1.0"
    plugin_type: Literal["tool", "model", "media", "knowledge_connector", "platform"]
    isolation: Literal["process", "builtin"] = "process"
    permissions: list[PermissionRequest] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)

    def validate_compatibility(self) -> None:
        Version(self.version)
        if self.api_version != API_VERSION or Version(__version__) not in SpecifierSet(self.requires_core):
            raise ValueError("Incompatible plugin contract")


@dataclass(frozen=True)
class CapabilityContext:
    destination: Destination
    grants: frozenset[str]

    def require(self, capability: str, destination: Destination) -> None:
        if destination != self.destination or capability not in self.grants:
            raise PermissionError("Plugin capability or resource scope denied")


class ToolExecutor(Protocol):
    async def execute(self, context: CapabilityContext, arguments: dict[str, Any]) -> dict[str, Any]: ...


class MediaProvider(Protocol):
    async def analyze(self, object_id: str, task_type: str) -> dict[str, Any]: ...


class ResearchProvider(Protocol):
    async def research(self, query: str) -> list[dict[str, Any]]: ...


class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class ObjectStore(Protocol):
    async def put(self, object_id: str, content: bytes, mime: str) -> None: ...
    async def delete(self, object_id: str) -> None: ...


class PluginRegistry:
    """Manifest validation never imports third-party executable code."""

    def __init__(self) -> None:
        self.manifests: dict[str, PluginManifest] = {}
        self.approvals: dict[str, frozenset[str]] = {}

    def discover(self, manifest: PluginManifest) -> None:
        manifest.validate_compatibility()
        if manifest.id in self.manifests:
            raise ValueError("Duplicate plugin ID")
        self.manifests[manifest.id] = manifest

    def approve(self, plugin_id: str, grants: frozenset[str]) -> None:
        manifest = self.manifests[plugin_id]
        requested = {permission.capability for permission in manifest.permissions}
        if not grants <= requested:
            raise PermissionError("Cannot grant undeclared capability")
        if any(dependency not in self.manifests for dependency in manifest.dependencies):
            raise ValueError("Missing plugin dependency")
        self.approvals[plugin_id] = grants

    def context(self, plugin_id: str, destination: Destination) -> CapabilityContext:
        if plugin_id not in self.approvals:
            raise PermissionError("Plugin permissions not approved")
        return CapabilityContext(destination, self.approvals[plugin_id])

# Optional V2 read/control contracts; no echo/send action is granted.
from osa.domain.work import TaskProgressProvider, EchoStateProvider, ClaimVerifier
from osa.domain.agents import (AGENT_PROTOCOL_VERSION, AgentCapabilities, CodingAgentProvider,
    CodingTaskRequest, CodeObserverProvider, DecisionContext, DecisionProvider, DecisionResult,
    ReplyBrief, ThinkResult)
