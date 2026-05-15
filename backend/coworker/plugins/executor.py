"""``execute_plugin`` — turn a PluginRun into a finished trace.

The single entry point the scheduler / webhook receiver / manual-
trigger route invokes. Responsible for:

1. Looking up the firm's ``plugin_installations`` row to confirm
   the plugin is enabled and pick up the authoritative config +
   dry_run flag (re-reading guards against stale values in the
   caller's PluginRun).
2. Slicing the tool registry by the plugin's declared
   ``enabled_tool_categories`` and ``allow_side_effects``.
3. Constructing an ``AgentContext`` carrying budget + extended-
   thinking + embedder.
4. Starting an ``AgentTraceWriter`` with the plugin's name + run
   metadata in the trace row's metadata_ JSONB.
5. Calling ``OrchestratorEngine.run`` and returning the result.

Errors that prevent execution surface as ``PluginNotEnabledError``
/ ``PluginNotFoundError``; mid-run failures from the engine end up
in the ``RunResult.status`` field (``failed`` /
``budget_exhausted`` / ``max_iterations``).
"""
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.anthropic_client import AnthropicClient
from coworker.db.models import (
    Firm,
    PluginInstallation,
)
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.engine import OrchestratorEngine, RunResult
from coworker.orchestrator.tools import ToolRegistry
from coworker.orchestrator.trace import AgentTraceWriter
from coworker.plugins.base import OrchestratorPlugin, PluginRun

if TYPE_CHECKING:
    from coworker.graph.context import GraphContext
    from coworker.memory.embeddings import Embedder


class PluginExecutionError(Exception):
    """Base for executor-level failures.

    Failures inside the engine loop (model errors, tool errors,
    max-iterations, budget-exhausted) are NOT raised — they
    surface via ``RunResult.status``. This class is reserved for
    "we couldn't even start" conditions: plugin disabled, config
    schema mismatch, plugin not in registry.
    """


class PluginNotInstalledError(PluginExecutionError):
    """No plugin_installations row for this (firm, plugin) pair."""


class PluginDisabledError(PluginExecutionError):
    """plugin_installations.is_enabled is False."""


class PluginConfigError(PluginExecutionError):
    """plugin_installations.config fails the plugin's config_schema."""


async def execute_plugin(
    plugin_cls: type[OrchestratorPlugin],
    run: PluginRun,
    *,
    engine: OrchestratorEngine,
    tool_registry: ToolRegistry,
    session: AsyncSession,
    firm: Firm,
    anthropic: AnthropicClient,
    embedder: "Embedder | None" = None,
    graph_ctx: "GraphContext | None" = None,
) -> RunResult:
    """Execute one plugin run end-to-end.

    Args:
        plugin_cls: the plugin to run. Caller has already looked up
            the right class from the registry.
        run: PluginRun with the triggering event. The executor
            re-reads ``is_dry_run`` and ``config`` from the DB to
            ensure they reflect the latest firm setting; the
            ``event_data`` and ``trigger`` fields are trusted
            (the caller constructed them from the actual event).
        engine: shared OrchestratorEngine instance.
        tool_registry: the process-global registry. The executor
            slices it per-plugin.
        session: AsyncSession inside ``firm_context(firm.id)``.
        firm: the firm the run is for.
        anthropic: per-firm AnthropicClient.
        embedder: optional Embedder for tools that need it.
        graph_ctx: optional GraphContext for plugins that hold
            Microsoft Graph user-context tools (read_email,
            send_teams_message, …). Resolved at the entry layer
            (webhook receiver / worker processor) from the event's
            mailbox-owner user; None for triggers / plugins that
            don't need Graph access.

    Returns:
        RunResult from the engine. status / completion_reason
        carry the loop's outcome.

    Raises:
        PluginNotInstalledError: no installation row.
        PluginDisabledError: installation row has is_enabled=False.
        PluginConfigError: installation.config doesn't validate
            against the plugin's config_schema.
    """
    if firm.id != run.firm_id:
        raise PluginExecutionError(
            f"firm.id ({firm.id}) does not match run.firm_id ({run.firm_id})"
        )

    installation = await _load_installation(
        session, firm_id=firm.id, plugin_name=plugin_cls.name
    )

    if installation is None:
        raise PluginNotInstalledError(
            f"plugin {plugin_cls.name!r} is not installed for firm {firm.id}"
        )
    if not installation.is_enabled:
        raise PluginDisabledError(
            f"plugin {plugin_cls.name!r} is disabled for firm {firm.id}"
        )

    try:
        plugin_cls.config_schema.model_validate(installation.config)
    except ValidationError as exc:
        raise PluginConfigError(
            f"plugin {plugin_cls.name!r} config failed validation: "
            f"{exc.errors()}"
        ) from exc

    # Re-read authoritative flags from DB rather than trust the
    # caller's PluginRun snapshot.
    effective_run = PluginRun(
        plugin_name=run.plugin_name,
        firm_id=run.firm_id,
        trigger=run.trigger,
        event_data=run.event_data,
        config=installation.config,
        is_dry_run=installation.is_dry_run,
        requested_at=run.requested_at,
    )

    # Slice the registry: only the plugin's categories, plus the
    # side-effect filter the plugin's flag + the installation's
    # dry-run flag together imply.
    exclude_writes = effective_run.is_dry_run or not plugin_cls.allow_side_effects
    plugin_tools = tool_registry.filter_by_categories(
        set(plugin_cls.enabled_tool_categories),
        exclude_side_effects=exclude_writes,
    )

    writer = AgentTraceWriter(session, firm.id)

    # Pre-pilot Task 2: capture goal-text sanitiser warnings if the
    # plugin exposes them. Plugins that compose their own goal
    # without sanitisation just return the string as before; those
    # that opt into ``compose_goal_with_warnings`` (smart_responder
    # today) get warnings stamped on the trace metadata so the
    # principal-side trace view shows what tripped during assembly.
    compose_with_warnings = getattr(
        plugin_cls, "compose_goal_with_warnings", None,
    )
    if compose_with_warnings is not None:
        goal_text, goal_warnings = compose_with_warnings(effective_run)
    else:
        goal_text = plugin_cls.goal(effective_run)
        goal_warnings = []

    trace_metadata: dict[str, Any] = {
        "trigger": effective_run.trigger,
        "is_dry_run": effective_run.is_dry_run,
        "plugin_version": plugin_cls.version,
        "event_data": effective_run.event_data,
    }
    if goal_warnings:
        trace_metadata["goal_sanitiser_warnings"] = goal_warnings

    trace_id = await writer.start_trace(
        goal=goal_text,
        plugin_name=plugin_cls.name,
        metadata=trace_metadata,
    )

    ctx = AgentContext(
        firm=firm,
        session=session,
        anthropic=anthropic,
        trace_id=trace_id,
        embedder=embedder,
        graph_ctx=graph_ctx,
        budget_cents=plugin_cls.cost_budget_cents,
        # email_propose_draft (and any future approval-queue tool)
        # stamps the producing plugin's name onto the approval row.
        metadata={"plugin_name": plugin_cls.name},
    )

    return await engine.run(
        ctx,
        goal=goal_text,
        tools=plugin_tools,
        writer=writer,
        system_prompt=plugin_cls.system_prompt(effective_run),
    )


async def _load_installation(
    session: AsyncSession,
    *,
    firm_id: uuid.UUID,
    plugin_name: str,
) -> PluginInstallation | None:
    return (
        await session.execute(
            select(PluginInstallation)
            .where(PluginInstallation.firm_id == firm_id)
            .where(PluginInstallation.plugin_name == plugin_name)
        )
    ).scalar_one_or_none()
