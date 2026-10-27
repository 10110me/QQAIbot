"""V3 agent projections, durable events, wakeups and read-only reviews."""
from alembic import op
from osa.infrastructure.v3.registry import SCHEMA

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('SELECT pg_advisory_xact_lock(7312030)')
        for statement in SCHEMA:
            op.execute(statement)


def downgrade():
    if op.get_bind().dialect.name == 'postgresql':
        for name in ['agent_tool_receipts', 'coding_review_reports', 'coding_bindings', 'thinking_wakeups', 'agent_domain_events', 'thinking_sessions', 'agent_tasks_v3']:
            op.execute('DROP TABLE IF EXISTS '+name)
