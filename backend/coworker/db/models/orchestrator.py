"""Agent trace models — one row per run, many per step.

These are the load-bearing observability surface for Phase 5+.
Every Claude call + every tool invocation lands in
``agent_trace_steps``; the parent ``agent_traces`` row holds
totals and status. ``coworker debug replay-trace <id>`` (planned)
reconstructs the full transcript by joining trace -> steps order
by ``step_index``.

Specialist sub-traces (Phase 8) reuse the same tables — a
specialist consultation creates a new ``agent_traces`` row with
``parent_trace_id`` set to the calling agent's trace and (later)
``metadata_.specialist_prompt_version_id`` pinning the prompt.
"""
import datetime as _dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base


class AgentTrace(Base):
    """One orchestrator run.

    ``status`` values (set by the engine):

    - ``running`` — the run is in progress
    - ``completed`` — model returned a final response without a tool
      call
    - ``budget_exhausted`` — per-context budget cap was hit
    - ``max_iterations`` — hit the loop's max-iterations cap
    - ``failed`` — a connector error propagated out of the loop

    ``parent_trace_id`` is null for top-level agent runs; set to the
    parent's id for Phase 8 specialist sub-traces.
    """

    __tablename__ = "agent_traces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_traces.id", ondelete="CASCADE"),
        nullable=True,
    )
    plugin_name: Mapped[str | None] = mapped_column(String(100))
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="running", server_default="running"
    )
    completion_reason: Mapped[str | None] = mapped_column(String(100))

    started_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    total_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    total_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    total_cost_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    num_steps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    metadata_: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class AgentTraceStep(Base):
    """One atomic event inside a trace.

    ``step_type`` values (engine-set):

    - ``model_call`` — a Claude completion call
    - ``tool_call`` — Claude requested a tool invocation
    - ``tool_result`` — our execution of that tool produced this result
    - ``error`` — a recoverable error that didn't end the loop

    ``content`` carries the verbatim payload (messages array, tool
    input dict, tool output, error string) so replay reproduces the
    exact sequence. Token + cost columns are denormalised from
    ``content`` for fast aggregation.

    ``firm_id`` is denormalised from the parent trace so the per-row
    RLS predicate doesn't need a join.
    """

    __tablename__ = "agent_trace_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_traces.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(50), nullable=False)

    model: Mapped[str | None] = mapped_column(String(100))
    tool_name: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    is_error: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "trace_id", "step_index",
            name="agent_trace_steps_step_index_unique",
        ),
    )
