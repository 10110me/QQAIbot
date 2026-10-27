from datetime import datetime
from zoneinfo import ZoneInfo

from osa.domain.entities import Consent, Destination, GroupPolicy, Memory, Scope


class PermissionPolicy:
    """Retrieval and disclosure are separate checks, applied before the model."""

    def __init__(self, global_group_memory: bool = False) -> None:
        self.global_group_memory = global_group_memory
        self.group_memory_sources: set[str] = set()

    def update_group_memory_source(self, group_id: str, policy: GroupPolicy) -> None:
        if policy.enabled and policy.collect_messages:
            self.group_memory_sources.add(group_id)
        else:
            self.group_memory_sources.discard(group_id)

    def shares_group_memory(self, origin_group_id: str | None, destination: Destination) -> bool:
        return (self.global_group_memory and destination.kind == 'group'
                and origin_group_id in self.group_memory_sources)

    def can_reuse_group_reply(
        self, memory_ids: list[str], resource_kinds: list[str],
        memories: dict[str, Memory], destination: Destination, now: float,
    ) -> bool:
        if resource_kinds:
            return False
        if not memory_ids:
            return True
        if destination.kind != "group":
            return False
        return all(
            (memory := memories.get(memory_id)) is not None
            and memory.status == "active"
            and (memory.expires_at is None or memory.expires_at > now)
            and memory.scope in {Scope.LONG_GROUP, Scope.DAILY_GROUP, Scope.GROUP_RESOURCE}
            and (memory.origin_group_id == destination.id or self.shares_group_memory(memory.origin_group_id, destination))
            for memory_id in memory_ids
        )

    def can_retrieve(
        self, memory: Memory, actor: str, destination: Destination,
        consent: Consent, group: GroupPolicy, now: float,
    ) -> bool:
        if memory.status != "active" or (memory.expires_at and memory.expires_at <= now):
            return False
        if memory.scope in {Scope.LONG_GROUP, Scope.DAILY_GROUP, Scope.GROUP_RESOURCE}:
            return destination.kind == "group" and (
                memory.origin_group_id == destination.id or self.shares_group_memory(memory.origin_group_id, destination))
        if memory.scope == Scope.PRIVATE_USER:
            return destination.kind == "private" and memory.subject_id == actor
        if memory.scope == Scope.PERSONAL_OWNER:
            return memory.subject_id == actor and consent.enabled and (
                destination.kind == "private" or group.cross_memory
            )
        # Public knowledge requires a separate verified knowledge ingestion flow.
        return False

    def can_disclose(
        self, memory: Memory, actor: str, destination: Destination,
        consent: Consent, group: GroupPolicy, now: float,
    ) -> bool:
        if not self.can_retrieve(memory, actor, destination, consent, group, now):
            return False
        if memory.scope == Scope.PERSONAL_OWNER and destination.kind == "group":
            return destination.id in consent.disclosure_groups
        return True

    def can_send(self, group: GroupPolicy, now: float, paused: bool) -> bool:
        if paused or not group.enabled:
            return False
        if group.quiet_start is None or group.quiet_end is None:
            return True
        hour = datetime.fromtimestamp(now, ZoneInfo(group.timezone)).hour
        start, end = group.quiet_start, group.quiet_end
        quiet = start <= hour < end if start < end else hour >= start or hour < end
        return not quiet


class ToolPolicy:
    """Fail closed: only implemented read-only capabilities can be executed."""

    READ_CAPABILITIES = frozenset({"group.notices.read", "group.files.read"})

    def require(self, capability: str, destination: Destination, group: GroupPolicy) -> None:
        permitted = destination.kind == "group" and (
            (capability == "group.notices.read" and group.notice_read)
            or (capability == "group.files.read" and group.file_metadata_read)
        )
        if capability not in self.READ_CAPABILITIES or not permitted:
            raise PermissionError(f"Capability denied: {capability}")
