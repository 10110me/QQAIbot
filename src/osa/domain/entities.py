from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


class Action(StrEnum):
    IGNORE = "IGNORE"
    WAIT = "WAIT"
    REACT = "REACT"
    REPLY = "REPLY"
    INITIATE = "INITIATE"
    TASK = "TASK"


class Scope(StrEnum):
    LONG_GROUP = "long_group"
    DAILY_GROUP = "daily_group"
    PRIVATE_USER = "private_user"
    PERSONAL_OWNER = "global_personal_owner"
    PUBLIC = "global_public_knowledge"
    GROUP_RESOURCE = "group_resource"


@dataclass(frozen=True)
class Destination:
    kind: str
    id: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass(frozen=True)
class Event:
    id: str
    dedupe_key: str
    adapter_id: str
    event_type: str
    destination: Destination
    actor_id: str
    message_id: str
    text: str
    segments: list[dict[str, Any]]
    occurred_at: float
    reply_to_id: str | None = None
    mentioned: bool = False
    schema_version: str = "1.0"
    actor_name: str = ""
    actor_nickname: str = ""
    actor_role: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        value = dict(data)
        value["destination"] = Destination(**value["destination"])
        return cls(**value)


@dataclass(frozen=True)
class GroupPolicy:
    enabled: bool = True
    collect_messages: bool = True
    notice_read: bool = False
    file_metadata_read: bool = False
    cross_memory: bool = False
    context_limit: int = 40
    reply_ttl_seconds: int = 180
    min_send_interval: float = 2.0
    quiet_start: int | None = None
    quiet_end: int | None = None
    timezone: str = "Asia/Shanghai"
    file_content_index: bool = False
    media_read: bool = False
    research: bool = False
    sandbox: bool = False
    initiative: bool = False
    initiative_min_interval: int = 45
    initiative_daily_limit: int = 0
    memory_extract: bool = False
    experience_collect: bool = False
    experience_embedding: bool = False
    research_external: bool = False
    echo_silence: bool = True
    talk_frequency: float = 0.5
    batch_quiet_seconds: float = 1.0
    batch_max_wait_seconds: float = 8.0
    decision_cooldown_seconds: float = 8.0
    decision_wait_seconds: float = 5.0


@dataclass(frozen=True)
class Consent:
    enabled: bool = False
    disclosure_groups: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Memory:
    id: str
    content: str
    scope: Scope
    subject_id: str | None
    origin_group_id: str | None
    source_event_id: str
    status: str = "active"
    expires_at: float | None = None
    version: int = 1


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


@dataclass(frozen=True)
class ModelResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    attachment_ids: list[str] = field(default_factory=list)
    mention_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Notice:
    id: str
    body: str
    published_at: float | None = None


@dataclass(frozen=True)
class GroupFile:
    id: str
    name: str
    size: int
    folder_id: str | None = None
    busid: int | None = None


@dataclass(frozen=True)
class ToolRequest:
    name: str
    capability: str
    actor_id: str
    destination: Destination
    arguments: dict[str, Any] = field(default_factory=dict)


class UpstreamUnavailable(Exception):
    """Safe failure before an external write was attempted."""


class DeliveryUnknown(Exception):
    """An external write may have succeeded; never blindly resend."""
