"""Phase 8a: specialists and specialist_prompt_versions

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-05-27 09:00:00.000000

Phase 8 scaffolding for the specialist sub-agents. A specialist
is a named system prompt the orchestrator can route to for a
narrow technical domain (GST, SMSF, Division 7A, trust tax, CGT
concessions). Each specialist's prompt is versioned: an edit
inserts a new ``specialist_prompt_versions`` row and flips
``specialists.active_version_id``; the prior active row is
retired in the same transaction. That keeps a permanent history
for reproducibility — every ``agent_trace_steps.metadata`` entry
that invoked a specialist can later pin
``specialist_prompt_version_id`` to the exact text used.

Schema is fully tenant-scoped: both tables FORCE RLS with the
standard four-policy ``firm_id = NULLIF(current_setting(...),
'')::uuid`` pattern. ``firm_id`` is denormalised onto
``specialist_prompt_versions`` (not just transitive via
``specialist_id``) so the policies don't have to join.

The partial unique index ``uq_one_active_per_specialist``
guarantees at most one row with ``status='active'`` per
specialist, so the active-version pointer can never go stale via
two concurrent edits.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "f8a9b0c1d2e3"
down_revision: str | Sequence[str] | None = "e7f8a9b0c1d2"
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
    # specialists first, without the active_version_id FK target
    # (the versions table doesn't exist yet). The column is created
    # nullable; the FK is added after the versions table lands.
    op.execute(
        """
        CREATE TABLE specialists (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            description TEXT NOT NULL,
            model TEXT NOT NULL,
            extended_thinking BOOLEAN NOT NULL DEFAULT TRUE,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            active_version_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_specialist_firm_name UNIQUE (firm_id, name)
        )
        """
    )
    op.execute("CREATE INDEX ix_specialists_firm_id ON specialists (firm_id)")
    _enable_rls("specialists")

    op.execute(
        """
        CREATE TABLE specialist_prompt_versions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            specialist_id UUID NOT NULL
                REFERENCES specialists(id) ON DELETE CASCADE,
            version_number INTEGER NOT NULL,
            prompt_text TEXT NOT NULL,
            status TEXT NOT NULL,
            change_summary TEXT NOT NULL,
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_version_specialist_number
                UNIQUE (specialist_id, version_number),
            CONSTRAINT ck_version_status
                CHECK (status IN ('active', 'retired'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_specialist_prompt_versions_firm_id "
        "ON specialist_prompt_versions (firm_id)"
    )
    op.execute(
        "CREATE INDEX ix_specialist_prompt_versions_specialist_id "
        "ON specialist_prompt_versions (specialist_id)"
    )
    # At-most-one active row per specialist; the application flips
    # the previous active to 'retired' in the same transaction as
    # the new INSERT so this never conflicts under normal flow.
    op.execute(
        "CREATE UNIQUE INDEX uq_one_active_per_specialist "
        "ON specialist_prompt_versions (specialist_id) "
        "WHERE status = 'active'"
    )
    _enable_rls("specialist_prompt_versions")

    # Now the FK on specialists.active_version_id can be created.
    # SET NULL on delete because the partial unique index doesn't
    # know about the back-pointer; if a versions row is ever
    # deleted out from under the pointer, leaving NULL is safer
    # than a dangling FK.
    op.execute(
        "ALTER TABLE specialists "
        "ADD CONSTRAINT fk_specialists_active_version "
        "FOREIGN KEY (active_version_id) "
        "REFERENCES specialist_prompt_versions(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    # Drop the back-FK first so the versions table can go without
    # CASCADE noise on the pointer column.
    op.execute(
        "ALTER TABLE specialists "
        "DROP CONSTRAINT IF EXISTS fk_specialists_active_version"
    )
    for op_name in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"specialist_prompt_versions_firm_isolation_{op_name} "
            f"ON specialist_prompt_versions"
        )
    op.execute("DROP TABLE IF EXISTS specialist_prompt_versions CASCADE")
    for op_name in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"specialists_firm_isolation_{op_name} "
            f"ON specialists"
        )
    op.execute("DROP TABLE IF EXISTS specialists CASCADE")
