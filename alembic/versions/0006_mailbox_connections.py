"""mailbox connections (existing IMAP/SMTP mailboxes as inboxes)

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('mailbox_connections',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('inbox_id', sa.String(length=40), nullable=False),
    sa.Column('provider', sa.String(length=30), nullable=False),
    sa.Column('address', sa.String(length=320), nullable=False),
    sa.Column('config_encrypted', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_uid', sa.Integer(), nullable=False),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('sync_interval_seconds', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['inbox_id'], ['inboxes.id'], ),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('inbox_id')
    )
    op.create_index(op.f('ix_mailbox_connections_organization_id'), 'mailbox_connections', ['organization_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_mailbox_connections_organization_id'), table_name='mailbox_connections')
    op.drop_table('mailbox_connections')
