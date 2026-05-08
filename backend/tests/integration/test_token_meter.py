"""Integration tests for `coworker.observability.token_meter.TokenMeter`.

Real Redis client against a dedicated logical DB (14, separate from
oauth_state's 15) so we never touch dev token-usage data. The fixture
flushdb's the database before and after each test for isolation.

Pattern matches `test_oauth_state.py`.
"""
import datetime as _dt
from urllib.parse import urlparse, urlunparse

import pytest_asyncio
from redis.asyncio import Redis, from_url

from coworker.config import get_settings
from coworker.observability.token_meter import TokenMeter

_TEST_REDIS_DB = "/14"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


@pytest_asyncio.fixture
async def redis_test_client():
    client = from_url(
        _test_redis_url(), encoding="utf-8", decode_responses=True
    )
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


# --------------------------- record() --------------------------------------


async def test_record_increments_hash_fields(redis_test_client: Redis) -> None:
    meter = TokenMeter(redis_test_client)
    await meter.record(
        firm_id="firm-a",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
    )
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    raw = await redis_test_client.hgetall(
        f"tokens:firm-a:claude-sonnet-4-6:{today}"
    )
    assert raw == {
        "input_tokens": "100",
        "output_tokens": "50",
        "calls": "1",
    }


async def test_record_accumulates_across_calls(
    redis_test_client: Redis,
) -> None:
    meter = TokenMeter(redis_test_client)
    for _ in range(3):
        await meter.record(
            firm_id="firm-a",
            model="claude-sonnet-4-6",
            input_tokens=10,
            output_tokens=5,
        )
    usage = await meter.usage(firm_id="firm-a", model="claude-sonnet-4-6")
    assert usage == {"input_tokens": 30, "output_tokens": 15, "calls": 3}


async def test_record_applies_ttl(redis_test_client: Redis) -> None:
    meter = TokenMeter(redis_test_client, ttl_days=7)
    await meter.record(
        firm_id="firm-a",
        model="claude-sonnet-4-6",
        input_tokens=1,
        output_tokens=1,
    )
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    ttl = await redis_test_client.ttl(
        f"tokens:firm-a:claude-sonnet-4-6:{today}"
    )
    # 7 days = 604800s; allow generous slack for test execution time.
    assert 604700 < ttl <= 604800


async def test_record_with_zero_tokens_is_allowed(
    redis_test_client: Redis,
) -> None:
    """count_tokens calls record (input=N, output=0)."""
    meter = TokenMeter(redis_test_client)
    await meter.record(
        firm_id="firm-a",
        model="claude-haiku-4-5-20251001",
        input_tokens=42,
        output_tokens=0,
    )
    usage = await meter.usage(
        firm_id="firm-a", model="claude-haiku-4-5-20251001"
    )
    assert usage == {"input_tokens": 42, "output_tokens": 0, "calls": 1}


async def test_record_rejects_negative_tokens(
    redis_test_client: Redis,
) -> None:
    meter = TokenMeter(redis_test_client)
    import pytest
    with pytest.raises(ValueError):
        await meter.record(
            firm_id="firm-a",
            model="x",
            input_tokens=-1,
            output_tokens=0,
        )
    with pytest.raises(ValueError):
        await meter.record(
            firm_id="firm-a",
            model="x",
            input_tokens=0,
            output_tokens=-5,
        )


# --------------------------- usage() ---------------------------------------


async def test_usage_returns_zeros_for_missing_key(
    redis_test_client: Redis,
) -> None:
    meter = TokenMeter(redis_test_client)
    usage = await meter.usage(firm_id="firm-never-seen", model="anything")
    assert usage == {"input_tokens": 0, "output_tokens": 0, "calls": 0}


async def test_usage_for_specific_day(redis_test_client: Redis) -> None:
    """A day-specific query reads the correct key."""
    meter = TokenMeter(redis_test_client)
    yesterday = _dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=1)
    # Seed yesterday's key directly.
    await redis_test_client.hset(
        f"tokens:firm-a:claude-sonnet-4-6:{yesterday.isoformat()}",
        mapping={"input_tokens": 1000, "output_tokens": 500, "calls": 10},
    )
    usage = await meter.usage(
        firm_id="firm-a", model="claude-sonnet-4-6", day=yesterday
    )
    assert usage == {"input_tokens": 1000, "output_tokens": 500, "calls": 10}


# --------------------------- isolation -------------------------------------


async def test_cross_firm_isolation(redis_test_client: Redis) -> None:
    meter = TokenMeter(redis_test_client)
    await meter.record(
        firm_id="firm-a",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
    )
    await meter.record(
        firm_id="firm-b",
        model="claude-sonnet-4-6",
        input_tokens=200,
        output_tokens=80,
    )
    a = await meter.usage(firm_id="firm-a", model="claude-sonnet-4-6")
    b = await meter.usage(firm_id="firm-b", model="claude-sonnet-4-6")
    assert a == {"input_tokens": 100, "output_tokens": 50, "calls": 1}
    assert b == {"input_tokens": 200, "output_tokens": 80, "calls": 1}


async def test_cross_model_isolation(redis_test_client: Redis) -> None:
    meter = TokenMeter(redis_test_client)
    await meter.record(
        firm_id="firm-a",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
    )
    await meter.record(
        firm_id="firm-a",
        model="claude-opus-4-7",
        input_tokens=300,
        output_tokens=200,
    )
    sonnet = await meter.usage(firm_id="firm-a", model="claude-sonnet-4-6")
    opus = await meter.usage(firm_id="firm-a", model="claude-opus-4-7")
    assert sonnet["input_tokens"] == 100
    assert opus["input_tokens"] == 300


# --------------------------- construction ----------------------------------


def test_invalid_ttl_rejected(redis_test_client: Redis) -> None:
    import pytest
    with pytest.raises(ValueError):
        TokenMeter(redis_test_client, ttl_days=0)
    with pytest.raises(ValueError):
        TokenMeter(redis_test_client, ttl_days=-1)
