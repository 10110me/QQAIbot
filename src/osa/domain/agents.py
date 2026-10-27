"""V3 public contracts. No platform, model vendor or database dependencies."""
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol

AGENT_PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class DecisionContext:
    event_id: str
    group_id: str | None
    conversation_version: int
    features: dict[str, Any]


@dataclass(frozen=True)
class DecisionResult:
    action: Literal["THINK", "SKIP", "DEFER"]
    provider: str
    model_version: str
    reason_code: str
    confidence: float | None = None
    calibrated: bool = False
    calibration_version: str | None = None
    scores: dict[str, float] | None = None
    wait_seconds: float | None = None


class DecisionProvider(Protocol):
    async def predict(self, context: DecisionContext) -> DecisionResult: ...


@dataclass(frozen=True)
class CodingTaskRequest:
    parent_task_id: str
    requester_id: str
    source_group_id: str | None
    workspace_id: str
    goal: str
    acceptance_criteria: list[str]
    idempotency_key: str
    constraints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentCapabilities:
    can_pause: bool = False
    can_resume: bool = False
    can_cancel: bool = False
    supports_stream: bool = False
    readonly: bool = False


class CodingAgentProvider(Protocol):
    capabilities: AgentCapabilities
    async def submit(self, request: CodingTaskRequest) -> str: ...
    async def get_status(self, task_id: str) -> dict: ...
    async def send_instruction(self, task_id: str, command: dict) -> str: ...
    async def pause(self, task_id: str) -> None: ...
    async def resume(self, task_id: str) -> None: ...
    async def cancel(self, task_id: str) -> None: ...
    def stream_events(self, task_id: str) -> AsyncIterator[dict]: ...
    async def artifacts(self, task_id: str) -> list[dict]: ...


class CodeObserverProvider(Protocol):
    async def inspect(self, task_id: str, mode: str, revision: str | None = None) -> dict: ...


@dataclass(frozen=True)
class ReplyBrief:
    intent: str
    key_points: list[str]
    evidence_ids: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    attachment_ids: list[str] = field(default_factory=list)
    # Verified files produced by the AI workspace, selected by their real name.
    artifact_names: list[str] = field(default_factory=list)
    artifact_renames: dict[str, str] = field(default_factory=dict)
    reply_text: str | None = None
    mention_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ThinkResult:
    session_id: str
    final_action: Literal["SILENT", "REPLY", "WAIT_FOR_USER", "TASK_DELEGATED", "FAILED"]
    brief: ReplyBrief | None = None
    delegated_task_ids: list[str] = field(default_factory=list)
    persona_note: dict | None = None


class ProviderStateUnknown(RuntimeError):
    """A remote write may have succeeded. Reconcile; never retry it blindly."""


class UnsupportedCapability(RuntimeError):
    pass
