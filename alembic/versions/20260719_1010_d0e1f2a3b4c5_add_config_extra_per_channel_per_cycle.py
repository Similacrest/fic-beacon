"""add config.extra_per_channel_per_cycle

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-19 10:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'extra_per_channel_per_cycle', sa.Integer(), nullable=False, server_default='1'))


def downgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('extra_per_channel_per_cycle')
