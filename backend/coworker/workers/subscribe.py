"""One-shot subscription sweep CLI: ``python -m coworker.workers.subscribe``.

Runs a single pass of ``sweep_subscriptions`` and exits. Designed
to be invoked by the systemd timer (`coworker-subscribe.timer`)
at a cadence shorter than ``DEFAULT_RENEWAL_BUFFER`` (default 12h)
so any subscription about to expire is renewed before Microsoft
deletes it.

Exits 0 on success even when individual firms/users had errors —
the systemd timer should keep trying. The CLI logs a structured
summary so dashboards can alert on persistent failures.
"""
import argparse
import asyncio
import sys

from loguru import logger

from coworker.config import get_settings
from coworker.db.session import get_sessionmaker
from coworker.graph.subscription_sweep import sweep_subscriptions
from coworker.logging import setup_logging


async def _amain(*, dry_run: bool) -> int:
    setup_logging()
    settings = get_settings()
    if not settings.PUBLIC_WEBHOOK_BASE_URL:
        logger.error(
            "subscription sweep PUBLIC_WEBHOOK_BASE_URL not set; aborting"
        )
        return 1
    if dry_run:
        # Even a dry-run touches Microsoft Graph; the design choice is
        # to fail explicitly so a misconfigured timer doesn't silently
        # do nothing. The real --dry-run lives behind a deliberate flag
        # if needed in future.
        logger.warning(
            "subscription sweep dry-run requested but no dry-run path "
            "implemented; refusing to call Graph",
        )
        return 1

    sm = get_sessionmaker()
    result = await sweep_subscriptions(
        sessionmaker=sm,
        public_webhook_base_url=settings.PUBLIC_WEBHOOK_BASE_URL,
    )

    # Exit non-zero only on total failure (no firms at all could be
    # processed). Per-firm errors are logged but don't fail the job —
    # next tick retries.
    if result.firms_seen == 0:
        logger.warning("subscription sweep no active firms")
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one pass of the Microsoft Graph subscription sweep. "
            "Designed to be called by a systemd timer."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Reserved — currently refuses to run (Graph contact unavoidable).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_amain(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
