"""Tests for ``coworker.memory.embeddings``.

Pattern: respx-mocked HTTP for the provider calls + a real Redis
(logical DB 10) for the cache. Tests stay tight — provider error
mapping reuses the same shape proven for Anthropic / Graph / XPM,
so we cover the load-bearing pieces (cache hit / miss / mixed,
order preservation, Voyage vs OpenAI request shapes, the factory)
without re-litigating every status code.
"""
from urllib.parse import urlparse, urlunparse

import httpx
import pytest
import pytest_asyncio
import respx
from pydantic import SecretStr
from redis.asyncio import Redis, from_url

from coworker.config import Settings, get_settings
from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.memory.embeddings import (
    EMBEDDING_DIM,
    EmbeddingCache,
    OpenAIEmbedder,
    VoyageEmbedder,
    get_embedder,
)

_TEST_REDIS_DB = "/10"
_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_OPENAI_URL = "https://api.openai.com/v1/embeddings"


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


def _vector(seed: float) -> list[float]:
    """1024-dim vector where every component is ``seed``. Saves space."""
    return [seed] * EMBEDDING_DIM


def _voyage_response(texts: list[str]) -> dict:
    return {
        "model": "voyage-3",
        "data": [
            {"embedding": _vector(float(i + 1)), "index": i}
            for i, _ in enumerate(texts)
        ],
        "usage": {"total_tokens": sum(len(t.split()) for t in texts)},
    }


def _openai_response(texts: list[str], dimensions: int = EMBEDDING_DIM) -> dict:
    return {
        "model": "text-embedding-3-large",
        "data": [
            {"embedding": [float(i + 1)] * dimensions, "index": i}
            for i, _ in enumerate(texts)
        ],
        "usage": {"total_tokens": sum(len(t.split()) for t in texts)},
    }


# =========================================================================
# VoyageEmbedder
# =========================================================================


async def test_voyage_embed_posts_expected_payload_and_parses_response() -> None:
    voyage = VoyageEmbedder(api_key=SecretStr("k-voyage"))
    texts = ["Acme Pty Ltd BAS Q1", "Director's loan account"]

    async with respx.mock(assert_all_called=True) as rmock:
        route = rmock.post(_VOYAGE_URL).mock(
            return_value=httpx.Response(200, json=_voyage_response(texts))
        )
        vectors = await voyage.embed(texts)

    sent = route.calls.last.request
    body = sent.read().decode()
    assert "voyage-3" in body
    assert "Acme Pty Ltd BAS Q1" in body
    assert "input_type" in body
    assert sent.headers["Authorization"] == "Bearer k-voyage"
    assert vectors == [_vector(1.0), _vector(2.0)]


async def test_voyage_401_raises_auth_error() -> None:
    voyage = VoyageEmbedder(api_key=SecretStr("k"))
    async with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_VOYAGE_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(ConnectorAuthError):
            await voyage.embed(["x"])


async def test_voyage_429_raises_rate_limited() -> None:
    voyage = VoyageEmbedder(api_key=SecretStr("k"))
    async with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_VOYAGE_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "7"})
        )
        with pytest.raises(ConnectorRateLimited) as excinfo:
            await voyage.embed(["x"])
        assert excinfo.value.retry_after == 7.0


async def test_voyage_5xx_raises_transient() -> None:
    voyage = VoyageEmbedder(api_key=SecretStr("k"))
    async with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_VOYAGE_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(ConnectorTransient):
            await voyage.embed(["x"])


async def test_voyage_network_error_raises_transient() -> None:
    voyage = VoyageEmbedder(api_key=SecretStr("k"))
    async with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_VOYAGE_URL).mock(
            side_effect=httpx.ConnectError("no net")
        )
        with pytest.raises(ConnectorTransient):
            await voyage.embed(["x"])


async def test_voyage_empty_input_returns_empty_without_http() -> None:
    voyage = VoyageEmbedder(api_key=SecretStr("k"))
    async with respx.mock(assert_all_called=False) as rmock:
        rmock.post(_VOYAGE_URL).mock(return_value=httpx.Response(500))
        result = await voyage.embed([])
    assert result == []


# =========================================================================
# OpenAIEmbedder
# =========================================================================


async def test_openai_sends_dimensions_param() -> None:
    openai = OpenAIEmbedder(api_key=SecretStr("k-openai"))
    async with respx.mock(assert_all_called=True) as rmock:
        route = rmock.post(_OPENAI_URL).mock(
            return_value=httpx.Response(200, json=_openai_response(["t"]))
        )
        await openai.embed(["t"])
    body = route.calls.last.request.read().decode()
    assert "text-embedding-3-large" in body
    # The dimensions param Matryoshka-truncates to 1024
    assert "1024" in body


async def test_openai_round_trips_vector() -> None:
    openai = OpenAIEmbedder(api_key=SecretStr("k"))
    async with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_OPENAI_URL).mock(
            return_value=httpx.Response(
                200, json=_openai_response(["a", "b"])
            )
        )
        vectors = await openai.embed(["a", "b"])
    assert vectors[0][0] == 1.0
    assert vectors[1][0] == 2.0


# =========================================================================
# EmbeddingCache + cache-aware embed
# =========================================================================


async def test_cache_round_trip(redis_client: Redis) -> None:
    cache = EmbeddingCache(redis_client)
    await cache.set_many("voyage-3", {"hello": _vector(0.5)})
    got = await cache.get_many("voyage-3", ["hello", "miss"])
    assert got[0] == [pytest.approx(0.5)] * EMBEDDING_DIM
    assert got[1] is None


async def test_cache_isolates_by_model_name(redis_client: Redis) -> None:
    cache = EmbeddingCache(redis_client)
    await cache.set_many("voyage-3", {"x": _vector(1.0)})
    got = await cache.get_many("openai-3-large", ["x"])
    assert got == [None]


async def test_embed_with_cache_serves_hit_without_http(
    redis_client: Redis,
) -> None:
    cache = EmbeddingCache(redis_client)
    await cache.set_many("voyage-3", {"client A": _vector(0.9)})

    voyage = VoyageEmbedder(api_key=SecretStr("k"), cache=cache)
    async with respx.mock(assert_all_called=False) as rmock:
        # Configure a mock that would explode if hit — only here so
        # respx errors loudly on an unexpected call.
        rmock.post(_VOYAGE_URL).mock(
            side_effect=AssertionError("cache hit should skip HTTP")
        )
        vectors = await voyage.embed(["client A"])
    assert vectors == [_vector(0.9)]


async def test_embed_with_cache_mixed_hit_and_miss_preserves_order(
    redis_client: Redis,
) -> None:
    cache = EmbeddingCache(redis_client)
    # Pre-cache index 0 and index 2; index 1 must be fetched.
    await cache.set_many(
        "voyage-3",
        {
            "first": _vector(1.0),
            "third": _vector(3.0),
        },
    )
    voyage = VoyageEmbedder(api_key=SecretStr("k"), cache=cache)

    async with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_VOYAGE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "voyage-3",
                    "data": [{"embedding": _vector(2.0), "index": 0}],
                    "usage": {"total_tokens": 1},
                },
            )
        )
        vectors = await voyage.embed(["first", "second", "third"])

    # Order matches input order; the miss was inserted into position 1.
    assert vectors[0] == _vector(1.0)
    assert vectors[1] == _vector(2.0)
    assert vectors[2] == _vector(3.0)

    # The newly-fetched vector landed in the cache too.
    refetched = await cache.get_many("voyage-3", ["second"])
    assert refetched[0] == _vector(2.0)


# =========================================================================
# get_embedder factory
# =========================================================================


def _settings_with(provider: str, **overrides) -> Settings:
    """Build a Settings overriding the env. SecretStr is required for keys."""
    base = {
        "DATABASE_URL": "postgresql+asyncpg://x:y@localhost/x",
        "REDIS_URL": "redis://localhost/0",
        "MASTER_ENCRYPTION_KEY": SecretStr("a" * 44),
        "SESSION_JWT_SECRET": SecretStr("a" * 44),
        "ANTHROPIC_API_KEY": SecretStr("k"),
        "EMBEDDING_PROVIDER": provider,
    }
    base.update(overrides)
    return Settings.model_validate(base)


def test_get_embedder_returns_voyage_when_provider_is_voyage() -> None:
    s = _settings_with("voyage", VOYAGE_API_KEY=SecretStr("k"))
    embedder = get_embedder(s)
    assert isinstance(embedder, VoyageEmbedder)
    assert embedder.dimensions == EMBEDDING_DIM


def test_get_embedder_returns_openai_when_provider_is_openai() -> None:
    s = _settings_with("openai", OPENAI_API_KEY=SecretStr("k"))
    embedder = get_embedder(s)
    assert isinstance(embedder, OpenAIEmbedder)


def test_get_embedder_missing_voyage_key_raises_auth_error() -> None:
    s = _settings_with("voyage")
    with pytest.raises(ConnectorAuthError, match="VOYAGE_API_KEY"):
        get_embedder(s)


def test_get_embedder_missing_openai_key_raises_auth_error() -> None:
    s = _settings_with("openai")
    with pytest.raises(ConnectorAuthError, match="OPENAI_API_KEY"):
        get_embedder(s)
