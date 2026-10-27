"""Durable feature records and scoped vectors; PostgreSQL uses pgvector."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("feature_records", sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False), sa.Column("owner_id", sa.String(100)),
        sa.Column("group_id", sa.String(100)), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("dedupe_key", sa.String(200)), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False), sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("kind", "dedupe_key"))
    for column in ("kind", "owner_id", "group_id", "status", "created_at"):
        op.create_index(f"ix_feature_records_{column}", "feature_records", [column])
    op.create_table("feature_vectors", sa.Column("record_id", sa.String(64), sa.ForeignKey("feature_records.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("model_id", sa.String(200), primary_key=True), sa.Column("dimension", sa.Integer(), nullable=False), sa.Column("vector", sa.JSON(), nullable=False))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("""CREATE TABLE feature_vectors_pg (
            record_id TEXT REFERENCES feature_records(id) ON DELETE CASCADE,
            model_id TEXT NOT NULL, dimension INTEGER NOT NULL, embedding vector NOT NULL,
            PRIMARY KEY(record_id,model_id), CHECK(vector_dims(embedding)=dimension))""")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TABLE feature_vectors_pg")
    op.drop_table("feature_vectors")
    op.drop_table("feature_records")
