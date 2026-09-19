"""release schedules: global schedule table, channel.budget -> channel.weight

Release timing and budgets move off the single global cadence/per-channel budget onto a
`schedule` table (cron + whole-release budget + words/minutes mode). Each channel keeps only a
relative `weight` (its share of a release's budget).

Data: every channel's budget is converted to words (minutes x config.wpm), `weight` is that
budget divided by the smallest one (so the smallest channel is 1.0 and the proportions are
preserved exactly), and one enabled 'Default' schedule is created from the old
`config.cadence_cron` with budget = the sum of the channels' words - so the very first release
after upgrading is the same size as before.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-19 10:40:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cfg = bind.execute(sa.text("SELECT wpm, cadence_cron FROM config WHERE id = 1")).fetchone()
    wpm = (cfg[0] if cfg else None) or 250
    cron = (cfg[1] if cfg else None) or '0 7,19 * * *'
    channels = bind.execute(sa.text("SELECT id, budget, budget_mode FROM channel")).fetchall()
    words = {
        cid: float(budget) * (wpm if str(mode).lower().endswith('minutes') else 1)
        for cid, budget, mode in channels
    }

    op.create_table(
        'schedule',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('cron', sa.String(), nullable=False),
        sa.Column('budget', sa.Float(), nullable=False),
        sa.Column('budget_mode', sa.Enum('words', 'minutes', name='budgetmode'), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    total = sum(words.values())
    bind.execute(
        sa.text(
            "INSERT INTO schedule (name, cron, budget, budget_mode, enabled, sort_order) "
            "VALUES ('Default', :cron, :budget, 'words', 1, 0)"
        ),
        {"cron": cron, "budget": total if total > 0 else 5000.0},
    )

    with op.batch_alter_table('channel', schema=None) as batch_op:
        batch_op.add_column(sa.Column('weight', sa.Float(), nullable=False, server_default='1.0'))
    smallest = min((w for w in words.values() if w > 0), default=1.0)
    for cid, w in words.items():
        bind.execute(
            sa.text("UPDATE channel SET weight = :w WHERE id = :id"),
            {"w": round(max(w, 0.0) / smallest, 3) or 1.0, "id": cid},
        )

    with op.batch_alter_table('channel', schema=None) as batch_op:
        batch_op.drop_column('budget')
        batch_op.drop_column('budget_mode')
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('cadence_cron')


def downgrade() -> None:
    bind = op.get_bind()
    first = bind.execute(
        sa.text("SELECT cron, budget FROM schedule ORDER BY enabled DESC, sort_order, id LIMIT 1")
    ).fetchone()
    cron = first[0] if first else '0 7,19 * * *'
    total = float(first[1]) if first else 5000.0
    weights = bind.execute(sa.text("SELECT id, weight FROM channel")).fetchall()
    weight_sum = sum(float(w) for _, w in weights) or 1.0

    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'cadence_cron', sa.String(), nullable=False, server_default='0 7,19 * * *'))
    bind.execute(sa.text("UPDATE config SET cadence_cron = :c"), {"c": cron})
    with op.batch_alter_table('channel', schema=None) as batch_op:
        batch_op.add_column(sa.Column('budget', sa.Float(), nullable=False, server_default='5000'))
        batch_op.add_column(sa.Column(
            'budget_mode', sa.Enum('words', 'minutes', name='budgetmode'),
            nullable=False, server_default='words'))
    for cid, w in weights:
        bind.execute(
            sa.text("UPDATE channel SET budget = :b WHERE id = :id"),
            {"b": max(0.01, round(total * float(w) / weight_sum, 2)), "id": cid},
        )
    with op.batch_alter_table('channel', schema=None) as batch_op:
        batch_op.drop_column('weight')
    op.drop_table('schedule')
