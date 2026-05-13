"""Integration tests for the builtin tool catalogue.

Drives each handler directly with a constructed ``AgentContext``
against the real test DB. End-to-end tool-use-via-engine is
already covered in ``test_orchestrator_engine.py``; this file
focuses on the handlers' own contracts (correct DB queries,
right error shapes, correct payload mapping).
"""
import datetime as _dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import (
    Entity,
    EntityRelationship,
    Firm,
    Lesson,
)
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.memory.embeddings import EMBEDDING_DIM
from coworker.orchestrator.builtin_tools import (
    clock,
    kg,
    memory,
    register_builtin_tools,
)
from coworker.orchestrator.builtin_tools import (
    firm as firm_mod,
)
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import ToolError, ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tools_env(test_database_url):
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
        "agent_trace_steps", "agent_traces",
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
                "agent_trace_steps", "agent_traces",
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


async def _seed_firm(sm, *, timezone: str = "Australia/Sydney") -> tuple[uuid.UUID, Firm]:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id,
            name="Tools Firm",
            slug=f"t-{uuid.uuid4().hex[:8]}",
            timezone=timezone,
        )
        session.add(firm)
        await session.commit()
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        session.expunge(firm)
    return firm_id, firm


def _make_ctx(session, firm, *, embedder=None) -> AgentContext:
    return AgentContext(
        firm=firm,
        session=session,
        anthropic=None,  # type: ignore[arg-type]
        trace_id=uuid.uuid4(),
        embedder=embedder,
    )


class _FakeEmbedder:
    @property
    def model(self) -> str:
        return "fake"

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIM

    async def embed(self, texts):
        return [[0.5] * EMBEDDING_DIM for _ in texts]


# ===========================================================================
# get_firm_info
# ===========================================================================


async def test_get_firm_info_returns_firm_attributes(tools_env) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    tools_env["created"].append(firm_id)

    handler = firm_mod._get_firm_info_handler
    input_cls =firm_mod.GetFirmInfoInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached)
        result = await handler(input_cls(), ctx)

    assert result["name"] == "Tools Firm"
    assert result["slug"].startswith("t-")
    assert result["timezone"] == "Australia/Sydney"


# ===========================================================================
# get_today_date
# ===========================================================================


async def test_get_today_date_uses_firm_timezone(tools_env) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm, timezone="Australia/Sydney")
    tools_env["created"].append(firm_id)

    handler = clock._get_today_date_handler
    input_cls =clock.GetTodayDateInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached)
        result = await handler(input_cls(), ctx)

    assert result["timezone"] == "Australia/Sydney"
    assert isinstance(result["year"], int)
    assert result["weekday"] in {
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    }


async def test_get_today_date_explicit_timezone_overrides(tools_env) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm, timezone="Australia/Sydney")
    tools_env["created"].append(firm_id)

    handler = clock._get_today_date_handler
    input_cls =clock.GetTodayDateInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached)
        result = await handler(
            input_cls(timezone="America/New_York"), ctx
        )

    assert result["timezone"] == "America/New_York"


async def test_get_today_date_unknown_timezone_falls_back_to_utc(
    tools_env,
) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm, timezone="Made/Up")
    tools_env["created"].append(firm_id)

    handler = clock._get_today_date_handler
    input_cls =clock.GetTodayDateInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached)
        result = await handler(input_cls(), ctx)

    # tz_name reflects what the firm asked for; the actual datetime
    # falls back to UTC silently.
    assert result["timezone"] == "Made/Up"
    parsed = _dt.datetime.fromisoformat(result["datetime"])
    # The fallback uses UTC so the offset is +00:00.
    assert parsed.utcoffset() == _dt.timedelta(0)


# ===========================================================================
# memory_query
# ===========================================================================


async def test_memory_query_returns_hits_from_lessons(tools_env) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    tools_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(
            Lesson(
                firm_id=firm_id,
                text="Always check vehicle logbooks before FBT.",
                priority=5,
                embedding=[0.5] * EMBEDDING_DIM,
            )
        )
        await session.commit()

    handler = memory._memory_query_handler
    input_cls =memory.MemoryQueryInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached, embedder=_FakeEmbedder())
        result = await handler(
            input_cls(query="FBT vehicle logbooks", kinds=["lessons"], k=5),
            ctx,
        )

    assert len(result["hits"]) >= 1
    top = result["hits"][0]
    assert top["kind"] == "lessons"
    assert "FBT" in top["payload"]["text"]


async def test_memory_query_raises_tool_error_when_no_embedder(
    tools_env,
) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    tools_env["created"].append(firm_id)

    handler = memory._memory_query_handler
    input_cls =memory.MemoryQueryInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached, embedder=None)
        with pytest.raises(ToolError, match="no embedder"):
            await handler(input_cls(query="x"), ctx)


# ===========================================================================
# kg_entity_lookup
# ===========================================================================


async def test_kg_entity_lookup_returns_top_candidates(tools_env) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    tools_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(
            Entity(
                firm_id=firm_id, entity_type="company",
                name="Acme Pty Ltd",
            )
        )
        session.add(
            Entity(
                firm_id=firm_id, entity_type="trust",
                name="Smith Family Trust",
            )
        )
        await session.commit()

    handler = kg._kg_entity_lookup_handler
    input_cls =kg.KGEntityLookupInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached)
        result = await handler(input_cls(name="Acme Pty Ltd"), ctx)

    assert len(result["candidates"]) >= 1
    assert result["candidates"][0]["name"] == "Acme Pty Ltd"
    assert 0.0 <= result["candidates"][0]["similarity"] <= 1.0


# ===========================================================================
# kg_get_relationships
# ===========================================================================


async def test_kg_get_relationships_returns_edges(tools_env) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    tools_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        alice = Entity(firm_id=firm_id, entity_type="individual", name="Alice")
        acme = Entity(firm_id=firm_id, entity_type="company", name="Acme")
        session.add_all([alice, acme])
        await session.flush()
        session.add(
            EntityRelationship(
                firm_id=firm_id,
                from_entity_id=alice.id,
                to_entity_id=acme.id,
                relationship_type="director_of",
            )
        )
        await session.commit()
        alice_id = alice.id

    handler = kg._kg_get_relationships_handler
    input_cls =kg.KGGetRelationshipsInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached)
        out = await handler(
            input_cls(entity_id=str(alice_id), direction="out"), ctx,
        )

    assert len(out["edges"]) == 1
    assert out["edges"][0]["relationship_type"] == "director_of"


async def test_kg_get_relationships_unknown_entity_raises_tool_error(
    tools_env,
) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    tools_env["created"].append(firm_id)

    handler = kg._kg_get_relationships_handler
    input_cls =kg.KGGetRelationshipsInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached)
        with pytest.raises(ToolError, match="not found"):
            await handler(
                input_cls(entity_id=str(uuid.uuid4())), ctx,
            )


async def test_kg_get_relationships_bad_uuid_raises_tool_error(
    tools_env,
) -> None:
    sm = tools_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    tools_env["created"].append(firm_id)

    handler = kg._kg_get_relationships_handler
    input_cls =kg.KGGetRelationshipsInput

    async with sm() as session, firm_context(firm_id):
        attached = await session.merge(firm)
        ctx = _make_ctx(session, attached)
        with pytest.raises(ToolError, match="valid UUID"):
            await handler(
                input_cls(entity_id="not-a-uuid"), ctx,
            )


# ===========================================================================
# Registry assembly
# ===========================================================================


def test_register_builtin_tools_populates_registry() -> None:
    reg = ToolRegistry()
    register_builtin_tools(reg)

    names = {t.name for t in reg.all()}
    assert names == {
        "memory_query",
        "kg_entity_lookup",
        "kg_get_relationships",
        "get_firm_info",
        "get_today_date",
        "email_get_message",
        "email_create_draft",
        "email_mark_as_read",
    }


def test_builtin_tools_render_anthropic_definitions() -> None:
    """Sanity check: every builtin tool produces a valid Anthropic schema."""
    reg = ToolRegistry()
    register_builtin_tools(reg)
    defs = reg.to_anthropic_definitions()
    assert len(defs) == 8
    for d in defs:
        assert d["name"]
        assert d["description"]
        assert d["input_schema"]["type"] == "object"
