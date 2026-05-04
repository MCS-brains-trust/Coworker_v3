"""Phase 2.1: Row-Level Security policies for tenant isolation

Revision ID: c3d4e5f6a7b8
Revises: 65f1a5282b54
Create Date: 2026-05-04 16:00:00.000000

Closes audit Issue C: enables RLS and creates per-operation policies on
every tenant-scoped table so cross-firm isolation is enforced at the
database layer, not just by application-level filters.

Mechanism
---------
Each tenant-scoped table gets RLS enabled and four policies — one each
for SELECT, INSERT, UPDATE, DELETE — that filter on
``NULLIF(current_setting('app.firm_id', true), '')::uuid``. The ``true``
second argument to current_setting makes a missing GUC return NULL
rather than raise; the NULLIF wrapper additionally maps '' to NULL,
which is necessary because once a custom GUC has been touched on a
connection, both ``SET LOCAL`` post-COMMIT and ``RESET`` leave the
value at '' (empty string), not NULL. Without NULLIF, the ::uuid cast
would raise InvalidTextRepresentationError on every transaction that
runs without a firm context after the first one — breaking the
secure-by-default contract. With NULLIF, both NULL and '' map to NULL,
``firm_id = NULL`` evaluates to NULL (treated as not-visible by RLS),
and the database returns zero rows rather than every row.

For ``firms`` the predicate uses ``id`` (the row IS the firm). For
``users`` and ``audit_log`` it uses ``firm_id``.

Operational note
----------------
RLS does NOT apply to superusers, nor to roles with the BYPASSRLS
attribute. Migrations, backups, and break-glass admin work should
continue to use the postgres superuser. The application role
(``coworker``) is a normal role and IS subject to these policies.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = '65f1a5282b54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES = ("firms", "users", "audit_log")


def _firm_match_expr(table: str) -> str:
    """The SQL expression that matches a row's firm against the GUC.

    The GUC is read through NULLIF(..., '') to map empty string to NULL
    before the ::uuid cast. This is required because once a custom GUC
    has been set on a connection, SET LOCAL clears it back to '' (empty
    string) at COMMIT, not to NULL — and likewise RESET app.firm_id
    sets it to ''. Without NULLIF, '' would be cast to ::uuid and
    raise InvalidTextRepresentationError on every transaction that
    legitimately runs without a firm context. With NULLIF, both NULL
    and '' map to NULL → predicate evaluates NULL → row not visible →
    secure-by-default holds.
    """
    column = "id" if table == "firms" else "firm_id"
    return (
        f"{column} = NULLIF(current_setting('app.firm_id', true), '')::uuid"
    )


def upgrade() -> None:
    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

        match = _firm_match_expr(table)

        op.execute(
            f"CREATE POLICY {table}_firm_isolation_select ON {table} "
            f"FOR SELECT USING ({match})"
        )
        op.execute(
            f"CREATE POLICY {table}_firm_isolation_insert ON {table} "
            f"FOR INSERT WITH CHECK ({match})"
        )
        op.execute(
            f"CREATE POLICY {table}_firm_isolation_update ON {table} "
            f"FOR UPDATE USING ({match}) WITH CHECK ({match})"
        )
        op.execute(
            f"CREATE POLICY {table}_firm_isolation_delete ON {table} "
            f"FOR DELETE USING ({match})"
        )


def downgrade() -> None:
    for table in _TENANT_TABLES:
        for cmd in ("select", "insert", "update", "delete"):
            op.execute(
                f"DROP POLICY IF EXISTS {table}_firm_isolation_{cmd} ON {table}"
            )
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
