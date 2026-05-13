"""Integration tests for ``flush_token_meter_to_postgres``.

Real Redis (logical DB 13) + real Postgres test DB. Tests are fully
async-top-level (matching ``test_token_meter.py``); helpers that
touch the DB are also async so we never call ``asyncio.run`` from
inside an active event loop.
"""
import datetime as _dt
import uuid
from urllib.parse import urlparse, urlunparse

import pytest_asyncio
from redis.asyncio import Redis, from_url
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.config import get_settings
from coworker.db.models.tenancy import Firm
from coworker.db.models.token_usage import TokenUsageRow
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.observability.token_meter import (
    TokenMeter,
    flush_token_meter_to_postgres,
)

_TEST_REDIS_DB = "/13"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


@pytest_asyncio.fixture
async def redis_client():
    client = from_url(
        _test_redis_url(), encoding="utf-8", decode_responses=True
    )
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def flush_env(test_database_url):
    """Per-test engine + sessionmaker bound to the test database.

    Cleans up any firms (and their cascaded token_usage rows) seeded
    during the test on teardown.
    """
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sessionmaker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    created_firm_ids: list[uuid.UUID] = []
    try:
        yield {"sessionmaker": sessionmaker, "created_firm_ids": created_firm_ids}
    finally:
        for firm_id in created_firm_ids:
            await _delete_test_firm(sessionmaker, firm_id)
        await engine.dispose()


async def _delete_test_firm(
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


async def _seed_firm(
    sessionmaker: async_sessionmaker[AsyncSession], slug: str
) -> uuid.UUID:
    firm_id = uuid.uuid4()
    async with sessionmaker() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="Flush Firm", slug=slug))
        await session.commit()
    return firm_id


async def _read_usage(
    sessionmaker: async_sessionmaker[AsyncSession],
    firm_id: uuid.UUID,
) -> list[TokenUsageRow]:
    async with sessionmaker() as session, firm_context(firm_id):
        result = await session.execute(
            select(TokenUsageRow)
            .where(TokenUsageRow.firm_id == firm_id)
            .order_by(TokenUsageRow.day, TokenUsageRow.model)
        )
        return list(result.scalars().all())


# =========================================================================
# flush_token_meter_to_postgres
# =========================================================================


async def test_flush_empty_redis_returns_zero(
    redis_client: Redis, flush_env
) -> None:
    sm = flush_env["sessionmaker"]
    flushed = await flush_token_meter_to_postgres(redis_client, sm)
    assert flushed == 0


async def test_flush_single_meter_upserts_one_row(
    redis_client: Redis, flush_env
) -> None:
    sm = flush_env["sessionmaker"]
    created = flush_env["created_firm_ids"]

    firm_id = await _seed_firm(sm, f"flush-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    meter = TokenMeter(redis_client)
    await meter.record(
        firm_id=str(firm_id),
        model="claude-sonnet-4-6",
        input_tokens=120,
        output_tokens=480,
    )

    flushed = await flush_token_meter_to_postgres(redis_client, sm)
    assert flushed == 1

    rows = await _read_usage(sm, firm_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.firm_id == firm_id
    assert row.model == "claude-sonnet-4-6"
    assert row.day == _dt.datetime.now(_dt.UTC).date()
    assert row.input_tokens == 120
    assert row.output_tokens == 480
    assert row.calls == 1


async def test_flush_is_idempotent_on_rerun(
    redis_client: Redis, flush_env
) -> None:
    """Re-flushing the same Redis data overwrites with current values."""
    sm = flush_env["sessionmaker"]
    created = flush_env["created_firm_ids"]

    firm_id = await _seed_firm(sm, f"flush-id-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    meter = TokenMeter(redis_client)
    await meter.record(
        firm_id=str(firm_id),
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=200,
    )

    flushed_a = await flush_token_meter_to_postgres(redis_client, sm)
    rows_a = await _read_usage(sm, firm_id)
    assert flushed_a == 1 and rows_a[0].input_tokens == 100

    await meter.record(
        firm_id=str(firm_id),
        model="claude-sonnet-4-6",
        input_tokens=50,
        output_tokens=60,
    )
    flushed_b = await flush_token_meter_to_postgres(redis_client, sm)
    rows_b = await _read_usage(sm, firm_id)
    assert flushed_b == 1
    assert len(rows_b) == 1
    assert rows_b[0].input_tokens == 150
    assert rows_b[0].output_tokens == 260
    assert rows_b[0].calls == 2


async def test_flush_handles_multiple_firms_models_and_days(
    redis_client: Redis, flush_env
) -> None:
    sm = flush_env["sessionmaker"]
    created = flush_env["created_firm_ids"]

    firm_a = await _seed_firm(sm, f"flush-a-{uuid.uuid4().hex[:8]}")
    firm_b = await _seed_firm(sm, f"flush-b-{uuid.uuid4().hex[:8]}")
    created.extend([firm_a, firm_b])

    meter = TokenMeter(redis_client)
    await meter.record(
        firm_id=str(firm_a), model="sonnet",
        input_tokens=10, output_tokens=20,
    )
    await meter.record(
        firm_id=str(firm_a), model="opus",
        input_tokens=5, output_tokens=15,
    )
    await meter.record(
        firm_id=str(firm_b), model="sonnet",
        input_tokens=7, output_tokens=14,
    )

    # Plant a yesterday key directly to exercise day variation.
    yesterday = _dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=1)
    key = f"tokens:{firm_a}:sonnet:{yesterday.isoformat()}"
    await redis_client.hset(
        key,
        mapping={"input_tokens": "1", "output_tokens": "2", "calls": "3"},
    )

    flushed = await flush_token_meter_to_postgres(redis_client, sm)
    assert flushed == 4

    rows_a = await _read_usage(sm, firm_a)
    assert len(rows_a) == 3
    rows_b = await _read_usage(sm, firm_b)
    assert len(rows_b) == 1
    assert rows_b[0].model == "sonnet"


async def test_flush_skips_malformed_keys(
    redis_client: Redis, flush_env
) -> None:
    sm = flush_env["sessionmaker"]
    created = flush_env["created_firm_ids"]

    firm_id = await _seed_firm(sm, f"flush-mal-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    await redis_client.hset(
        f"tokens:{firm_id}:sonnet:2026-05-10",
        mapping={"input_tokens": "1", "output_tokens": "2", "calls": "1"},
    )
    await redis_client.hset(
        "tokens:not-a-uuid:sonnet:2026-05-10",
        mapping={"input_tokens": "1"},
    )
    await redis_client.hset(
        f"tokens:{firm_id}:sonnet:not-a-date",
        mapping={"input_tokens": "1"},
    )
    await redis_client.hset(
        f"tokens:{firm_id}::2026-05-10",  # empty model
        mapping={"input_tokens": "1"},
    )

    flushed = await flush_token_meter_to_postgres(redis_client, sm)
    assert flushed == 1


async def test_flush_tolerates_empty_hash_collapse(
    redis_client: Redis, flush_env
) -> None:
    """SCAN race where a key exists at SCAN time but its hash is empty
    by the time HGETALL runs — function should skip cleanly.
    """
    sm = flush_env["sessionmaker"]
    created = flush_env["created_firm_ids"]

    firm_id = await _seed_firm(sm, f"flush-emp-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    real_key = f"tokens:{firm_id}:sonnet:2026-05-10"
    await redis_client.hset(real_key, mapping={"input_tokens": "1"})
    # Plant a key and immediately delete its only field — Redis
    # collapses the empty hash, so SCAN/HGETALL won't see it; this
    # also covers the "key disappears between SCAN and HGETALL"
    # race (the function returns {} for missing keys).
    stale_key = f"tokens:{firm_id}:opus:2026-05-09"
    await redis_client.hset(stale_key, mapping={"foo": "bar"})
    await redis_client.hdel(stale_key, "foo")

    flushed = await flush_token_meter_to_postgres(redis_client, sm)
    assert flushed == 1
