"""rename app_state key last_drop_run_at -> last_release_run_at

Terminology cleanup: the scheduled cycle that emits `drop` rows is now called a
"release cycle" (to disambiguate from ❌ *dropping* a source). Rename the persisted
last-run timestamp key so the dashboard keeps showing the last run after the rename.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-08 17:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE app_state SET key = 'last_release_run_at' "
        "WHERE key = 'last_drop_run_at'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE app_state SET key = 'last_drop_run_at' "
        "WHERE key = 'last_release_run_at'"
    )
