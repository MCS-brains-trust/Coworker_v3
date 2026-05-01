from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from coworker.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Construct the async engine lazily on first use.

    Deferring construction means `import coworker.db.session` does not require
    DATABASE_URL/REDIS_URL/etc. to be set — important for unit tests that never
    touch the DB and for tools (CLI introspection, mypy) that import the module
    without an env.
    """
    settings = get_settings()
    return create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_POOL_MAX_OVERFLOW,
        pool_pre_ping=True,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def __getattr__(name: str) -> Any:
    """PEP 562 lazy module attributes.

    Preserves `from coworker.db.session import engine` and
    `from coworker.db.session import SessionLocal` as before, but the underlying
    objects are only built on first access.
    """
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_sessionmaker()
    raise AttributeError(f"module 'coworker.db.session' has no attribute {name!r}")


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
