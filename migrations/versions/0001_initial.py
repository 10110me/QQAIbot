"""Initial immutable schema snapshot for v0.1.

Revision ID: 0001
Revises: None
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dedupe_key", sa.String(64), nullable=False, unique=True),
        sa.Column("conversation", sa.String(100), nullable=False),
        sa.Column("message_id", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.Float(), nullable=False),
        sa.Column("received_at", sa.Float(), nullable=False),
        sa.Column("recalled", sa.Boolean(), nullable=False),
        sa.Column("collect", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(100)),
    )
    for column in ("conversation", "message_id", "actor_id", "status", "occurred_at"):
        op.create_index(f"ix_events_{column}", "events", [column])
    op.create_table("groups", sa.Column("id", sa.String(100), primary_key=True), sa.Column("settings", sa.JSON(), nullable=False))
    op.create_table("controls", sa.Column("id", sa.String(32), primary_key=True), sa.Column("paused", sa.Boolean(), nullable=False))
    op.create_table("user_consents", sa.Column("actor_id", sa.String(100), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("groups", sa.JSON(), nullable=False), sa.Column("version", sa.Integer(), nullable=False))
    op.create_table("memories", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False), sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(100)), sa.Column("origin_group_id", sa.String(100)),
        sa.Column("source_event_id", sa.String(64), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("expires_at", sa.Float()), sa.Column("version", sa.Integer(), nullable=False))
    for column in ("scope", "subject_id", "origin_group_id", "source_event_id", "status"):
        op.create_index(f"ix_memories_{column}", "memories", [column])
    op.create_table("decision_logs", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("event_id", sa.String(64), sa.ForeignKey("events.id"), nullable=False, unique=True),
        sa.Column("action", sa.String(32), nullable=False), sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_table("outbox_messages", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("event_id", sa.String(64), sa.ForeignKey("events.id"), nullable=False, unique=True),
        sa.Column("decision_id", sa.String(64), sa.ForeignKey("decision_logs.id"), nullable=False),
        sa.Column("conversation", sa.String(100), nullable=False), sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("reply_to", sa.String(100), nullable=False), sa.Column("text", sa.Text(), nullable=False),
        sa.Column("memory_ids", sa.JSON(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("resource_kinds", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False), sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("platform_message_id", sa.String(100)), sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=False), sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False), sa.Column("sent_at", sa.Float()), sa.Column("error", sa.String(100)))
    for column in ("conversation", "status", "created_at", "platform_message_id"):
        op.create_index(f"ix_outbox_messages_{column}", "outbox_messages", [column])
    op.create_table("message_reviews", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("outbox_id", sa.String(64), sa.ForeignKey("outbox_messages.id"), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False), sa.Column("better_response", sa.Text()),
        sa.Column("note", sa.Text()), sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_message_reviews_outbox_id", "message_reviews", ["outbox_id"])
    op.create_table("group_resources", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("group_id", sa.String(100), nullable=False), sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("platform_id", sa.String(200), nullable=False), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False), sa.Column("missing_count", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("group_id", "kind", "platform_id", "version"))
    op.create_index("ix_group_resources_group_id", "group_resources", ["group_id"])
    op.create_table("daily_episodes", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("group_id", sa.String(100), nullable=False), sa.Column("local_date", sa.String(16), nullable=False),
        sa.Column("pipeline_version", sa.String(32), nullable=False), sa.Column("source_ids", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False), sa.Column("status", sa.String(32), nullable=False),
        sa.UniqueConstraint("group_id", "local_date", "pipeline_version"))
    op.create_index("ix_daily_episodes_group_id", "daily_episodes", ["group_id"])
    op.create_table("audit_logs", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("action", sa.String(100), nullable=False), sa.Column("resource_id", sa.String(200), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False))


def downgrade():
    for table in ("audit_logs", "daily_episodes", "group_resources", "message_reviews", "outbox_messages", "decision_logs", "memories", "user_consents", "controls", "groups", "events"):
        op.drop_table(table)
