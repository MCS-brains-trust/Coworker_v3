"""Phase 9-4: approval_items status enum gains 'sent' + 'dispatch_failed'

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-14 14:10:00.000000

Extends the approval state machine for the dispatch step:

    pending -> approved -> sent          (happy path)
    pending -> approved -> dispatch_failed -> sent
                                         (transient Graph failure
                                          then a later retry succeeds)
    pending -> rejected                  (terminal)

The dispatch worker (``coworker.workers.dispatch``) scans for
``status='approved'`` items, creates the draft in the user's
Outlook Drafts folder via Graph, and transitions to ``sent`` on
success or ``dispatch_failed`` on a transient error (so the next
sweep retries). The terminal observer-visible state for a
successfully-dispatched email draft is ``sent``; the principal
still has to click Send in Outlook (CLAUDE.md's "drafts only"
rule).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE approval_items "
        "DROP CONSTRAINT approval_items_status_check"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "ADD CONSTRAINT approval_items_status_check CHECK ("
        "status IN ("
        "'pending', 'approved', 'rejected', 'sent', 'dispatch_failed'"
        "))"
    )
    # Partial index for the dispatch worker's scan path.
    op.execute(
        "CREATE INDEX ix_approval_items_firm_dispatchable "
        "ON approval_items (firm_id, updated_at) "
        "WHERE status IN ('approved', 'dispatch_failed')"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_approval_items_firm_dispatchable"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "DROP CONSTRAINT approval_items_status_check"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "ADD CONSTRAINT approval_items_status_check CHECK ("
        "status IN ('pending', 'approved', 'rejected'))"
    )
