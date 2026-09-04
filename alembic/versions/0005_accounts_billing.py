"""organization plan, billing and retention columns

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-03 22:35:25.380598

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('organizations', sa.Column('billing_email', sa.String(length=320), nullable=True))
    op.add_column('organizations', sa.Column('billing_status', sa.String(length=20), nullable=False, server_default='ok'))
    op.add_column('organizations', sa.Column('payment_method_id', sa.String(length=100), nullable=True))
    op.add_column('organizations', sa.Column('payment_method_title', sa.String(length=100), nullable=True))
    op.add_column('organizations', sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('organizations', sa.Column('audit_retention_days', sa.Integer(), nullable=True))
    op.add_column('organizations', sa.Column('message_retention_days', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('organizations', 'message_retention_days')
    op.drop_column('organizations', 'audit_retention_days')
    op.drop_column('organizations', 'is_system')
    op.drop_column('organizations', 'payment_method_title')
    op.drop_column('organizations', 'payment_method_id')
    op.drop_column('organizations', 'billing_status')
    op.drop_column('organizations', 'billing_email')
