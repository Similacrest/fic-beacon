"""additive vote weights, fade-below-floor, retire 👎 threshold + multiplicative extra boost

Votes are now additive on quota_weight (capped at 100.0): `vote_step` per 👍/👎 and
`extra_boost_step` per 🪝. A source whose weight is below `weight_skip_floor` fades (its
acceptance is scaled by weight/floor) instead of being skipped; the unread-drop penalty is a
transient ramp bounded by `unacked_penalty_floor`. Auto-drop is now "weight reaches 0 via 👎", so
`thumbs_down_drop_threshold` and the multiplicative `extra_boost_multiplier` are removed.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-19 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'vote_step', sa.Float(), nullable=False, server_default='0.25'))
        batch_op.add_column(sa.Column(
            'extra_boost_step', sa.Float(), nullable=False, server_default='0.5'))
        batch_op.add_column(sa.Column(
            'weight_skip_floor', sa.Float(), nullable=False, server_default='1.0'))
        batch_op.add_column(sa.Column(
            'unacked_penalty_floor', sa.Float(), nullable=False, server_default='0.2'))
        batch_op.drop_column('thumbs_down_drop_threshold')
        batch_op.drop_column('extra_boost_multiplier')


def downgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'thumbs_down_drop_threshold', sa.Integer(), nullable=False, server_default='3'))
        batch_op.add_column(sa.Column(
            'extra_boost_multiplier', sa.Float(), nullable=False, server_default='1.5'))
        batch_op.drop_column('unacked_penalty_floor')
        batch_op.drop_column('weight_skip_floor')
        batch_op.drop_column('extra_boost_step')
        batch_op.drop_column('vote_step')
