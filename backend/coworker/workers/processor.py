"""Plugin event processor — fans one event out to its listening plugins.

The worker pool consumes ``queue:plugin_events`` and calls
``process_event`` for each item. The processor:

1. Looks up the firm and confirms it exists.
2. For every plugin whose ``triggers`` include the event's trigger,
   opens a fresh session+transaction (so a rollback in one plugin
   doesn't expire ORM state used by the next), resolves a
   ``GraphContext`` for email_received events, and calls
   ``execute_plugin``.
3. Returns the list of RunResults. Plugins not installed for the
   firm are recorded as ``skipped`` (PluginNotInstalledError /
   PluginDisabledError / PluginConfigError); any other in-engine
   failure is captured as ``crashed`` and the loop continues.

The BRPOP wrapper (``coworker.workers.loop.run_worker``) drives
this — keeping the per-event logic isolated lets tests exercise
it without a real worker process.
"""
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coworker.connectors.anthropic_client import AnthropicClient
from coworker.db.models import Firm, User
from coworker.db.session import firm_context
from coworker.graph.context import GraphContext
from coworker.graph.user_context import resolve_user_graph_context
from coworker.orchestrator.engine import (
    ModelCaller,
    OrchestratorEngine,
    RunResult,
)
from coworker.orchestrator.tools import ToolRegistry
from coworker.plugins.base import OrchestratorPlugin, PluginRegistry, PluginRun
from coworker.plugins.executor import (
    PluginConfigError,
    PluginDisabledError,
    PluginNotInstalledError,
    execute_plugin,
)
from coworker.workers.dedup import PluginRunDedup
from coworker.workers.plugin_queue import PluginEvent

if TYPE_CHECKING:
    from coworker.memory.embeddings import Embedder

# Factory: given a Firm row, return an AnthropicClient for it. The
# default (``AnthropicClient(firm_id=str(firm.id))``) uses the
# platform-shared API key from Settings; alternatives can read
# firm.anthropic_api_key_ciphertext for per-firm BYO keys.
AnthropicFactory = Callable[[Firm], AnthropicClient]


def _default_anthropic_factory(firm: Firm) -> AnthropicClient:
    return AnthropicClient(firm_id=str(firm.id))


@dataclass
class ProcessResult:
    """Outcome of ``process_event`` for one (event, fan-out) pair.

    ``run_results`` is one entry per plugin that ran. ``skipped``
    is one entry per plugin we considered but didn't run (not
    installed, disabled, config error). ``firm_not_found`` is a
    short-circuit case when the firm row no longer exists for the
    event's firm_id — typically a deleted firm.
    """

    event_id: uuid.UUID
    firm_not_found: bool = False
    run_results: list[RunResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


async def process_event(
    event: PluginEvent,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    plugin_registry: PluginRegistry,
    tool_registry: ToolRegistry,
    model_caller: ModelCaller | None = None,
    embedder: "Embedder | None" = None,
    anthropic_factory: AnthropicFactory = _default_anthropic_factory,
    dedup: PluginRunDedup | None = None,
) -> ProcessResult:
    """Fan one event out to every plugin listening to its trigger.

    Args:
        event: the queued event.
        sessionmaker: factory the processor uses to open the
            session inside ``firm_context``. Caller owns the
            sessionmaker's engine lifecycle.
        plugin_registry: every known plugin. Filtered per-call by
            the event's trigger.
        tool_registry: the full builtin + plugin-contributed tool
            catalogue. Sliced per-plugin in execute_plugin.
        model_caller: optional override for the engine's model
            caller. Tests pass a ScriptedModelCaller. When None
            (production), the processor uses each firm's
            ``AnthropicClient.complete_tool_use`` so PII scrubbing
            and token metering are correctly scoped per firm.
        embedder: shared embedder for memory_query, etc. Optional.
        anthropic_factory: builds a per-firm AnthropicClient.
            Default uses the platform-shared key from Settings.

    Returns:
        ``ProcessResult`` summarising what ran.
    """
    # event.trigger is a free-string in the dataclass (it survived
    # JSON-roundtrip from the queue payload); cast to the Trigger
    # Literal for the registry call. Unknown triggers will simply
    # return no candidates.
    candidates = plugin_registry.filter_by_trigger(event.trigger)  # type: ignore[arg-type]
    if not candidates:
        logger.debug(
            "worker no plugins listening trigger={} event={}",
            event.trigger,
            event.event_id,
        )
        return ProcessResult(event_id=event.event_id)

    # Lookup pass: confirm the firm exists. Closes the session
    # immediately so each plugin run gets a fresh transaction —
    # an exception in one plugin can't poison sibling iterations.
    async with sessionmaker() as session, firm_context(event.firm_id):
        firm_exists = (
            await session.execute(
                select(Firm.id).where(Firm.id == event.firm_id)
            )
        ).scalar_one_or_none() is not None

    if not firm_exists:
        logger.warning(
            "worker firm not found firm_id={} event={}",
            event.firm_id,
            event.event_id,
        )
        return ProcessResult(event_id=event.event_id, firm_not_found=True)

    result = ProcessResult(event_id=event.event_id)
    for plugin_cls in candidates:
        await _run_one_plugin(
            plugin_cls,
            event=event,
            sessionmaker=sessionmaker,
            tool_registry=tool_registry,
            model_caller=model_caller,
            embedder=embedder,
            anthropic_factory=anthropic_factory,
            dedup=dedup,
            result=result,
        )
    return result


async def _run_one_plugin(
    plugin_cls: type[OrchestratorPlugin],
    *,
    event: PluginEvent,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_registry: ToolRegistry,
    model_caller: ModelCaller | None,
    embedder: "Embedder | None",
    anthropic_factory: AnthropicFactory,
    dedup: PluginRunDedup | None,
    result: ProcessResult,
) -> None:
    """Execute a single plugin in its own session/transaction.

    Each plugin gets a fresh session so a rollback in one doesn't
    expire ORM objects used by the next. For email_received events,
    ``graph_ctx`` is resolved from the notification's mailbox-owner
    and handed to ``execute_plugin`` so the plugin's email tools
    can read the triggering message.
    """
    # Dedup: ask before opening the session. Saves DB work when
    # a duplicate notification is in flight. The dedup_key is
    # event+plugin specific so different plugins on the same
    # event each get one shot.
    if dedup is not None and not await dedup.claim(
        event.firm_id, plugin_cls.name, event,
    ):
        result.skipped.append(f"{plugin_cls.name}: deduped")
        return

    async with sessionmaker() as session, firm_context(event.firm_id):
        firm = (
            await session.execute(
                select(Firm).where(Firm.id == event.firm_id)
            )
        ).scalar_one()

        graph_ctx: GraphContext | None = None
        if event.trigger in ("email_received", "calendar_event"):
            # Both triggers carry a ``resource`` of the shape
            # ``users/{azure_object_id}/...``. The mailbox-owner
            # lookup is identical for either.
            graph_ctx = await _resolve_graph_ctx_for_email(
                session, firm=firm, event=event
            )

        anthropic = anthropic_factory(firm)
        # Default to the per-firm AnthropicClient's tool-use call so
        # PII scrubbing + token metering are firm-scoped. Tests
        # override this with a ScriptedModelCaller. The ignore is
        # because complete_tool_use returns ToolUseResult, which is
        # shape-compatible with ModelCallResult but not the same
        # nominal type (documented as "drop-in" in its docstring).
        caller: ModelCaller = (
            model_caller
            if model_caller is not None
            else anthropic.complete_tool_use  # type: ignore[assignment]
        )
        engine = OrchestratorEngine(model_caller=caller)

        run = PluginRun(
            plugin_name=plugin_cls.name,
            firm_id=firm.id,
            trigger=event.trigger,  # type: ignore[arg-type]
            event_data=event.event_data,
            config={},
            is_dry_run=False,
            requested_at=event.enqueued_at,
        )

        try:
            run_result = await execute_plugin(
                plugin_cls,
                run,
                engine=engine,
                tool_registry=tool_registry,
                session=session,
                firm=firm,
                anthropic=anthropic,
                embedder=embedder,
                graph_ctx=graph_ctx,
            )
            await session.commit()
        except PluginNotInstalledError:
            await session.rollback()
            result.skipped.append(f"{plugin_cls.name}: not_installed")
            return
        except PluginDisabledError:
            await session.rollback()
            result.skipped.append(f"{plugin_cls.name}: disabled")
            return
        except PluginConfigError as exc:
            await session.rollback()
            logger.warning(
                "worker plugin config error plugin={} event={} err={}",
                plugin_cls.name,
                event.event_id,
                exc,
            )
            result.skipped.append(f"{plugin_cls.name}: config_error")
            return
        except Exception:
            await session.rollback()
            logger.exception(
                "worker plugin execution crashed plugin={} event={}",
                plugin_cls.name,
                event.event_id,
            )
            result.skipped.append(f"{plugin_cls.name}: crashed")
            return

        result.run_results.append(run_result)


async def _resolve_graph_ctx_for_email(
    session: AsyncSession,
    *,
    firm: Firm,
    event: PluginEvent,
) -> GraphContext | None:
    """Resolve a mailbox-owner GraphContext from an email_received event.

    Microsoft's notification resource path is
    ``users/{azure_object_id}/messages/{message_id}``. We extract
    azure_object_id, look up the matching User, then delegate to
    ``resolve_user_graph_context`` for token-refresh + context
    construction. Returns None if any step fails — the worker
    continues so the trace still records what was attempted.
    """
    resource = event.event_data.get("resource")
    if not isinstance(resource, str) or not resource.startswith("users/"):
        return None
    parts = resource.split("/", 4)
    if len(parts) < 2:
        return None
    azure_oid = parts[1]

    user = (
        await session.execute(
            select(User)
            .where(User.firm_id == firm.id)
            .where(User.azure_object_id == azure_oid)
        )
    ).scalar_one_or_none()
    if user is None:
        logger.warning(
            "worker user not found firm_id={} azure_oid={}",
            firm.id,
            azure_oid,
        )
        return None

    return await resolve_user_graph_context(session, firm=firm, user=user)
