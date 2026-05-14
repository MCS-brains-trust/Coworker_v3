"""Phase 9-1: approval_items

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-05-14 13:50:00.000000

The principal-facing review queue. Every plugin-generated side
effect (email draft, client_interaction proposal, entity change,
etc.) writes a row here when produced under shadow mode or when
its confidence is below the per-category autonomy threshold.
The web frontend (Phase 10) renders these for the principal to
approve / edit / reject; for now the table + helpers are exercised
through the CLI and tests.

State machine (this commit):
    pending -> approved
    pending -> rejected

Future transitions deferred to later sub-phases:
    pending -> edited (Phase 9-3 in-place edit + re-review)
    approved -> sent (Phase 9-4 dispatch confirmation)
    pending -> expired (Phase 9-5 TTL sweep)

RLS+FORCE so the principal's "my firm's pending queue" query
isolates correctly without explicit firm_id filtering. The
state machine itself is enforced by a CHECK constraint plus
application-side guards.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RLS_MATCH = "firm_id = NULLIF(current_setting('app.firm_id', true), '')::uuid"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for action in ("select", "insert", "update", "delete"):
        if action == "insert":
            op.execute(
                f"CREATE POLICY {table}_firm_isolation_{action} ON {table} "
                f"FOR INSERT WITH CHECK ({_RLS_MATCH})"
            )
        elif action == "update":
            op.execute(
                f"CREATE POLICY {table}_firm_isolation_{action} ON {table} "
                f"FOR UPDATE USING ({_RLS_MATCH}) WITH CHECK ({_RLS_MATCH})"
            )
        else:
            op.execute(
                f"CREATE POLICY {table}_firm_isolation_{action} ON {table} "
                f"FOR {action.upper()} USING ({_RLS_MATCH})"
            )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE approval_items (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            trace_id UUID REFERENCES agent_traces(id) ON DELETE SET NULL,
            plugin_name VARCHAR(100) NOT NULL,
            category VARCHAR(50) NOT NULL,
            summary VARCHAR(500) NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            decided_at TIMESTAMPTZ,
            decided_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            decision_notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT approval_items_status_check CHECK (
                status IN (
                    'pending', 'approved', 'rejected'
                )
            )
        )
        """
    )
    # Pending-queue scan: the principal's review page loads pending
    # items ordered by recency, scoped to one firm. Partial index
    # keeps the scan O(pending) not O(total).
    op.execute(
        "CREATE INDEX ix_approval_items_firm_pending "
        "ON approval_items (firm_id, created_at DESC) "
        "WHERE status = 'pending'"
    )
    op.execute(
        "CREATE INDEX ix_approval_items_trace "
        "ON approval_items (trace_id) WHERE trace_id IS NOT NULL"
    )
    _enable_rls("approval_items")


def downgrade() -> None:
    for op_name in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"approval_items_firm_isolation_{op_name} "
            f"ON approval_items"
        )
    op.execute("DROP TABLE IF EXISTS approval_items CASCADE")
