"""add book.story_status

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-19 10:20:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.add_column(sa.Column('story_status', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.drop_column('story_status')
