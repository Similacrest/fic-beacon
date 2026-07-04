"""add channel.feed_item_limit

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-04 10:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing channels to 50 (the old static settings.feed_item_limit).
    with op.batch_alter_table('channel', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'feed_item_limit', sa.Integer(), nullable=False, server_default='50'))


def downgrade() -> None:
    with op.batch_alter_table('channel', schema=None) as batch_op:
        batch_op.drop_column('feed_item_limit')
