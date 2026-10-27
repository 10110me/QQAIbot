"""V2 task/control/experience tables for PostgreSQL.

SQLite's separate control file is initialized transactionally by Registry.
"""
from alembic import op
from osa.infrastructure.v2.registry import SCHEMA

revision='0004'
down_revision='0003'
branch_labels=None
depends_on=None


def upgrade():
    if op.get_bind().dialect.name=='postgresql':
        op.execute('SELECT pg_advisory_xact_lock(7312030)')
        for statement in SCHEMA:op.execute(statement)
        op.execute("INSERT INTO v2_meta VALUES('schema_version','1') ON CONFLICT(key) DO NOTHING")


def downgrade():
    if op.get_bind().dialect.name=='postgresql':
        for name in ['v2_policy','v2_echo','v2_records','v2_checkpoints','v2_commands','v2_events','v2_tasks','v2_meta']:
            op.execute('DROP TABLE IF EXISTS '+name)
