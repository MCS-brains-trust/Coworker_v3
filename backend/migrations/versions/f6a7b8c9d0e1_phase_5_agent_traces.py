"""Phase 5: agent_traces + agent_trace_steps for the orchestrator

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-13 03:00:00.000000

Two-table addition:

- ``agent_traces``: one row per OrchestratorEngine.run() call. Carries
  the goal, status (running/completed/budget_exhausted/max_iterations/
  failed), token totals, cost in cents, num_steps, and an optional
  ``parent_trace_id`` for Phase 8 specialist sub-traces.
- ``agent_trace_steps``: one row per atomic event in a trace (model
  call, tool call, tool result, soft error). ``step_index`` is the
  0-based position; unique-within-trace enforces consistency for
  ``coworker debug replay-trace <id>``.

The ``content`` JSONB column is the source of truth for the step's
full content — Claude's request / response, tool inputs, tool
outputs. Cost and timing live in dedicated columns for fast
aggregation; content is for replay.

RLS+FORCE on both tables on the same ``app.firm_id`` GUC pattern
as Phase 2.1 / 4A. ``agent_trace_steps.firm_id`` is denormalised
from ``agent_traces.firm_id`` so the per-row RLS check doesn't
need a join.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = ("agent_traces", "agent_trace_steps")
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
        CREATE TABLE agent_traces (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            parent_trace_id UUID REFERENCES agent_traces(id) ON DELETE CASCADE,
            plugin_name VARCHAR(100),
            goal TEXT NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'running',
            completion_reason VARCHAR(100),
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at TIMESTAMPTZ,
            total_input_tokens BIGINT NOT NULL DEFAULT 0,
            total_output_tokens BIGINT NOT NULL DEFAULT 0,
            total_cost_cents BIGINT NOT NULL DEFAULT 0,
            num_steps INTEGER NOT NULL DEFAULT 0,
            metadata_ JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_agent_traces_firm_started ON agent_traces "
        "(firm_id, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_agent_traces_firm_status ON agent_traces "
        "(firm_id, status)"
    )
    op.execute(
        "CREATE INDEX ix_agent_traces_parent ON agent_traces "
        "(parent_trace_id) WHERE parent_trace_id IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE agent_trace_steps (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            trace_id UUID NOT NULL REFERENCES agent_traces(id) ON DELETE CASCADE,
            step_index INTEGER NOT NULL,
            step_type VARCHAR(50) NOT NULL,
            model VARCHAR(100),
            tool_name VARCHAR(100),
            input_tokens BIGINT,
            output_tokens BIGINT,
            cost_cents BIGINT NOT NULL DEFAULT 0,
            duration_ms INTEGER,
            is_error BOOLEAN NOT NULL DEFAULT FALSE,
            content JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT agent_trace_steps_step_index_unique
                UNIQUE (trace_id, step_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_agent_trace_steps_trace ON agent_trace_steps "
        "(trace_id, step_index)"
    )
    op.execute(
        "CREATE INDEX ix_agent_trace_steps_firm_tool ON agent_trace_steps "
        "(firm_id, tool_name) WHERE tool_name IS NOT NULL"
    )

    for table in _TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in _TABLES:
        for op_name in ("select", "insert", "update", "delete"):
            op.execute(
                f"DROP POLICY IF EXISTS "
                f"{table}_firm_isolation_{op_name} ON {table}"
            )
    op.execute("DROP TABLE IF EXISTS agent_trace_steps CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_traces CASCADE")
