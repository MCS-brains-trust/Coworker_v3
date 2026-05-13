"""Integration tests for ``coworker.memory.retriever.HybridRetriever``.

Real Postgres test DB + real pgvector. Each test seeds a firm,
plants a handful of rows in client_interactions / lessons /
documents (with deterministic embeddings and text), then drives
the retriever and asserts on the returned order and metadata.

The embedder is mocked — every test uses a ``FakeEmbedder`` that
returns a known vector for a known input string. This keeps tests
hermetic and fast; the Voyage / OpenAI HTTP path has its own
coverage in ``test_embeddings.py``.
"""
import datetime as _dt
import uuid

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import (
    ClientInteraction,
    Document,
    Firm,
    Lesson,
)
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.memory.embeddings import EMBEDDING_DIM
from coworker.memory.retriever import (
    HybridRetriever,
    RetrievedItem,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def retriever_env(test_database_url):
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    created: list[uuid.UUID] = []
    try:
        yield {"sm": sm, "created": created}
    finally:
        for firm_id in created:
            await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = (
        "firms", "users", "audit_log", "token_usage",
        "client_interactions", "lessons", "documents",
        "entity_relationships", "entities", "jobs", "deadlines",
    )
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in (
                "entity_relationships", "deadlines", "jobs",
                "documents", "lessons", "client_interactions",
                "entities", "audit_log", "token_usage", "users",
            ):
                await session.execute(
                    text(f"DELETE FROM {t} WHERE firm_id = :id"),
                    {"id": str(firm_id)},
                )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"),
                {"id": str(firm_id)},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            )
        await session.commit()


async def _seed_firm(sm) -> uuid.UUID:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        session.add(
            Firm(id=firm_id, name="Retr Firm", slug=f"r-{uuid.uuid4().hex[:8]}")
        )
        await session.commit()
    return firm_id


def _vec(seed: float) -> list[float]:
    return [seed] * EMBEDDING_DIM


class FakeEmbedder:
    """Returns a deterministic vector per input from a lookup table.

    Unknown inputs fall back to the zero vector so vector-stream
    matches still happen but rank low against any seeded item.
    """

    def __init__(self, table: dict[str, list[float]]):
        self._table = table

    @property
    def model(self) -> str:
        return "fake-embedder-1"

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._table.get(t, [0.0] * EMBEDDING_DIM) for t in texts]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_bm25_only_match_returns_ranked_item(retriever_env) -> None:
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(
            ClientInteraction(
                firm_id=firm_id,
                interaction_type="email",
                subject="June quarter BAS lodgement",
                summary="Client lodged BAS on time",
                body="GST collected $11000, GST paid $4500",
                occurred_at=_dt.datetime(2025, 7, 28, tzinfo=_dt.UTC),
            )
        )
        await session.commit()

    embedder = FakeEmbedder({"BAS lodgement": _vec(0.5)})
    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session, embedder=embedder, firm_id=firm_id
        )
        items = await retriever.retrieve("BAS lodgement", k=5)

    assert len(items) == 1
    item = items[0]
    assert item.kind == "client_interactions"
    assert item.bm25_rank == 1
    # Embedding wasn't planted on the row, so vector stream filters it out.
    assert item.vector_rank is None
    assert "lodgement" in (item.payload.get("subject") or "").lower()


async def test_vector_only_match_returns_ranked_item(retriever_env) -> None:
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    # Insert a lesson whose embedding aligns with the query vector.
    target_vec = _vec(0.9)
    async with sm() as session, firm_context(firm_id):
        session.add(
            Lesson(
                firm_id=firm_id,
                text="Discount irrelevant phrasing fdsklfdsj here",
                category="other",
                priority=1,
                embedding=target_vec,
            )
        )
        await session.commit()

    # Query text doesn't appear in any tsv but the vector matches.
    embedder = FakeEmbedder({"obscure-query": target_vec})
    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session, embedder=embedder, firm_id=firm_id
        )
        items = await retriever.retrieve("obscure-query", k=5)

    assert len(items) == 1
    item = items[0]
    assert item.kind == "lessons"
    assert item.vector_rank == 1
    assert item.bm25_rank is None


async def test_rrf_fuses_both_streams_higher_for_dual_match(
    retriever_env,
) -> None:
    """An item that ranks in BOTH streams scores higher than one ranking
    in only one stream.
    """
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    target_vec = _vec(0.7)
    other_vec = _vec(0.05)

    async with sm() as session, firm_context(firm_id):
        # Dual-match item: text contains query terms AND embedding is the target.
        session.add(
            ClientInteraction(
                firm_id=firm_id,
                interaction_type="email",
                subject="Tax Deduction Question",
                summary="Client asks about FBT deduction",
                body="Specifically, motor vehicle FBT.",
                embedding=target_vec,
                occurred_at=_dt.datetime(2025, 5, 1, tzinfo=_dt.UTC),
            )
        )
        # Text-only match (similar terms, but unrelated embedding):
        session.add(
            ClientInteraction(
                firm_id=firm_id,
                interaction_type="email",
                subject="Tax Deduction Reminder",
                summary="A reminder about deductions deadline",
                body="More deduction notes.",
                embedding=other_vec,
                occurred_at=_dt.datetime(2025, 5, 2, tzinfo=_dt.UTC),
            )
        )
        await session.commit()

    embedder = FakeEmbedder({"FBT deduction": target_vec})
    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session, embedder=embedder, firm_id=firm_id
        )
        items = await retriever.retrieve("FBT deduction", k=5)

    # Both should appear. The dual-match item must rank above the text-
    # only one because it scored in both streams.
    assert len(items) >= 2
    top = items[0]
    assert top.bm25_rank is not None
    assert top.vector_rank is not None
    assert top.score > items[1].score


async def test_lesson_priority_multiplier_boosts_rank(retriever_env) -> None:
    """Two lessons with the same text match equally on BM25 + vector;
    the higher-priority one comes first after the multiplier.
    """
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    target_vec = _vec(0.6)
    async with sm() as session, firm_context(firm_id):
        # Identical text + embedding; only priority differs.
        session.add(
            Lesson(
                firm_id=firm_id,
                text="Audit clients should keep working papers seven years",
                category="audit",
                priority=10,
                embedding=target_vec,
            )
        )
        session.add(
            Lesson(
                firm_id=firm_id,
                text="Audit clients should keep working papers seven years",
                category="audit",
                priority=1,
                embedding=target_vec,
            )
        )
        await session.commit()

    embedder = FakeEmbedder({"audit working papers": target_vec})
    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session, embedder=embedder, firm_id=firm_id
        )
        items = await retriever.retrieve("audit working papers", k=5)

    assert len(items) == 2
    # Higher-priority lesson must come first.
    assert items[0].payload["priority"] >= items[1].payload["priority"]
    assert items[0].score > items[1].score


async def test_kinds_filter_narrows_search_scope(retriever_env) -> None:
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(
            ClientInteraction(
                firm_id=firm_id,
                interaction_type="email",
                subject="Engagement letter signed",
                summary="Client returned signed engagement",
                body="...",
            )
        )
        session.add(
            Document(
                firm_id=firm_id,
                source="kb",
                doc_type="engagement_template",
                title="Standard engagement template",
                summary="Reference template for engagement letters",
                body="...",
            )
        )
        await session.commit()

    embedder = FakeEmbedder({"engagement": _vec(0.1)})
    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session, embedder=embedder, firm_id=firm_id
        )
        # Narrow to documents only.
        items = await retriever.retrieve(
            "engagement", k=5, kinds=["documents"]
        )

    assert all(i.kind == "documents" for i in items)
    assert any(
        (i.payload.get("title") or "").startswith("Standard engagement")
        for i in items
    )


async def test_inactive_lessons_excluded_from_results(retriever_env) -> None:
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(
            Lesson(
                firm_id=firm_id,
                text="Active lesson about superannuation contributions",
                priority=1,
                is_active=True,
                embedding=_vec(0.4),
            )
        )
        session.add(
            Lesson(
                firm_id=firm_id,
                text="Inactive lesson about superannuation contributions",
                priority=1,
                is_active=False,
                embedding=_vec(0.4),
            )
        )
        await session.commit()

    embedder = FakeEmbedder({"superannuation": _vec(0.4)})
    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session, embedder=embedder, firm_id=firm_id
        )
        items = await retriever.retrieve("superannuation", k=5)

    # Only the active lesson should appear.
    assert len(items) == 1
    assert items[0].kind == "lessons"
    assert items[0].payload["is_active"] is True


async def test_empty_query_returns_empty_list(retriever_env) -> None:
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session,
            embedder=FakeEmbedder({}),
            firm_id=firm_id,
        )
        assert await retriever.retrieve("", k=5) == []
        assert await retriever.retrieve("   ", k=5) == []


async def test_unknown_kind_raises_value_error(retriever_env) -> None:
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session,
            embedder=FakeEmbedder({"x": _vec(0.0)}),
            firm_id=firm_id,
        )
        try:
            await retriever.retrieve("x", k=5, kinds=["wibble"])  # type: ignore[list-item]
            crashed = False
        except ValueError:
            crashed = True
        assert crashed


async def test_rerank_reorders_top_pool(retriever_env) -> None:
    """A custom reranker can reorder the top pool while leaving the
    tail untouched.
    """
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    target_vec = _vec(0.3)
    async with sm() as session, firm_context(firm_id):
        # Three interactions, two will match the query.
        session.add(
            ClientInteraction(
                firm_id=firm_id,
                interaction_type="email",
                subject="GST refund inquiry",
                summary="Client asks about GST refund",
                body="More GST refund details",
                embedding=target_vec,
                occurred_at=_dt.datetime(2025, 5, 1, tzinfo=_dt.UTC),
            )
        )
        session.add(
            ClientInteraction(
                firm_id=firm_id,
                interaction_type="email",
                subject="GST quarterly report",
                summary="Client filed GST quarterly",
                body="GST quarterly report submitted",
                embedding=target_vec,
                occurred_at=_dt.datetime(2025, 5, 2, tzinfo=_dt.UTC),
            )
        )
        await session.commit()

    embedder = FakeEmbedder({"GST refund": target_vec})

    async def reranker(query, pairs):
        # Score the "refund inquiry" item highest deterministically.
        return [
            10.0 if "refund" in snippet.lower() else 1.0
            for _, snippet in pairs
        ]

    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session, embedder=embedder, firm_id=firm_id,
            reranker=reranker,
        )
        items = await retriever.retrieve(
            "GST refund", k=5, rerank=True, rerank_pool_size=5,
        )

    assert items[0].score == 10.0
    assert "refund" in (items[0].payload.get("subject") or "").lower()


async def test_cache_hit_returns_cached_items(retriever_env, monkeypatch) -> None:
    """When the cache has a value, the retriever returns it without
    embedding the query or hitting the DB.
    """
    sm = retriever_env["sm"]
    firm_id = await _seed_firm(sm)
    retriever_env["created"].append(firm_id)

    cached_items = [
        RetrievedItem(
            kind="lessons",
            row_id=uuid.uuid4(),
            score=0.99,
            bm25_rank=1,
            vector_rank=1,
            payload={"text": "Cached lesson"},
        )
    ]

    class _StubCache:
        async def get(self, key):
            return cached_items

        async def set(self, key, items):
            return None

    class _LoudEmbedder:
        @property
        def model(self) -> str:
            return "loud"

        @property
        def dimensions(self) -> int:
            return EMBEDDING_DIM

        async def embed(self, texts):  # pragma: no cover - must not run
            raise AssertionError("cache hit must skip embedder")

    async with sm() as session, firm_context(firm_id):
        retriever = HybridRetriever(
            session=session,
            embedder=_LoudEmbedder(),
            firm_id=firm_id,
            cache=_StubCache(),  # type: ignore[arg-type]
        )
        items = await retriever.retrieve("anything", k=5)

    assert items == cached_items
