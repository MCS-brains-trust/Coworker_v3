"""Integration tests for ``coworker.knowledge_graph.sharepoint_resolver``.

Real Postgres test DB so the pg_trgm GIN index from the Phase 4A
migration actually drives the queries.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import Entity, Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.knowledge_graph.sharepoint_resolver import (
    resolve_folder_candidates,
    resolve_folder_to_entity,
)


@pytest_asyncio.fixture
async def resolver_env(test_database_url):
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
            Firm(id=firm_id, name="Resolver Firm", slug=f"r-{uuid.uuid4().hex[:8]}")
        )
        await session.commit()
    return firm_id


async def _seed_entities(sm, firm_id: uuid.UUID, names: list[str]) -> None:
    async with sm() as session, firm_context(firm_id):
        for name in names:
            session.add(
                Entity(
                    firm_id=firm_id,
                    entity_type="company",
                    name=name,
                )
            )
        await session.commit()


# ---------------------------------------------------------------------------


async def test_exact_match_returns_high_similarity(resolver_env) -> None:
    sm = resolver_env["sm"]
    firm_id = await _seed_firm(sm)
    resolver_env["created"].append(firm_id)
    await _seed_entities(sm, firm_id, ["Acme Pty Ltd", "Beta Trust"])

    async with sm() as session, firm_context(firm_id):
        match = await resolve_folder_to_entity(
            session, folder_name="Acme Pty Ltd"
        )

    assert match is not None
    assert match.entity_name == "Acme Pty Ltd"
    assert match.similarity == pytest.approx(1.0)


async def test_minor_typo_still_matches(resolver_env) -> None:
    sm = resolver_env["sm"]
    firm_id = await _seed_firm(sm)
    resolver_env["created"].append(firm_id)
    await _seed_entities(sm, firm_id, ["Smith Family Trust"])

    async with sm() as session, firm_context(firm_id):
        match = await resolve_folder_to_entity(
            session, folder_name="Smith Famly Trust"  # typo: missing 'i'
        )

    assert match is not None
    assert match.entity_name == "Smith Family Trust"
    assert 0.4 <= match.similarity < 1.0


async def test_completely_unrelated_returns_none(resolver_env) -> None:
    sm = resolver_env["sm"]
    firm_id = await _seed_firm(sm)
    resolver_env["created"].append(firm_id)
    await _seed_entities(sm, firm_id, ["Acme Pty Ltd"])

    async with sm() as session, firm_context(firm_id):
        match = await resolve_folder_to_entity(
            session, folder_name="Totally Different Name XYZ"
        )

    assert match is None


async def test_picks_highest_similarity_when_multiple_match(resolver_env) -> None:
    sm = resolver_env["sm"]
    firm_id = await _seed_firm(sm)
    resolver_env["created"].append(firm_id)
    await _seed_entities(
        sm, firm_id,
        ["Acme Pty Ltd", "Acme Holdings Pty Ltd", "Acme Trading"],
    )

    async with sm() as session, firm_context(firm_id):
        match = await resolve_folder_to_entity(
            session, folder_name="Acme Pty Ltd"
        )

    assert match is not None
    assert match.entity_name == "Acme Pty Ltd"  # exact match wins


async def test_empty_folder_name_returns_none(resolver_env) -> None:
    sm = resolver_env["sm"]
    firm_id = await _seed_firm(sm)
    resolver_env["created"].append(firm_id)
    await _seed_entities(sm, firm_id, ["Acme Pty Ltd"])

    async with sm() as session, firm_context(firm_id):
        assert (
            await resolve_folder_to_entity(session, folder_name="")
        ) is None
        assert (
            await resolve_folder_to_entity(session, folder_name="   ")
        ) is None


async def test_threshold_parameter_gates_match(resolver_env) -> None:
    """A high threshold rejects a near-match the default would accept."""
    sm = resolver_env["sm"]
    firm_id = await _seed_firm(sm)
    resolver_env["created"].append(firm_id)
    await _seed_entities(sm, firm_id, ["Smith Family Trust"])

    async with sm() as session, firm_context(firm_id):
        default_match = await resolve_folder_to_entity(
            session, folder_name="Smith Famly Trust"
        )
        strict_match = await resolve_folder_to_entity(
            session, folder_name="Smith Famly Trust", threshold=0.99,
        )

    assert default_match is not None
    assert strict_match is None  # 0.99 too strict for a typo


async def test_invalid_threshold_raises_value_error(resolver_env) -> None:
    sm = resolver_env["sm"]
    firm_id = await _seed_firm(sm)
    resolver_env["created"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        with pytest.raises(ValueError):
            await resolve_folder_to_entity(
                session, folder_name="x", threshold=-0.1,
            )
        with pytest.raises(ValueError):
            await resolve_folder_to_entity(
                session, folder_name="x", threshold=1.5,
            )
        with pytest.raises(ValueError):
            await resolve_folder_to_entity(
                session, folder_name="x", top_k=0,
            )


async def test_resolve_candidates_returns_sorted_list(resolver_env) -> None:
    sm = resolver_env["sm"]
    firm_id = await _seed_firm(sm)
    resolver_env["created"].append(firm_id)
    await _seed_entities(
        sm, firm_id,
        ["Smith Family Trust", "Smith Holdings", "Smith Brothers Pty Ltd"],
    )

    async with sm() as session, firm_context(firm_id):
        result = await resolve_folder_candidates(
            session, folder_name="Smith Family Trust", top_k=3,
        )

    # All three Smith* entities pass the threshold, sorted by similarity.
    assert len(result.candidates) >= 1
    sims = [c.similarity for c in result.candidates]
    assert sims == sorted(sims, reverse=True)
    assert result.candidates[0].entity_name == "Smith Family Trust"


async def test_rls_isolates_resolver_across_firms(resolver_env) -> None:
    """A firm's resolver MUST NOT see another firm's entities, even with
    a near-perfect name match.
    """
    sm = resolver_env["sm"]
    firm_a = await _seed_firm(sm)
    firm_b = await _seed_firm(sm)
    resolver_env["created"].extend([firm_a, firm_b])

    # Same name in both firms — verify the resolver only finds the one
    # in the active firm_context.
    await _seed_entities(sm, firm_a, ["Acme Pty Ltd"])
    await _seed_entities(sm, firm_b, ["Acme Pty Ltd"])

    async with sm() as session, firm_context(firm_a):
        match_a = await resolve_folder_to_entity(
            session, folder_name="Acme Pty Ltd"
        )

    async with sm() as session, firm_context(firm_b):
        match_b = await resolve_folder_to_entity(
            session, folder_name="Acme Pty Ltd"
        )

    assert match_a is not None
    assert match_b is not None
    # Different IDs prove the resolver returned each firm's own row.
    assert match_a.entity_id != match_b.entity_id
