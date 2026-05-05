"""End-to-end test for the create-firm CLI command.

Drives `coworker create-firm` through Click's CliRunner so the entire
code path runs — including the SessionLocal acquisition, firm_context
entry, INSERT under FORCE RLS, and commit. Catches the regression
where create-firm's INSERT was denied because no firm_context was
applied (the WITH CHECK on the firms_firm_isolation_insert policy
evaluates id = NULLIF(current_setting('app.firm_id', true), '')::uuid,
which is NULL when firm_context is never entered).

The CLI command commits via SessionLocal, which by default points at
the dev DB; the `cli_test_sessionmaker` fixture monkey-patches
coworker.db.session's lazy factories so the patched SessionLocal points
at the test DB.

This test is sync (not pytest-asyncio) because the CLI command itself
calls asyncio.run() internally — that crashes inside a running event
loop. The CliRunner invocation is sync; the post-invoke verification
runs through its own asyncio.run().

Verification uses a separately-built session against the test DB
because the CLI's commit lives on a different connection from the
db_session fixture, and we need a session whose own commit is real
(not a savepoint that gets rolled back at fixture teardown).
"""
import asyncio
import re
import uuid

import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from coworker.cli.main import cli
from coworker.db.models.tenancy import Firm
from coworker.db.session import _attach_pool_listeners, firm_context


@pytest.fixture
def cli_test_sessionmaker(test_database_url, monkeypatch):
    """Redirect SessionLocal to the test DB for the duration of the test.

    The CLI commands import SessionLocal lazily inside their function
    bodies via PEP 562 __getattr__, which calls get_sessionmaker()
    each time. Patching get_sessionmaker (and get_engine for symmetry)
    means CliRunner-invoked commands write to the test DB instead of
    whatever DATABASE_URL points at.
    """
    from coworker.db import session as session_module

    test_engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(test_engine)
    test_sm = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: test_sm)
    monkeypatch.setattr(session_module, "get_engine", lambda: test_engine)

    try:
        yield test_sm
    finally:
        asyncio.run(test_engine.dispose())


def test_create_firm_inserts_under_firm_context(cli_test_sessionmaker) -> None:
    slug = f"create-firm-test-{uuid.uuid4().hex[:8]}"

    runner = CliRunner()
    result = runner.invoke(cli, ["create-firm", "Test Firm", "--slug", slug])

    assert result.exit_code == 0, (
        f"create-firm exited {result.exit_code}\n"
        f"stdout: {result.output}\n"
        f"exception: {result.exception}"
    )

    match = re.search(r"id=([0-9a-fA-F-]{36})", result.output)
    assert match, f"output did not contain id=<uuid>: {result.output!r}"
    firm_id = uuid.UUID(match.group(1))

    async def _verify_and_cleanup() -> None:
        async with cli_test_sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            assert firm.slug == slug
            assert firm.name == "Test Firm"

            await session.delete(firm)
            await session.commit()

    asyncio.run(_verify_and_cleanup())
