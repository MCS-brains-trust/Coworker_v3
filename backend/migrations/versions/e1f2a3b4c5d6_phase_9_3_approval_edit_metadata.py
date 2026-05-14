"""Phase 9-3: approval_items edit metadata

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-05-14 14:00:00.000000

Lets the principal tweak a pending item's payload before
approving — typically the email body text in a smart_responder
draft. The status stays ``pending`` while edits accumulate; the
caller commits with approve / reject when ready.

We track only the latest edit's metadata in two new columns. A
versioned history table can come later if the principal wants
to roll back; until then the existing agent_trace mechanism is
the audit trail for what produced the original payload.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE approval_items "
        "ADD COLUMN last_edited_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "ADD COLUMN last_edited_by_user_id UUID REFERENCES users(id) "
        "ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE approval_items "
        "DROP COLUMN IF EXISTS last_edited_by_user_id"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "DROP COLUMN IF EXISTS last_edited_at"
    )
