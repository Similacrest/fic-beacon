"""add book.label_map

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-05 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Piecewise stub label map: JSON [physical_index, offset] breakpoints. NULL for books that
    # never had a URL-diffed stub (they use the legacy scalar chapter_label_offset).
    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.add_column(sa.Column('label_map', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('book', schema=None) as batch_op:
        batch_op.drop_column('label_map')
