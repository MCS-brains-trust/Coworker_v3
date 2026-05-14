"""One-shot approval dispatch CLI.

``python -m coworker.workers.dispatch``

Runs a single pass of ``sweep_dispatch`` and exits. Designed for
the systemd timer ``coworker-dispatch.timer`` (every 1 minute):
once the principal approves an email_draft item, the next tick
of this worker creates the corresponding Outlook draft via Graph
and transitions the row to ``sent``.

Exits 0 on success even when individual items failed (those rows
stay in ``dispatch_failed`` and the next tick retries). Logs a
structured summary for ops alerting.
"""
import argparse
import asyncio
import sys

from loguru import logger

from coworker.approval.dispatch import sweep_dispatch
from coworker.db.session import get_sessionmaker
from coworker.logging import setup_logging


async def _amain() -> int:
    setup_logging()
    sm = get_sessionmaker()
    result = await sweep_dispatch(sessionmaker=sm)
    if result.firms_seen == 0:
        logger.info("dispatch sweep no active firms")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one pass of the approval dispatch sweep. Designed "
            "to be called by a systemd timer."
        ),
    )
    parser.parse_args(argv)
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
