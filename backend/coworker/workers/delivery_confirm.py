"""One-shot delivery-confirmation CLI (pre-pilot Task 3).

``python -m coworker.workers.delivery_confirm``

Runs a single pass of ``sweep_delivery_confirmation`` and exits.
Designed for the systemd timer ``coworker-delivery-confirm.timer``
(every 30 minutes): once an ``approval_items`` row's
``delivery_status_updated_at`` is older than the 4h confirmation
window with status still ``'sent'``, the next sweep flips it to
``'delivered'``. Microsoft would have NDR'd within the window if
delivery had failed.

Exits 0 on success; logs a structured summary for ops alerting.
"""
import argparse
import asyncio
import sys

from loguru import logger

from coworker.approval.delivery import sweep_delivery_confirmation
from coworker.db.session import get_sessionmaker
from coworker.logging import setup_logging


async def _amain() -> int:
    setup_logging()
    sm = get_sessionmaker()
    result = await sweep_delivery_confirmation(sessionmaker=sm)
    if result.firms_seen == 0:
        logger.info("delivery confirm sweep no active firms")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one pass of the 4h delivery-confirmation sweep. "
            "Designed to be called by a systemd timer."
        ),
    )
    parser.parse_args(argv)
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
