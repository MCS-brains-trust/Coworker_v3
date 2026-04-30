import sys

from loguru import logger

from coworker.config import get_settings


def setup_logging() -> None:
    """Configure structured JSON logging to stdout for systemd capture."""
    settings = get_settings()
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        serialize=True,           # JSON output
        enqueue=True,             # async-safe
        backtrace=False,          # don't leak source in prod
        diagnose=settings.ENVIRONMENT == "dev",
    )
    if settings.ENVIRONMENT == "dev":
        # Pretty console output additionally during dev
        logger.add(
            sys.stderr,
            level="DEBUG",
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
                   "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
                   "- <level>{message}</level>",
        )
