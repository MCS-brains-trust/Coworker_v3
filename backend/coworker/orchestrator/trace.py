"""Trace persistence helpers — ``AgentTraceWriter``.

The engine constructs one writer per ``OrchestratorEngine.run()``
call. Every Claude completion, every tool invocation, and every
soft error flows through the writer's record methods, which
append a step row and update the parent trace's running totals in
the same transaction.

Lifecycle::

    writer = AgentTraceWriter(session, firm_id)
    trace_id = await writer.start_trace(goal="draft a reply", ...)
    for ... in loop:
        await writer.record_model_call(...)
        if tool_calls:
            for call in tool_calls:
                await writer.record_tool_call(...)
                await writer.record_tool_result(...)
    await writer.finish_trace(status="completed")

The writer owns step indexing. It increments locally so a single
``run()`` is atomic with respect to ordering, even if multiple
calls run concurrently (which they don't today, but the property
is cheap to maintain).
"""
import datetime as _dt
import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.db.models import AgentTrace, AgentTraceStep

_STATUS_RUNNING = "running"


class TraceNotStartedError(RuntimeError):
    """Raised when record_* is called before start_trace."""


class AgentTraceWriter:
    """Writes trace + step rows under firm_context.

    Construct per run with the same ``session`` the engine uses
    for its own queries. The writer doesn't commit on every step —
    the engine batches commits at sensible points (typically after
    each loop iteration) so a crash doesn't lose a partial trace.
    Each record method flushes so the rows are visible to a
    concurrent read within the same transaction; the engine
    commits explicitly.
    """

    def __init__(
        self,
        session: AsyncSession,
        firm_id: uuid.UUID,
    ) -> None:
        self._session = session
        self._firm_id = firm_id
        self._trace_id: uuid.UUID | None = None
        self._step_index: int = 0

    @property
    def trace_id(self) -> uuid.UUID:
        """The current trace id. Raises if start_trace hasn't run."""
        if self._trace_id is None:
            raise TraceNotStartedError(
                "AgentTraceWriter.start_trace must be called before "
                "accessing trace_id"
            )
        return self._trace_id

    async def start_trace(
        self,
        *,
        goal: str,
        plugin_name: str | None = None,
        parent_trace_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Insert the parent trace row. Returns the new trace id."""
        trace = AgentTrace(
            firm_id=self._firm_id,
            parent_trace_id=parent_trace_id,
            plugin_name=plugin_name,
            goal=goal,
            status=_STATUS_RUNNING,
            metadata_=metadata or {},
        )
        self._session.add(trace)
        await self._session.flush()
        self._trace_id = trace.id
        return trace.id

    async def record_model_call(
        self,
        *,
        model: str,
        request_messages: list[dict[str, Any]],
        response_content: list[dict[str, Any]],
        input_tokens: int,
        output_tokens: int,
        cost_cents: int,
        duration_ms: int,
        stop_reason: str | None = None,
    ) -> uuid.UUID:
        """Append a model_call step and update trace totals.

        Returns the new step's id. ``request_messages`` and
        ``response_content`` land verbatim in ``content`` for
        exact replay. Tokens + cost roll up to the trace.
        """
        return await self._insert_step(
            step_type="model_call",
            model=model,
            tool_name=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cents=cost_cents,
            duration_ms=duration_ms,
            is_error=False,
            content={
                "request_messages": request_messages,
                "response_content": response_content,
                "stop_reason": stop_reason,
            },
        )

    async def record_tool_call(
        self,
        *,
        tool_name: str,
        tool_use_id: str,
        input_data: dict[str, Any],
    ) -> uuid.UUID:
        """Append a tool_call step. No token cost (the model's call
        cost was recorded by the preceding model_call step).
        """
        return await self._insert_step(
            step_type="tool_call",
            model=None,
            tool_name=tool_name,
            input_tokens=None,
            output_tokens=None,
            cost_cents=0,
            duration_ms=None,
            is_error=False,
            content={
                "tool_use_id": tool_use_id,
                "input": input_data,
            },
        )

    async def record_tool_result(
        self,
        *,
        tool_name: str,
        tool_use_id: str,
        result: Any,
        is_error: bool,
        duration_ms: int,
        cost_cents: int = 0,
        error_class: str | None = None,
    ) -> uuid.UUID:
        """Append a tool_result step.

        ``result`` lands in ``content.result`` as-is (must be JSON-
        serialisable; the engine ensures that). ``is_error`` reflects
        whether Claude should see the result as an error (the
        engine sets this from a raised ``ToolError`` or a connector
        exception). ``error_class`` records the exception class
        when is_error is True so the trace shows whether the failure
        was a ToolError vs a ConnectorAuthError vs something else.

        ``cost_cents`` is for tools that themselves cost money —
        a Voyage embedding tool, a Sonnet rerank, a vision pipeline
        call. The engine accumulates them into the trace total.
        """
        return await self._insert_step(
            step_type="tool_result",
            model=None,
            tool_name=tool_name,
            input_tokens=None,
            output_tokens=None,
            cost_cents=cost_cents,
            duration_ms=duration_ms,
            is_error=is_error,
            content={
                "tool_use_id": tool_use_id,
                "result": result,
                "error_class": error_class,
            },
        )

    async def record_error(
        self,
        *,
        message: str,
        error_class: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Append an error step for non-fatal engine-level failures.

        Used for things the model never sees: a tool handler that
        raised an unrelated exception (caught and recovered from),
        a parse failure, a budget warning, etc. The trace records
        these for ops visibility without disturbing the model's
        view of the loop.
        """
        return await self._insert_step(
            step_type="error",
            model=None,
            tool_name=None,
            input_tokens=None,
            output_tokens=None,
            cost_cents=0,
            duration_ms=None,
            is_error=True,
            content={
                "message": message,
                "error_class": error_class,
                "metadata": metadata or {},
            },
        )

    async def finish_trace(
        self,
        *,
        status: str,
        completion_reason: str | None = None,
    ) -> None:
        """Mark the trace ended.

        Sets ``status``, ``completion_reason``, and ``ended_at``.
        Idempotent — calling twice on the same trace is a no-op on
        the second call (the engine guarantees one finish_trace per
        run, but the safety helps if a wrapper crashes mid-cleanup).
        """
        trace_id = self.trace_id  # raises if not started
        now = _dt.datetime.now(_dt.UTC)
        await self._session.execute(
            update(AgentTrace)
            .where(AgentTrace.id == trace_id)
            .where(AgentTrace.status == _STATUS_RUNNING)
            .values(
                status=status,
                completion_reason=completion_reason,
                ended_at=now,
            )
        )
        await self._session.flush()

    async def _insert_step(
        self,
        *,
        step_type: str,
        model: str | None,
        tool_name: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_cents: int,
        duration_ms: int | None,
        is_error: bool,
        content: dict[str, Any],
    ) -> uuid.UUID:
        """Shared insert path that maintains trace totals.

        Bumps ``self._step_index`` so the next step is positioned
        one higher. The single UPDATE on the trace keeps the
        totals consistent — readers see correct cumulative numbers
        as soon as the step is flushed.
        """
        if self._trace_id is None:
            raise TraceNotStartedError(
                f"AgentTraceWriter.start_trace must be called before "
                f"recording {step_type}"
            )

        step = AgentTraceStep(
            firm_id=self._firm_id,
            trace_id=self._trace_id,
            step_index=self._step_index,
            step_type=step_type,
            model=model,
            tool_name=tool_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cents=cost_cents,
            duration_ms=duration_ms,
            is_error=is_error,
            content=content,
        )
        self._session.add(step)

        await self._session.execute(
            update(AgentTrace)
            .where(AgentTrace.id == self._trace_id)
            .values(
                total_input_tokens=(
                    AgentTrace.total_input_tokens + (input_tokens or 0)
                ),
                total_output_tokens=(
                    AgentTrace.total_output_tokens + (output_tokens or 0)
                ),
                total_cost_cents=(
                    AgentTrace.total_cost_cents + cost_cents
                ),
                num_steps=AgentTrace.num_steps + 1,
            )
        )
        await self._session.flush()

        self._step_index += 1
        return step.id
