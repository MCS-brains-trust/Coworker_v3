"""sandbox firm columns

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-05-15 11:50:00.000000

Pre-pilot pull-forward Task 1. Adds ``is_sandbox`` and
``sandbox_outbound_catchall`` to ``firms`` so we can drive the
whole pipeline against synthetic data without leaking outbound
sends to real MC&S clients.

The connector-layer rerouting (Graph mail / FuseSign envelopes)
reads these columns at write time; the rerouting fires even
when ``shadow_mode=False`` — sandbox bypasses shadow but never
reaches a real recipient.

CHECK constraint: when ``is_sandbox=TRUE``,
``sandbox_outbound_catchall`` must be set. Modeled at the DB
layer (not Python) because the CLI bootstrap path inserts via
SQLAlchemy core in places where a model-level validator would
not fire.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: str | Sequence[str] | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE firms "
        "ADD COLUMN is_sandbox BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE firms "
        "ADD COLUMN sandbox_outbound_catchall TEXT"
    )
    op.execute(
        "ALTER TABLE firms "
        "ADD CONSTRAINT firms_sandbox_catchall_check "
        "CHECK (NOT is_sandbox OR sandbox_outbound_catchall IS NOT NULL)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE firms "
        "DROP CONSTRAINT IF EXISTS firms_sandbox_catchall_check"
    )
    op.execute(
        "ALTER TABLE firms "
        "DROP COLUMN IF EXISTS sandbox_outbound_catchall"
    )
    op.execute(
        "ALTER TABLE firms "
        "DROP COLUMN IF EXISTS is_sandbox"
    )
