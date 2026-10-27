import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    conversation: Mapped[str] = mapped_column(String(100), index=True)
    message_id: Mapped[str] = mapped_column(String(100), index=True)
    actor_id: Mapped[str] = mapped_column(String(100), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    occurred_at: Mapped[float] = mapped_column(Float, index=True)
    received_at: Mapped[float] = mapped_column(Float)
    recalled: Mapped[bool] = mapped_column(Boolean, default=False)
    collect: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(String(100), nullable=True)


class GroupRow(Base):
    __tablename__ = "groups"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ControlRow(Base):
    __tablename__ = "controls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)


class ConsentRow(Base):
    __tablename__ = "user_consents"
    actor_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    groups: Mapped[list[str]] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)


class MemoryRow(Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    origin_group_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class DecisionRow(Base):
    __tablename__ = "decision_logs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), unique=True)
    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(100))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class OutboxRow(Base):
    __tablename__ = "outbox_messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decision_logs.id"))
    conversation: Mapped[str] = mapped_column(String(100), index=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    reply_to: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    memory_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    resource_kinds: Mapped[list[str]] = mapped_column(JSON, default=list)
    attachment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[float] = mapped_column(Float, index=True)
    expires_at: Mapped[float] = mapped_column(Float)
    platform_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ReviewRow(Base):
    __tablename__ = "message_reviews"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    outbox_id: Mapped[str] = mapped_column(ForeignKey("outbox_messages.id"), index=True)
    tags: Mapped[list[str]] = mapped_column(JSON)
    better_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class ResourceRow(Base):
    __tablename__ = "group_resources"
    __table_args__ = (UniqueConstraint("group_id", "kind", "platform_id", "version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    group_id: Mapped[str] = mapped_column(String(100), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    platform_id: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    current: Mapped[bool] = mapped_column(Boolean, default=True)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[float] = mapped_column(Float)


class DailyRow(Base):
    __tablename__ = "daily_episodes"
    __table_args__ = (UniqueConstraint("group_id", "local_date", "pipeline_version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    group_id: Mapped[str] = mapped_column(String(100), index=True)
    local_date: Mapped[str] = mapped_column(String(16))
    pipeline_version: Mapped[str] = mapped_column(String(32), default="source-card-v1")
    source_ids: Mapped[list[str]] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active")


class AuditRow(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str] = mapped_column(String(200))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class FeatureRow(Base):
    __tablename__ = "feature_records"
    __table_args__ = (UniqueConstraint("kind", "dedupe_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, index=True)
    updated_at: Mapped[float] = mapped_column(Float)


class VectorRow(Base):
    __tablename__ = "feature_vectors"
    record_id: Mapped[str] = mapped_column(ForeignKey("feature_records.id", ondelete="CASCADE"), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    dimension: Mapped[int] = mapped_column(Integer)
    vector: Mapped[list[float]] = mapped_column(JSON)
