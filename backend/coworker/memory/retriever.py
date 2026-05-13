"""Hybrid retriever: BM25 + vector + Reciprocal Rank Fusion.

Single entry point for "give me the most relevant rows from the
memory layer." Searches across ``client_interactions``, ``lessons``,
and ``documents`` (the three indexed tables from Phase 4A) and
returns a unified, ranked list.

Two scoring streams run in parallel:

- **BM25** over the weighted ``tsv`` column via
  ``ts_rank_cd(tsv, plainto_tsquery('english', q))``. Subject /
  title weighted A, summary B, body / lesson text C (the trigger
  sets that up at insert time).
- **Vector cosine distance** via pgvector's ``<=>`` operator on the
  ``embedding`` column. The query is embedded by the configured
  ``Embedder`` (Voyage by default, OpenAI optional).

Both streams' top-K results merge via Reciprocal Rank Fusion: each
item's final score is ``sum over streams of 1 / (rrf_k + rank)``.
The ``rrf_k`` constant defaults to 60, the value Reciprocal Rank
Fusion's original paper found robust across many domains.

Lesson priority multiplier
--------------------------

Lessons carry a ``priority`` field that boosts their rank
multiplicatively after RRF (``score * (1 + priority * 0.1)``). High-
priority lessons surface even when BM25 and vector rank them lower
in raw relevance; the multiplier is intentional bias that reflects
"the firm has told us this lesson is important."

Sonnet rerank
-------------

Optional. When ``rerank=True``, the retriever forwards the top
``rerank_pool_size`` items to Claude Sonnet for relevance scoring
against the original query, then re-sorts by the rerank score. The
Anthropic call is wired through a caller-supplied callable so this
module stays free of an Anthropic dependency; the orchestrator
will pass a ``rerank_with_anthropic`` function constructed against
its per-firm AnthropicClient.

Redis cache
-----------

Optional. When a ``RetrieverCache`` is supplied, the retriever
keys cached results by
``retriever:{firm_id}:{sha256(query+kinds+k+rerank)}`` with a
24-hour TTL. Cache misses run the full hybrid retrieval and write
back. The cache is firm-scoped (the firm_id is in the key, and we
key against the firm whose firm_context is active) so cross-firm
leakage is impossible.
"""
import datetime as _dt
import hashlib
import json
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.memory.embeddings import Embedder

_KindLiteral = Literal["client_interactions", "lessons", "documents"]
_VALID_KINDS: tuple[_KindLiteral, ...] = (
    "client_interactions",
    "lessons",
    "documents",
)
_DEFAULT_RRF_K = 60
_DEFAULT_PER_STREAM_LIMIT = 50
_DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class RetrievedItem:
    """One result from the retriever.

    ``kind`` identifies the source table; ``row_id`` is the table's
    UUID PK. ``score`` is the final fused score (higher is better);
    ``bm25_rank`` / ``vector_rank`` are 1-based ranks within each
    stream (None if the item didn't appear in that stream's top-K).
    ``payload`` carries the row's text content for the consumer
    (subject + summary + body for interactions, text for lessons,
    title + summary for documents) without re-loading the row.
    """

    kind: _KindLiteral
    row_id: uuid.UUID
    score: float
    bm25_rank: int | None
    vector_rank: int | None
    payload: dict[str, Any]


class RetrieverCache:
    """Redis-backed result cache for the hybrid retriever.

    Cache hits return the exact list returned previously; cache
    misses run the full retrieval. 24-hour TTL — most firms' query
    patterns repeat heavily within a day (Smart Responder reprocesses
    the same email after edits), much less so across days.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def get(self, key: str) -> list[RetrievedItem] | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return [
            RetrievedItem(
                kind=item["kind"],
                row_id=uuid.UUID(item["row_id"]),
                score=float(item["score"]),
                bm25_rank=item.get("bm25_rank"),
                vector_rank=item.get("vector_rank"),
                payload=item.get("payload", {}),
            )
            for item in data
        ]

    async def set(
        self, key: str, items: list[RetrievedItem]
    ) -> None:
        payload = json.dumps(
            [
                {
                    "kind": item.kind,
                    "row_id": str(item.row_id),
                    "score": item.score,
                    "bm25_rank": item.bm25_rank,
                    "vector_rank": item.vector_rank,
                    "payload": item.payload,
                }
                for item in items
            ]
        )
        await self._redis.set(key, payload, ex=self._ttl_seconds)


# A reranker is a callable: given the original query and a list of
# (item, snippet_text), return a list of scores in the same order.
# Pass None to skip reranking; the orchestrator constructs one from
# its per-firm AnthropicClient.
Reranker = Callable[
    [str, list[tuple[RetrievedItem, str]]],
    Awaitable[list[float]],
]


class HybridRetriever:
    """Hybrid BM25 + vector retriever over the memory layer.

    Construct per request with a session already inside firm_context
    (RLS does the per-firm filtering — we don't need to mention
    firm_id in the WHERE clauses). The retriever is stateless across
    calls, but the cache (if any) is per-process.
    """

    def __init__(
        self,
        session: AsyncSession,
        embedder: Embedder,
        *,
        firm_id: uuid.UUID,
        cache: RetrieverCache | None = None,
        reranker: Reranker | None = None,
        rrf_k: int = _DEFAULT_RRF_K,
        per_stream_limit: int = _DEFAULT_PER_STREAM_LIMIT,
    ) -> None:
        self._session = session
        self._embedder = embedder
        self._firm_id = firm_id
        self._cache = cache
        self._reranker = reranker
        self._rrf_k = rrf_k
        self._per_stream_limit = per_stream_limit

    async def retrieve(
        self,
        query: str,
        *,
        kinds: list[_KindLiteral] | None = None,
        k: int = 10,
        rerank: bool = False,
        rerank_pool_size: int = 20,
    ) -> list[RetrievedItem]:
        """Run hybrid retrieval and return the top ``k`` items.

        Args:
            query: the natural-language search string.
            kinds: which tables to search. ``None`` searches all
                three (``client_interactions`` + ``lessons`` +
                ``documents``).
            k: number of items to return.
            rerank: when True and ``self._reranker`` is set, apply
                Sonnet reranking to the top ``rerank_pool_size``
                items before truncating to ``k``.
            rerank_pool_size: how many top items to consider for
                reranking (defaults to 2x k to give the reranker
                room to reorder).

        Returns:
            ``[]`` for an empty query or zero kinds. Otherwise a
            list of ``RetrievedItem`` sorted by descending final
            score, length ≤ ``k``.

        Raises:
            ValueError: query empty, k < 1, or kinds contains a
                value outside ``_VALID_KINDS``.
        """
        if k < 1:
            raise ValueError("k must be >= 1")
        if not query or not query.strip():
            return []

        target_kinds = list(kinds) if kinds is not None else list(_VALID_KINDS)
        for kind in target_kinds:
            if kind not in _VALID_KINDS:
                raise ValueError(
                    f"Unknown kind {kind!r}; expected one of {_VALID_KINDS}"
                )
        if not target_kinds:
            return []

        cache_key = self._build_cache_key(query, target_kinds, k, rerank)
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        # Embed the query once, used by every vector-stream query.
        query_vectors = await self._embedder.embed([query])
        if not query_vectors:
            return []
        query_vec = query_vectors[0]

        # Per-kind streams — each kind contributes a BM25 stream and
        # a vector stream. Sequencing here is sequential per kind
        # but cheap (each query is a single index hit) and avoids
        # the asyncio.gather complexity for what is already a fast
        # set of round-trips.
        bm25_results: dict[tuple[str, uuid.UUID], int] = {}
        vector_results: dict[tuple[str, uuid.UUID], int] = {}
        payloads: dict[tuple[str, uuid.UUID], dict[str, Any]] = {}

        for kind in target_kinds:
            bm25_rows = await self._bm25_for_kind(kind, query)
            for rank, (row_id, payload) in enumerate(bm25_rows, start=1):
                key = (kind, row_id)
                bm25_results[key] = rank
                payloads.setdefault(key, payload)

            vec_rows = await self._vector_for_kind(kind, query_vec)
            for rank, (row_id, payload) in enumerate(vec_rows, start=1):
                key = (kind, row_id)
                vector_results[key] = rank
                payloads.setdefault(key, payload)

        # RRF merge.
        candidates: set[tuple[str, uuid.UUID]] = set(bm25_results) | set(
            vector_results
        )
        items: list[RetrievedItem] = []
        for (kind_str, row_id) in candidates:
            bm25_rank = bm25_results.get((kind_str, row_id))
            vec_rank = vector_results.get((kind_str, row_id))
            score = 0.0
            if bm25_rank is not None:
                score += 1.0 / (self._rrf_k + bm25_rank)
            if vec_rank is not None:
                score += 1.0 / (self._rrf_k + vec_rank)

            # Lesson priority multiplier — see module docstring.
            if kind_str == "lessons":
                priority = int(payloads[(kind_str, row_id)].get("priority", 1))
                score *= 1.0 + max(0, priority) * 0.1

            items.append(
                RetrievedItem(
                    kind=kind_str,  # type: ignore[arg-type]
                    row_id=row_id,
                    score=score,
                    bm25_rank=bm25_rank,
                    vector_rank=vec_rank,
                    payload=payloads[(kind_str, row_id)],
                )
            )

        items.sort(key=lambda i: i.score, reverse=True)

        if rerank and self._reranker is not None and items:
            pool = items[:rerank_pool_size]
            scored = await self._reranker(
                query, [(i, _snippet(i)) for i in pool]
            )
            if len(scored) != len(pool):
                # Defensive: a malformed reranker shouldn't silently
                # drop or duplicate items. Fall back to RRF order.
                pass
            else:
                rescored = sorted(
                    (
                        RetrievedItem(
                            kind=item.kind,
                            row_id=item.row_id,
                            score=float(new_score),
                            bm25_rank=item.bm25_rank,
                            vector_rank=item.vector_rank,
                            payload=item.payload,
                        )
                        for item, new_score in zip(pool, scored, strict=True)
                    ),
                    key=lambda i: i.score,
                    reverse=True,
                )
                items = rescored + items[rerank_pool_size:]

        result = items[:k]
        if self._cache is not None:
            await self._cache.set(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # Per-kind stream queries
    # ------------------------------------------------------------------

    async def _bm25_for_kind(
        self, kind: _KindLiteral, query: str
    ) -> list[tuple[uuid.UUID, dict[str, Any]]]:
        sql_text, payload_fields = _bm25_sql(kind)
        result = await self._session.execute(
            text(sql_text),
            {"q": query, "limit": self._per_stream_limit},
        )
        return [
            (row[0], _row_payload(payload_fields, tuple(row[1:])))
            for row in result.all()
        ]

    async def _vector_for_kind(
        self, kind: _KindLiteral, query_vec: list[float]
    ) -> list[tuple[uuid.UUID, dict[str, Any]]]:
        # pgvector accepts the vector as a string literal of the form
        # '[v1,v2,...]'. We send it through the parameter binding so
        # SQLAlchemy + asyncpg handle quoting consistently with the
        # rest of the codebase.
        sql_text, payload_fields = _vector_sql(kind)
        result = await self._session.execute(
            text(sql_text),
            {
                "q": _format_vector_literal(query_vec),
                "limit": self._per_stream_limit,
            },
        )
        return [
            (row[0], _row_payload(payload_fields, tuple(row[1:])))
            for row in result.all()
        ]

    def _build_cache_key(
        self,
        query: str,
        kinds: list[_KindLiteral],
        k: int,
        rerank: bool,
    ) -> str:
        material = json.dumps(
            {
                "q": query,
                "kinds": sorted(kinds),
                "k": k,
                "rerank": rerank,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(material.encode()).hexdigest()
        return f"retriever:{self._firm_id}:{digest}"


# ---------------------------------------------------------------------------
# Per-kind SQL builders. The text fields differ between tables; the
# returned-column tuple is mirrored in ``_row_payload``.
# ---------------------------------------------------------------------------


def _bm25_sql(kind: _KindLiteral) -> tuple[str, tuple[str, ...]]:
    """Return (SQL text, payload field names) for the BM25 query.

    All queries select ``(id, ...text_columns)`` in a stable column
    order so ``_row_payload`` can zip them without ambiguity.
    """
    if kind == "client_interactions":
        sql = """
            SELECT id, subject, summary, occurred_at::text
            FROM client_interactions
            WHERE tsv @@ plainto_tsquery('english', :q)
            ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', :q)) DESC
            LIMIT :limit
        """
        return sql, ("subject", "summary", "occurred_at")
    if kind == "lessons":
        sql = """
            SELECT id, text, priority, category, is_active
            FROM lessons
            WHERE tsv @@ plainto_tsquery('english', :q)
              AND is_active = TRUE
            ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', :q)) DESC
            LIMIT :limit
        """
        return sql, ("text", "priority", "category", "is_active")
    if kind == "documents":
        sql = """
            SELECT id, title, summary, doc_type
            FROM documents
            WHERE tsv @@ plainto_tsquery('english', :q)
            ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', :q)) DESC
            LIMIT :limit
        """
        return sql, ("title", "summary", "doc_type")
    raise ValueError(f"unhandled kind {kind!r}")


def _vector_sql(kind: _KindLiteral) -> tuple[str, tuple[str, ...]]:
    if kind == "client_interactions":
        sql = """
            SELECT id, subject, summary, occurred_at::text
            FROM client_interactions
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:q AS vector) ASC
            LIMIT :limit
        """
        return sql, ("subject", "summary", "occurred_at")
    if kind == "lessons":
        sql = """
            SELECT id, text, priority, category, is_active
            FROM lessons
            WHERE embedding IS NOT NULL AND is_active = TRUE
            ORDER BY embedding <=> CAST(:q AS vector) ASC
            LIMIT :limit
        """
        return sql, ("text", "priority", "category", "is_active")
    if kind == "documents":
        sql = """
            SELECT id, title, summary, doc_type
            FROM documents
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:q AS vector) ASC
            LIMIT :limit
        """
        return sql, ("title", "summary", "doc_type")
    raise ValueError(f"unhandled kind {kind!r}")


def _row_payload(
    fields: tuple[str, ...], values: tuple[Any, ...]
) -> dict[str, Any]:
    return {
        name: (
            value.isoformat() if isinstance(value, _dt.datetime) else value
        )
        for name, value in zip(fields, values, strict=False)
    }


def _format_vector_literal(vec: list[float]) -> str:
    """Format a Python list of floats into pgvector's text literal.

    pgvector accepts ``'[v1,v2,...]'`` when cast to ``vector``.
    NaN / Inf would crash the cast; we coerce to 0.0 defensively
    rather than silently fail the whole query.
    """

    def _clean(x: float) -> float:
        if not math.isfinite(x):
            return 0.0
        return float(x)

    return "[" + ",".join(repr(_clean(x)) for x in vec) + "]"


def _snippet(item: RetrievedItem, max_len: int = 600) -> str:
    """Build a short snippet from a retrieved item's payload for reranking.

    Keeps the prompt manageable when the reranker batches many items;
    600 chars is enough context for a one-paragraph summary without
    overwhelming the Sonnet context window for a 20-item pool.
    """
    payload = item.payload
    if item.kind == "client_interactions":
        text_parts = [payload.get("subject") or "", payload.get("summary") or ""]
    elif item.kind == "lessons":
        text_parts = [payload.get("text") or ""]
    elif item.kind == "documents":
        text_parts = [
            payload.get("title") or "",
            payload.get("summary") or "",
        ]
    else:
        text_parts = []
    raw = " — ".join(p for p in text_parts if p)
    return raw[:max_len]
