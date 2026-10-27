from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("outbox_messages", sa.Column("attachment_ids", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    with op.batch_alter_table("outbox_messages") as batch:
        batch.drop_column("attachment_ids")
