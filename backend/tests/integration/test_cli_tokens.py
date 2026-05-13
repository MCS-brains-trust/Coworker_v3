"""End-to-end test for the ``coworker tokens`` CLI command.

Drives the command through Click's CliRunner so the full path
runs — slug lookup with NO FORCE bracket, optional Redis flush,
firm_context query of token_usage, and the rendered table output.

Pattern matches test_cli_create_firm.py for the SessionLocal
monkey-patch; Redis is also redirected to test logical DB 12 so
production token data is never touched.
"""
import asyncio
import datetime as _dt
import uuid
from urllib.parse import urlparse, urlunparse

import pytest
from click.testing import CliRunner
from redis.asyncio import from_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.cli.main import cli
from coworker.config import get_settings
from coworker.db.models.tenancy import Firm
from coworker.db.models.token_usage import TokenUsageRow
from coworker.db.session import _attach_pool_listeners, firm_context

_TEST_REDIS_DB = "/12"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


def _fresh_test_redis():
    """Build a one-shot Redis client bound to the test logical DB.

    Tests cannot share a single Redis client across asyncio.run()
    boundaries — the CLI's asyncio.run() closes its event loop on
    exit, leaving the client's underlying transport orphaned, and
    any subsequent use raises "Event loop is closed". Each call
    builds a fresh client; callers are responsible for closing it
    within their own asyncio.run() block.
    """
    return from_url(
        _test_redis_url(), encoding="utf-8", decode_responses=True
    )


async def _redis_flushdb_oneshot() -> None:
    """Connect, flushdb, disconnect — for setup/teardown isolation."""
    client = _fresh_test_redis()
    try:
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture
def cli_tokens_environment(test_database_url, monkeypatch):
    """Redirect SessionLocal + Redis to test instances; clean up after.

    Redis is patched to return a *fresh* client per ``get_redis()``
    call so each asyncio.run() in the CLI gets a connection bound
    to its own event loop. The CLI is responsible for using the
    client within a single asyncio.run() block (which it does).
    """
    from coworker.db import redis as redis_module
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

    redis_module.get_redis.cache_clear()
    monkeypatch.setattr(redis_module, "get_redis", _fresh_test_redis)

    asyncio.run(_redis_flushdb_oneshot())

    created_firm_ids: list[uuid.UUID] = []
    try:
        yield {
            "sessionmaker": test_sm,
            "created_firm_ids": created_firm_ids,
        }
    finally:
        for firm_id in created_firm_ids:
            asyncio.run(_delete_firm(test_sm, firm_id))
        asyncio.run(_redis_flushdb_oneshot())
        asyncio.run(test_engine.dispose())
        # monkeypatch.undo() (run by pytest after this fixture's
        # finally) restores the original get_redis. We pre-cleared
        # its cache at setup so no stale entries can leak between
        # tests; clearing again here would crash because get_redis
        # is currently the un-cached _fresh_test_redis function.


async def _delete_firm(
    sessionmaker: async_sessionmaker[AsyncSession],
    firm_id: uuid.UUID,
) -> None:
    tables = ("firms", "users", "audit_log", "token_usage")
    async with sessionmaker() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        try:
            await session.execute(
                text("DELETE FROM token_usage WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM audit_log WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"),
                {"id": str(firm_id)},
            )
            await session.commit()
        finally:
            for t in tables:
                await session.execute(
                    text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
                )
            await session.commit()


def _seed_firm(
    sessionmaker: async_sessionmaker[AsyncSession], slug: str
) -> uuid.UUID:
    async def _run() -> uuid.UUID:
        firm_id = uuid.uuid4()
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="Tokens Test Firm",
                    slug=slug,
                )
            )
            await session.commit()
            return firm_id

    return asyncio.run(_run())


def _seed_token_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
    firm_id: uuid.UUID,
    rows: list[dict],
) -> None:
    async def _run() -> None:
        async with sessionmaker() as session, firm_context(firm_id):
            for r in rows:
                session.add(
                    TokenUsageRow(
                        firm_id=firm_id,
                        model=r["model"],
                        day=r["day"],
                        input_tokens=r["input_tokens"],
                        output_tokens=r["output_tokens"],
                        calls=r["calls"],
                    )
                )
            await session.commit()

    asyncio.run(_run())


# =========================================================================
# coworker tokens
# =========================================================================


def test_tokens_renders_report_for_seeded_data(cli_tokens_environment) -> None:
    sm = cli_tokens_environment["sessionmaker"]
    created = cli_tokens_environment["created_firm_ids"]

    slug = f"tok-cli-{uuid.uuid4().hex[:8]}"
    firm_id = _seed_firm(sm, slug)
    created.append(firm_id)

    _seed_token_rows(
        sm,
        firm_id,
        rows=[
            {
                "model": "claude-sonnet-4-6",
                "day": _dt.date(2026, 5, 1),
                "input_tokens": 1_000,
                "output_tokens": 2_000,
                "calls": 5,
            },
            {
                "model": "claude-sonnet-4-6",
                "day": _dt.date(2026, 5, 15),
                "input_tokens": 500,
                "output_tokens": 750,
                "calls": 3,
            },
            {
                "model": "claude-opus-4-7",
                "day": _dt.date(2026, 5, 10),
                "input_tokens": 200,
                "output_tokens": 800,
                "calls": 2,
            },
            # Different month — must NOT appear in the report.
            {
                "model": "claude-sonnet-4-6",
                "day": _dt.date(2026, 4, 28),
                "input_tokens": 9_999,
                "output_tokens": 9_999,
                "calls": 99,
            },
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["tokens", "--firm", slug, "--month", "2026-05", "--no-flush"],
    )
    assert result.exit_code == 0, (
        f"exit={result.exit_code}\noutput={result.output}\n"
        f"exception={result.exception}"
    )
    out = result.output
    # Header lines
    assert f"firm '{slug}'" in out
    assert "2026-05" in out
    # Sonnet aggregates: 1000+500, 2000+750, 5+3 = (1500, 2750, 8)
    assert "claude-sonnet-4-6" in out
    assert "1,500" in out
    assert "2,750" in out
    # Opus row (200, 800, 2)
    assert "claude-opus-4-7" in out
    assert "200" in out
    assert "800" in out
    # Totals: 1700, 3550, 10
    assert "TOTAL" in out
    assert "1,700" in out
    assert "3,550" in out
    # The April row (9999) must NOT appear
    assert "9,999" not in out


def test_tokens_empty_period_prints_no_usage_line(cli_tokens_environment) -> None:
    sm = cli_tokens_environment["sessionmaker"]
    created = cli_tokens_environment["created_firm_ids"]

    slug = f"tok-empty-{uuid.uuid4().hex[:8]}"
    firm_id = _seed_firm(sm, slug)
    created.append(firm_id)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["tokens", "--firm", slug, "--month", "2026-05", "--no-flush"],
    )
    assert result.exit_code == 0
    assert "(no usage recorded in this period)" in result.output


def test_tokens_unknown_firm_exits_with_error(cli_tokens_environment) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "tokens",
            "--firm", f"does-not-exist-{uuid.uuid4().hex[:6]}",
            "--month", "2026-05",
            "--no-flush",
        ],
    )
    assert result.exit_code != 0
    assert "No firm with slug" in result.output


def test_tokens_invalid_month_format_exits_with_error(
    cli_tokens_environment,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["tokens", "--firm", "x", "--month", "May 2026", "--no-flush"],
    )
    assert result.exit_code != 0
    assert "YYYY-MM" in result.output


def _hset_oneshot(key: str, mapping: dict) -> None:
    """Plant a Redis hash via a one-shot client. Bound to its own loop."""

    async def _run() -> None:
        client = _fresh_test_redis()
        try:
            await client.hset(key, mapping=mapping)
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_tokens_with_flush_includes_redis_data(cli_tokens_environment) -> None:
    """End-to-end: data in Redis only is flushed and shown in the report."""
    sm = cli_tokens_environment["sessionmaker"]
    created = cli_tokens_environment["created_firm_ids"]

    slug = f"tok-flush-{uuid.uuid4().hex[:8]}"
    firm_id = _seed_firm(sm, slug)
    created.append(firm_id)

    today = _dt.datetime.now(_dt.UTC).date()
    _hset_oneshot(
        f"tokens:{firm_id}:claude-haiku-4-5:{today.isoformat()}",
        mapping={
            "input_tokens": "777",
            "output_tokens": "888",
            "calls": "9",
        },
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["tokens", "--firm", slug, "--month", f"{today.year}-{today.month:02d}"],
    )
    assert result.exit_code == 0, result.output
    assert "claude-haiku-4-5" in result.output
    assert "777" in result.output
    assert "888" in result.output
