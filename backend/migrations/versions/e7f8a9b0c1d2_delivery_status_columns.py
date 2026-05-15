"""approval_items delivery status columns

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-05-15 13:30:00.000000

Pre-pilot pull-forward Task 3. Extends ``approval_items`` with
the columns that drive bounce / non-delivery report (NDR)
handling and the 4h delivery-confirmation sweep:

- ``delivery_status`` — 'unknown' (default) -> 'sent' (dispatcher
  created the draft) -> 'delivered' (4h elapsed without NDR) or
  'failed' (NDR received). Distinct from ``status`` which tracks
  the approval-side state machine.
- ``delivery_status_detail`` — free-text reason on transitions
  ('shadow_blocked', NDR diagnostic-code, etc.) for the trace.
- ``delivery_status_updated_at`` — when ``delivery_status`` last
  changed. The 4h sweep uses this as the cutoff rather than
  ``updated_at`` which fluctuates on every approval-side write.
- ``executed_internet_message_id`` — the Message-ID header value
  Graph proposed for the draft when it was created. The NDR
  correlator (``delivery_status_handler``) matches this against
  the original-message Message-ID parsed from an incoming NDR's
  References / In-Reply-To headers.

Index on (firm_id, delivery_status, delivery_status_updated_at)
backs the sweep's ``WHERE delivery_status='sent' AND ...`` scan.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE approval_items "
        "ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'unknown'"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "ADD COLUMN delivery_status_detail TEXT"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "ADD COLUMN delivery_status_updated_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "ADD COLUMN executed_internet_message_id TEXT"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "ADD CONSTRAINT approval_items_delivery_status_check CHECK ("
        "delivery_status IN ('unknown', 'sent', 'delivered', 'failed')"
        ")"
    )
    op.execute(
        "CREATE INDEX ix_approval_items_firm_delivery_status "
        "ON approval_items "
        "(firm_id, delivery_status, delivery_status_updated_at)"
    )
    # NDR correlation: the delivery_status_handler plugin matches
    # an incoming NDR's referenced Message-ID against the
    # executed_internet_message_id of an existing approval_items
    # row. The lookup is scoped by firm (RLS) and indexed on the
    # message id alone.
    op.execute(
        "CREATE INDEX ix_approval_items_executed_internet_message_id "
        "ON approval_items (executed_internet_message_id) "
        "WHERE executed_internet_message_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS "
        "ix_approval_items_executed_internet_message_id"
    )
    op.execute(
        "DROP INDEX IF EXISTS ix_approval_items_firm_delivery_status"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "DROP CONSTRAINT IF EXISTS approval_items_delivery_status_check"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "DROP COLUMN IF EXISTS executed_internet_message_id"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "DROP COLUMN IF EXISTS delivery_status_updated_at"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "DROP COLUMN IF EXISTS delivery_status_detail"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "DROP COLUMN IF EXISTS delivery_status"
    )
