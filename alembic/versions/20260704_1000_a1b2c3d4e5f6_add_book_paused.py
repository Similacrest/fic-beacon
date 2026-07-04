"""add book.paused

Revision ID: a1b2c3d4e5f6
Revises: cba964cd14a1
Create Date: 2026-07-04 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'cba964cd14a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing rows to un-paused; the model default keeps new rows
    # consistent. (New FeedbackAction values 'pause'/'read' need no migration — the enum is a
    # constraint-free VARCHAR on SQLite.)
    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'paused', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.drop_column('paused')
