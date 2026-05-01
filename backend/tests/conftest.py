"""Pytest configuration and shared fixtures for CoWorker v3 tests.

Test database setup
-------------------
Integration tests run against a separate Postgres database so they cannot
pollute the dev `coworker` database. Resolution order for the test URL:

    1. $TEST_DATABASE_URL if set, OR
    2. The dev DATABASE_URL with the database name swapped to
       `coworker_test` (same Postgres server, same credentials).

At session start the test DB is created if it doesn't exist (via asyncpg
connecting to the `postgres` admin DB) and `alembic upgrade head` is run
against it in a subprocess with DATABASE_URL pointed at the test DB.
The DB is left in place between sessions for fast re-use; drop it manually
if a migration changes shape and you want a clean slate.

Local prerequisite
------------------
The Postgres role used in DATABASE_URL must have CREATEDB privilege so
the fixture can create coworker_test on first run. Grant it once as
superuser:

    sudo -u postgres psql -c "ALTER ROLE coworker CREATEDB;"

If the role lacks CREATEDB the fixture errors with
asyncpg.exceptions.InsufficientPrivilegeError on first use.

Each test gets a `db_session` fixture wrapped in an outer transaction that
is rolled back at teardown — tests cannot pollute each other and they
cannot leave data behind. Any session.commit() inside the test only
commits a SAVEPOINT (via join_transaction_mode="create_savepoint"), so
the outer rollback still wipes everything the test did.
"""
import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from coworker.config import get_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "backend"


def _resolve_test_db_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    settings = get_settings()
    url = make_url(str(settings.DATABASE_URL))
    return url.set(database="coworker_test").render_as_string(hide_password=False)


async def _ensure_database_exists(test_url: str) -> None:
    url = make_url(test_url)
    db_name = url.database
    if not db_name:
        raise RuntimeError("Test DB URL has no database component")
    admin = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database="postgres",
    )
    try:
        exists = await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        if not exists:
            # CREATE DATABASE cannot run inside a transaction; asyncpg's
            # execute() runs in autocommit for non-transactional queries.
            await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()


def _alembic_upgrade_head(test_url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = test_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_BACKEND_DIR),
        env=env,
        check=True,
    )


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _resolve_test_db_url()
    asyncio.run(_ensure_database_exists(url))
    _alembic_upgrade_head(url)
    return url


@pytest_asyncio.fixture
async def db_session(test_database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            trans = await connection.begin()
            session = AsyncSession(
                bind=connection,
                join_transaction_mode="create_savepoint",
                expire_on_commit=False,
            )
            try:
                yield session
            finally:
                await session.close()
                if trans.is_active:
                    await trans.rollback()
    finally:
        await engine.dispose()
