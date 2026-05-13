"""Embedding providers — Voyage and OpenAI behind one interface.

The hybrid retriever (Phase 4C), the KG populator (Phase 4D), and
the SharePoint indexer (Phase 4E) all need embeddings. They share
one provider per process via ``get_embedder(settings)`` so a
firm-wide model change goes through ``Settings.EMBEDDING_PROVIDER``,
not scattered call sites.

Both providers expose 1024-dimensional vectors so the
``vector(1024)`` columns from migration ``e5f6a7b8c9d0`` are
unambiguous. Voyage's ``voyage-3`` is native 1024; OpenAI's
``text-embedding-3-large`` is natively 3072 and we request 1024 via
its ``dimensions`` parameter (which Matryoshka-truncates without
re-embedding overhead).

Redis cache
-----------

``EmbeddingCache`` wraps a Redis client with a simple
``get_many`` / ``set_many`` API keyed by
``embedding:{model}:{sha256(text)}``. A 24-hour TTL covers
repeated queries within a day (the retriever may re-embed the same
prompt several times in a session) without holding stale vectors
forever. The cache is *optional* — callers that don't care about
the saving (small one-off batches, tests, the first prompt of a
session) can construct an embedder with ``cache=None`` and pay the
full round-trip every call.
"""
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx
from pydantic import SecretStr
from redis.asyncio import Redis

from coworker.config import Settings
from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
)

EMBEDDING_DIM = 1024
_DEFAULT_TTL_SECONDS = 24 * 60 * 60

_VOYAGE_ENDPOINT = "https://api.voyageai.com/v1/embeddings"
_OPENAI_ENDPOINT = "https://api.openai.com/v1/embeddings"


class Embedder(Protocol):
    """Embedding provider contract.

    Implementations are async, batch-aware (accept a list of texts
    and return a list of vectors in the same order), and stateless
    beyond the API key and optional cache reference.
    """

    @property
    def model(self) -> str: ...
    @property
    def dimensions(self) -> int: ...

    async def embed(
        self, texts: list[str]
    ) -> list[list[float]]: ...


class EmbeddingCache:
    """Redis-backed embedding cache.

    Keys: ``embedding:{model}:{sha256(text)}``. Values: comma-joined
    floats (compact + JSON-safe) under a 24-hour TTL. The cache is
    opaque to embedder implementations — the same cache object can
    sit in front of any provider as long as the model string is
    namespaced into the key.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def get_many(
        self, model: str, texts: list[str]
    ) -> list[list[float] | None]:
        """Return cached vectors in the same order as ``texts``.

        ``None`` for cache misses. Callers re-embed the misses and
        write back via ``set_many``.
        """
        if not texts:
            return []
        keys = [_key(model, t) for t in texts]
        raw = await self._redis.mget(keys)
        return [_decode(v) for v in raw]

    async def set_many(
        self, model: str, mapping: dict[str, list[float]]
    ) -> None:
        if not mapping:
            return
        pipe = self._redis.pipeline(transaction=False)
        for text, vector in mapping.items():
            key = _key(model, text)
            pipe.set(key, _encode(vector), ex=self._ttl_seconds)
        await pipe.execute()


class VoyageEmbedder:
    """Voyage AI embedder. Default model ``voyage-3`` (1024-dim native)."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str = "voyage-3",
        input_type: str | None = "document",
        cache: EmbeddingCache | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        # input_type tells Voyage whether this is a search query
        # ("query") or content being indexed ("document"). Voyage
        # uses asymmetric encoders that perform better when told.
        # We default to "document" because the indexer is the
        # primary heavy caller; the retriever overrides to "query"
        # for its at-query-time embed.
        self._input_type = input_type
        self._cache = cache

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await _embed_with_cache(
            cache=self._cache,
            model=self._model,
            texts=texts,
            fetch=self._fetch,
        )

    async def _fetch(self, texts: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": self._model, "input": texts}
        if self._input_type is not None:
            body["input_type"] = self._input_type
        return await _post_embeddings(
            url=_VOYAGE_ENDPOINT,
            api_key=self._api_key,
            payload=body,
            provider="voyage",
        )


class OpenAIEmbedder:
    """OpenAI embedder. Default ``text-embedding-3-large`` truncated to 1024."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str = "text-embedding-3-large",
        dimensions: int = EMBEDDING_DIM,
        cache: EmbeddingCache | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._cache = cache

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await _embed_with_cache(
            cache=self._cache,
            model=self._model,
            texts=texts,
            fetch=self._fetch,
        )

    async def _fetch(self, texts: list[str]) -> list[list[float]]:
        body = {
            "model": self._model,
            "input": texts,
            "dimensions": self._dimensions,
        }
        return await _post_embeddings(
            url=_OPENAI_ENDPOINT,
            api_key=self._api_key,
            payload=body,
            provider="openai",
        )


def get_embedder(
    settings: Settings,
    cache: EmbeddingCache | None = None,
    *,
    input_type: str | None = None,
) -> Embedder:
    """Construct the embedder configured in ``Settings.EMBEDDING_PROVIDER``.

    ``input_type`` is passed through to providers that distinguish
    query vs document embeddings (Voyage). Callers indexing content
    pass ``None`` (or rely on the embedder's default of "document");
    the retriever passes ``"query"`` at search time.

    Raises:
        ConnectorAuthError: the configured provider's API key is
            missing on Settings. Surfaced early so the failure mode
            is "you forgot to set VOYAGE_API_KEY" rather than a
            confusing 401 deep in the retriever.
    """
    provider = settings.EMBEDDING_PROVIDER
    if provider == "voyage":
        if settings.VOYAGE_API_KEY is None:
            raise ConnectorAuthError(
                "EMBEDDING_PROVIDER=voyage but VOYAGE_API_KEY is not set"
            )
        return VoyageEmbedder(
            api_key=settings.VOYAGE_API_KEY,
            cache=cache,
            input_type=input_type or "document",
        )
    if provider == "openai":
        if settings.OPENAI_API_KEY is None:
            raise ConnectorAuthError(
                "EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set"
            )
        return OpenAIEmbedder(api_key=settings.OPENAI_API_KEY, cache=cache)
    # Pydantic's Literal validation should have caught this, but be
    # defensive — Settings could be constructed outside the env-
    # validation path in tests.
    raise ValueError(f"Unknown EMBEDDING_PROVIDER: {provider!r}")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _embed_with_cache(
    *,
    cache: EmbeddingCache | None,
    model: str,
    texts: list[str],
    fetch: Callable[[list[str]], Awaitable[list[list[float]]]],
) -> list[list[float]]:
    """Cache-aware embed orchestration.

    Two passes: gather cached vectors first; embed only the misses;
    stash the new vectors back into the cache. Preserves order so
    the returned list aligns 1:1 with ``texts``.
    """
    if not texts:
        return []
    if cache is None:
        return await fetch(texts)

    cached = await cache.get_many(model, texts)
    miss_indices = [i for i, v in enumerate(cached) if v is None]
    if not miss_indices:
        # Cast through; we know none are None now.
        return [v for v in cached if v is not None]

    miss_texts = [texts[i] for i in miss_indices]
    fetched = await fetch(miss_texts)
    if len(fetched) != len(miss_texts):
        raise ConnectorTransient(
            f"embedder returned {len(fetched)} vectors for "
            f"{len(miss_texts)} inputs"
        )

    new_cache: dict[str, list[float]] = {}
    out: list[list[float]] = []
    fetched_iter = iter(fetched)
    for i, cached_vec in enumerate(cached):
        if cached_vec is not None:
            out.append(cached_vec)
        else:
            vec = next(fetched_iter)
            out.append(vec)
            new_cache[texts[i]] = vec
    await cache.set_many(model, new_cache)
    return out


async def _post_embeddings(
    *,
    url: str,
    api_key: SecretStr,
    payload: dict[str, Any],
    provider: str,
) -> list[list[float]]:
    """POST to a provider's embeddings endpoint with the connector taxonomy."""
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as exc:
        raise ConnectorTransient(
            f"network error talking to {provider} embeddings"
        ) from exc

    status = response.status_code
    if 200 <= status < 300:
        body = response.json()
        data = body.get("data") or []
        result: list[list[float]] = [item["embedding"] for item in data]
        return result
    if status == 401 or status == 403:
        raise ConnectorAuthError(
            f"{provider} rejected embeddings request: HTTP {status}"
        )
    if status == 429:
        retry_after_raw = response.headers.get("Retry-After")
        try:
            retry_after = float(retry_after_raw) if retry_after_raw else None
        except (TypeError, ValueError):
            retry_after = None
        raise ConnectorRateLimited(retry_after=retry_after)
    if 500 <= status < 600:
        raise ConnectorTransient(
            f"{provider} embeddings returned {status}"
        )
    # Other 4xx — bad request, model not found, etc.
    raise ConnectorAuthError(
        f"{provider} embeddings returned {status}: {response.text[:200]}"
    )


def _key(model: str, text: str) -> str:
    digest = hashlib.sha256(
        f"{model}\0{text}".encode()
    ).hexdigest()
    return f"embedding:{model}:{digest}"


def _encode(vector: list[float]) -> str:
    return ",".join(repr(x) for x in vector)


def _decode(raw: object) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    if not isinstance(raw, str):
        return None
    try:
        return [float(piece) for piece in raw.split(",") if piece]
    except ValueError:
        return None
