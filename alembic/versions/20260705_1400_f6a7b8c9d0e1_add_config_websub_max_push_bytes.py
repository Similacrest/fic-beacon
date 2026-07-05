"""add config.websub_max_push_bytes

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-05 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'websub_max_push_bytes', sa.Integer(), nullable=False, server_default='100000'))


def downgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('websub_max_push_bytes')
