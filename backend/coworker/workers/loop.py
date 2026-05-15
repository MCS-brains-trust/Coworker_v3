"""BRPOP worker loop — long-running consumer of the plugin event queue.

A single process can run one or more concurrent ``run_worker``
tasks; multiple workers BRPOP'ing the same Redis list is safe
because Redis hands each event to exactly one consumer. The
loop:

1. Blocks on ``queue.dequeue(timeout_seconds=idle_poll_seconds)``
   so the stop signal is honoured within a bounded delay.
2. Hands each dequeued event to ``process_event``.
3. Catches and logs any exception so the loop never dies on a
   bad event — the next iteration BRPOPs the next one.

Shutdown is cooperative via an ``asyncio.Event``. The supervising
script wires SIGINT/SIGTERM to ``stop_event.set()`` before
awaiting the worker tasks.
"""
import asyncio
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coworker.orchestrator.engine import ModelCaller
from coworker.orchestrator.tools import ToolRegistry
from coworker.plugins.base import PluginRegistry
from coworker.workers.dedup import PluginRunDedup
from coworker.workers.plugin_queue import PluginEventQueue
from coworker.workers.processor import (
    AnthropicFactory,
    _default_anthropic_factory,
    process_event,
)

if TYPE_CHECKING:
    from coworker.memory.embeddings import Embedder


async def run_worker(
    *,
    queue: PluginEventQueue,
    sessionmaker: async_sessionmaker[AsyncSession],
    plugin_registry: PluginRegistry,
    tool_registry: ToolRegistry,
    stop_event: asyncio.Event,
    model_caller: ModelCaller | None = None,
    embedder: "Embedder | None" = None,
    anthropic_factory: AnthropicFactory = _default_anthropic_factory,
    dedup: PluginRunDedup | None = None,
    idle_poll_seconds: int = 5,
) -> None:
    """Consume the plugin event queue until ``stop_event`` is set.

    Args:
        queue: the shared Redis-backed event queue.
        sessionmaker: factory for per-event sessions (passed
            through to ``process_event``).
        plugin_registry: every known plugin.
        tool_registry: builtin + plugin-contributed tools.
        model_caller: optional engine model caller. When omitted
            (production), the processor uses each firm's
            ``AnthropicClient.complete_tool_use``. Tests pass a
            ScriptedModelCaller to bypass the real API.
        stop_event: when set, the loop drains its current event and
            exits cleanly. Bounded by ``idle_poll_seconds`` — the
            BRPOP timeout — so an idle worker still notices.
        embedder: shared Embedder, optional.
        anthropic_factory: per-firm AnthropicClient factory.
        idle_poll_seconds: BRPOP timeout per iteration. Smaller =
            faster shutdown response, more Redis traffic; default 5s
            balances both.

    Returns:
        None. Logs at INFO on start/stop, WARNING on per-event
        decoding errors, ERROR on per-event processing crashes.
    """
    logger.info("worker loop start idle_poll={}s", idle_poll_seconds)
    while not stop_event.is_set():
        try:
            event = await queue.dequeue(timeout_seconds=idle_poll_seconds)
        except Exception:
            # Malformed payloads can raise in _decode. A bad message
            # has already been removed from the queue by BRPOP — log
            # and continue so the loop survives.
            logger.exception("worker dequeue failed")
            continue

        if event is None:
            # Idle tick: BRPOP timed out with no event. Loop back so
            # stop_event is re-checked.
            continue

        logger.info(
            "worker received event_id={} trigger={} firm_slug={}",
            event.event_id,
            event.trigger,
            event.firm_slug,
        )
        try:
            result = await process_event(
                event,
                sessionmaker=sessionmaker,
                plugin_registry=plugin_registry,
                tool_registry=tool_registry,
                model_caller=model_caller,
                embedder=embedder,
                anthropic_factory=anthropic_factory,
                dedup=dedup,
            )
        except Exception:
            logger.exception(
                "worker process_event crashed event_id={}", event.event_id,
            )
            continue

        logger.info(
            "worker event done event_id={} ran={} skipped={} firm_not_found={}",
            event.event_id,
            len(result.run_results),
            len(result.skipped),
            result.firm_not_found,
        )

    logger.info("worker loop exit")
