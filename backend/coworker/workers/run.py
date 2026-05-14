"""Worker entry-point: ``python -m coworker.workers.run``.

Wires the BRPOP loop to production resources:

- Redis client from settings (the same client other parts of the
  app share).
- Sessionmaker from ``coworker.db.session`` so RLS pool listeners
  and the firm-context guc behave identically to the API.
- Plugin registry populated with the builtin catalogue.
- Tool registry populated with the builtin tools.
- Signal handlers (SIGTERM / SIGINT) flip ``stop_event`` so the
  worker drains its current event and exits cleanly. systemd
  sends SIGTERM on stop and waits up to ``TimeoutStopSec`` before
  escalating to SIGKILL.

Run multiple workers per process by adjusting ``--concurrency``;
multiple BRPOP-ing workers on the same queue are safe because
Redis hands each event to exactly one consumer.
"""
import argparse
import asyncio
import signal
import sys

from loguru import logger

from coworker.db import redis as redis_module
from coworker.db.session import get_sessionmaker
from coworker.logging import setup_logging
from coworker.orchestrator.builtin_tools import register_builtin_tools
from coworker.orchestrator.tools import ToolRegistry
from coworker.plugins.base import PluginRegistry
from coworker.plugins.builtin import register_builtin_plugins
from coworker.workers.loop import run_worker
from coworker.workers.plugin_queue import PluginEventQueue


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Flip ``stop_event`` on SIGTERM/SIGINT.

    Registered via ``loop.add_signal_handler`` so the wakeup is
    delivered to the running event loop; running synchronously from
    a signal handler would race with in-flight DB I/O.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig, _on_signal, sig, stop_event,
        )


def _on_signal(sig: signal.Signals, stop_event: asyncio.Event) -> None:
    if stop_event.is_set():
        # Second signal — operator wants out *now*. Honour it by
        # letting the default handler escalate (KeyboardInterrupt).
        logger.warning("worker repeat signal={} forcing exit", sig.name)
        signal.raise_signal(sig)
        return
    logger.info("worker received signal={} draining", sig.name)
    stop_event.set()


async def _amain(*, concurrency: int, idle_poll_seconds: int) -> None:
    setup_logging()
    logger.info(
        "worker bootstrap concurrency={} idle_poll={}s",
        concurrency,
        idle_poll_seconds,
    )

    sm = get_sessionmaker()
    plugin_registry = PluginRegistry()
    register_builtin_plugins(plugin_registry)

    tool_registry = ToolRegistry()
    register_builtin_tools(tool_registry)

    redis = redis_module.get_redis()
    queue = PluginEventQueue(redis)

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    workers = [
        asyncio.create_task(
            run_worker(
                queue=queue,
                sessionmaker=sm,
                plugin_registry=plugin_registry,
                tool_registry=tool_registry,
                stop_event=stop_event,
                idle_poll_seconds=idle_poll_seconds,
            ),
            name=f"worker-{i}",
        )
        for i in range(concurrency)
    ]
    logger.info("worker started count={}", len(workers))

    try:
        await asyncio.gather(*workers)
    finally:
        logger.info("worker shutdown closing redis")
        await redis.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the CoWorker v3 plugin event worker."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of concurrent BRPOP workers in this process.",
    )
    parser.add_argument(
        "--idle-poll-seconds",
        type=int,
        default=5,
        help=(
            "BRPOP timeout per iteration. Smaller = faster shutdown "
            "response, more Redis traffic."
        ),
    )
    args = parser.parse_args(argv)

    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.idle_poll_seconds < 1:
        parser.error("--idle-poll-seconds must be >= 1")

    try:
        asyncio.run(
            _amain(
                concurrency=args.concurrency,
                idle_poll_seconds=args.idle_poll_seconds,
            )
        )
    except KeyboardInterrupt:
        # Second signal escalation path — already logged.
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
