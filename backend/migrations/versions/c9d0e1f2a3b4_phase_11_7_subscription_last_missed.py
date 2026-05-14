"""Phase 11-7: graph_subscriptions.last_missed_at

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-05-14 12:35:00.000000

Records when Microsoft told us a subscription dropped notifications.
The Phase 11-7 backfill function reads this column to decide which
rows need reconciliation; on completion it clears the column.

Nullable: the column is meaningful only when set; NULL means "no
known missed window since the last successful renewal." Indexed
partially on NOT NULL so the backfill scan stays cheap as the
total subscription count grows.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE graph_subscriptions "
        "ADD COLUMN last_missed_at TIMESTAMPTZ"
    )
    op.execute(
        "CREATE INDEX ix_graph_subscriptions_last_missed_at "
        "ON graph_subscriptions (last_missed_at) "
        "WHERE last_missed_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_graph_subscriptions_last_missed_at"
    )
    op.execute(
        "ALTER TABLE graph_subscriptions DROP COLUMN IF EXISTS last_missed_at"
    )
