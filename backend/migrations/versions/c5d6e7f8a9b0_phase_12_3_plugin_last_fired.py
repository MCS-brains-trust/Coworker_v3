"""Phase 12-3: plugin_installations.last_fired_at

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-05-15 10:00:00.000000

Tracks when each scheduled plugin most recently fired for a
given firm. The Phase 12-3 scheduler reads this column to
compute "next scheduled time after last_fired_at" via the
plugin's cron expression and enqueues a ``scheduled`` event
when that time has passed.

Nullable: installations of plugins that have no scheduled
trigger leave this NULL. Production never reads NULL except to
treat a freshly-installed scheduled plugin's first firing as
"any cron tick after installed_at" — that case is handled in
the sweep helper without needing a NOT NULL constraint here.

Partial index on NOT NULL + plugin_name keeps the per-tick
scan O(scheduled-installations) rather than O(all-installations).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | Sequence[str] | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE plugin_installations "
        "ADD COLUMN last_fired_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE plugin_installations "
        "DROP COLUMN IF EXISTS last_fired_at"
    )
