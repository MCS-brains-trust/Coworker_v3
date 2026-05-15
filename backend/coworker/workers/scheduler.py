"""One-shot scheduled-trigger CLI.

``python -m coworker.workers.scheduler``

Runs a single pass of ``sweep_scheduler`` and exits. Designed for
the systemd timer ``coworker-scheduler.timer`` (every 1 minute):
the sweep finds every enabled plugin whose cron has elapsed since
its last firing and enqueues a ``scheduled`` PluginEvent for the
worker pool to consume.

Exits 0 on success even when individual plugins failed (bad cron
expressions are logged; the row's last_fired_at stays put so the
next tick retries). Logs a structured summary for ops alerting.
"""
import argparse
import asyncio
import sys

from loguru import logger

from coworker.db import redis as redis_module
from coworker.db.session import get_sessionmaker
from coworker.logging import setup_logging
from coworker.plugins.base import PluginRegistry
from coworker.plugins.builtin import register_builtin_plugins
from coworker.plugins.scheduler import sweep_scheduler
from coworker.workers.plugin_queue import PluginEventQueue


async def _amain() -> int:
    setup_logging()
    sm = get_sessionmaker()
    redis = redis_module.get_redis()
    queue = PluginEventQueue(redis)

    registry = PluginRegistry()
    register_builtin_plugins(registry)

    try:
        result = await sweep_scheduler(
            sessionmaker=sm, queue=queue, plugin_registry=registry,
        )
    finally:
        await redis.aclose()

    if result.firms_seen == 0:
        logger.info("scheduler sweep no active firms")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one pass of the scheduled-trigger sweep. Designed "
            "to be called by a systemd timer once a minute."
        ),
    )
    parser.parse_args(argv)
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
