"""One decision may produce several independently tracked short QQ messages."""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    constraints = sa.inspect(op.get_bind()).get_unique_constraints('outbox_messages')
    name = next((c['name'] for c in constraints if c['column_names']==['event_id']), None)
    with op.batch_alter_table('outbox_messages', naming_convention={'uq':'uq_%(table_name)s_%(column_0_name)s'}) as batch:
        batch.drop_constraint(name or 'uq_outbox_messages_event_id', type_='unique')
        batch.create_index('ix_outbox_messages_event_id', ['event_id'], unique=False)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT event_id FROM outbox_messages GROUP BY event_id HAVING COUNT(*)>1 LIMIT 1')).first():
        raise RuntimeError('Cannot restore one-message constraint while split replies exist; preserve the database and application backup.')
    with op.batch_alter_table('outbox_messages') as batch:
        batch.drop_index('ix_outbox_messages_event_id')
        batch.create_unique_constraint('uq_outbox_messages_event_id', ['event_id'])
