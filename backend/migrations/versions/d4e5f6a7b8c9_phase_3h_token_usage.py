"""Phase 3H: token_usage table for permanent token-spend retention

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-12 18:00:00.000000

Adds the permanent home for daily per-firm / per-model token counters.
Phase 3B-4 records counters in Redis (35-day TTL) so the orchestrator's
cost-guard logic has sub-millisecond access; Phase 3H adds the nightly
flush + Postgres home for permanent retention so monthly reports
covering arbitrary windows are possible.

Schema choices:

- Composite PK ``(firm_id, model, day)`` reflects the natural key.
  No surrogate id — there's nothing to reference these rows by other
  than the tuple, and inserts use UPSERT on the PK.
- ``BigInteger`` counters because heavy firms running specialists
  with extended thinking can clear 100M tokens/month per model; INT
  has headroom for many months but BIGINT removes the question.
- ``server_default='0'`` so the UPSERT can leave columns implicit
  when only some are being added (e.g. a count_tokens call increments
  ``input_tokens`` and ``calls`` but not ``output_tokens``).
- RLS+FORCE with the four policies from Phase 2.1, on the same
  ``app.firm_id`` GUC pattern.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RLS_MATCH = (
    "firm_id = NULLIF(current_setting('app.firm_id', true), '')::uuid"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE token_usage (
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            model VARCHAR(100) NOT NULL,
            day DATE NOT NULL,
            input_tokens BIGINT NOT NULL DEFAULT 0,
            output_tokens BIGINT NOT NULL DEFAULT 0,
            calls BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (firm_id, model, day)
        )
        """
    )
    # Useful secondary index for the monthly-report query, which
    # filters by (firm_id, day BETWEEN ...) and groups by model.
    op.execute(
        "CREATE INDEX ix_token_usage_firm_day "
        "ON token_usage (firm_id, day DESC)"
    )

    op.execute("ALTER TABLE token_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE token_usage FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY token_usage_firm_isolation_select ON token_usage "
        f"FOR SELECT USING ({_RLS_MATCH})"
    )
    op.execute(
        f"CREATE POLICY token_usage_firm_isolation_insert ON token_usage "
        f"FOR INSERT WITH CHECK ({_RLS_MATCH})"
    )
    op.execute(
        f"CREATE POLICY token_usage_firm_isolation_update ON token_usage "
        f"FOR UPDATE USING ({_RLS_MATCH}) WITH CHECK ({_RLS_MATCH})"
    )
    op.execute(
        f"CREATE POLICY token_usage_firm_isolation_delete ON token_usage "
        f"FOR DELETE USING ({_RLS_MATCH})"
    )


def downgrade() -> None:
    # Policies are dropped automatically when the table is dropped,
    # but doing it explicitly is more legible and survives partial
    # downgrade scenarios.
    for op_name in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"token_usage_firm_isolation_{op_name} ON token_usage"
        )
    op.execute("DROP INDEX IF EXISTS ix_token_usage_firm_day")
    op.execute("DROP TABLE token_usage")
