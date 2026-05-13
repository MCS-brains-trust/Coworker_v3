"""Integration tests for ``coworker.knowledge_graph.xpm_sync``.

Real Postgres test DB (so we exercise the partial unique index for
relationships and the entity find-or-update path). The XPM API is
mocked by a ``FakeXPMClient`` that returns pre-built records; the
real ``XPMClient.list_clients`` / ``list_relationships`` paths have
their own coverage in ``test_xpm_client.py``.
"""
import datetime as _dt
import uuid

import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.connectors.xpm_client import (
    XPMClientRecord,
    XPMRelationship,
)
from coworker.db.models import Entity, EntityRelationship, Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.knowledge_graph.xpm_sync import sync_xpm_clients_to_kg

# ---------------------------------------------------------------------------
# FakeXPMClient — minimal interface to satisfy sync_xpm_clients_to_kg
# ---------------------------------------------------------------------------


class _FakeFirm:
    def __init__(self, firm_id: uuid.UUID):
        self.id = firm_id


class FakeXPMClient:
    """Stub matching only the surface the sync uses."""

    def __init__(
        self,
        firm_id: uuid.UUID,
        clients: list[XPMClientRecord],
        relationships: dict[str, list[XPMRelationship]],
    ):
        self._firm = _FakeFirm(firm_id)
        self._clients = clients
        self._relationships = relationships

    @property
    def firm(self) -> _FakeFirm:
        return self._firm

    async def list_clients(
        self, *, updated_since: _dt.datetime | None = None
    ) -> list[XPMClientRecord]:
        return list(self._clients)

    async def list_relationships(self, client_id: str) -> list[XPMRelationship]:
        return list(self._relationships.get(client_id, []))


def _client(
    cid: str, name: str, entity_type: str = "Company"
) -> XPMClientRecord:
    return XPMClientRecord(
        id=cid,
        name=name,
        entity_type=entity_type,
        created_at=_dt.datetime(2020, 1, 1, tzinfo=_dt.UTC),
        modified_at=_dt.datetime(2024, 1, 1, tzinfo=_dt.UTC),
    )


def _edge(
    rid: str,
    from_id: str,
    to_id: str,
    rel_type: str = "Director",
    active: bool = True,
) -> XPMRelationship:
    return XPMRelationship(
        id=rid,
        from_client_id=from_id,
        to_client_id=to_id,
        relationship_type=rel_type,
        is_active=active,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def xpm_sync_env(test_database_url):
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
            Firm(id=firm_id, name="XPM Sync Firm", slug=f"xs-{uuid.uuid4().hex[:8]}")
        )
        await session.commit()
    return firm_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_first_sync_creates_entities_and_edges(xpm_sync_env) -> None:
    sm = xpm_sync_env["sm"]
    firm_id = await _seed_firm(sm)
    xpm_sync_env["created"].append(firm_id)

    clients = [
        _client("c-1", "Acme Pty Ltd", "Company"),
        _client("c-2", "Alice Director", "Individual"),
    ]
    edges = {"c-2": [_edge("e-1", "c-2", "c-1", "Director")]}
    fake = FakeXPMClient(firm_id, clients, edges)

    async with sm() as session, firm_context(firm_id):
        stats = await sync_xpm_clients_to_kg(fake, session)

    assert stats.clients_seen == 2
    assert stats.entities_created == 2
    assert stats.entities_updated == 0
    assert stats.edges_upserted == 1
    assert stats.errors == []

    async with sm() as session, firm_context(firm_id):
        ents = (
            await session.execute(
                select(Entity).where(Entity.firm_id == firm_id)
                .order_by(Entity.xpm_client_id)
            )
        ).scalars().all()
        assert {(e.xpm_client_id, e.entity_type) for e in ents} == {
            ("c-1", "company"),
            ("c-2", "individual"),
        }

        edges_db = (
            await session.execute(
                select(EntityRelationship)
                .where(EntityRelationship.firm_id == firm_id)
            )
        ).scalars().all()
        assert len(edges_db) == 1
        edge = edges_db[0]
        assert edge.relationship_type == "director_of"
        assert edge.is_active is True
        assert edge.provenance["source"] == "xpm"
        assert "synced_at" in edge.provenance


async def test_resync_updates_existing_entities_without_duplication(
    xpm_sync_env,
) -> None:
    sm = xpm_sync_env["sm"]
    firm_id = await _seed_firm(sm)
    xpm_sync_env["created"].append(firm_id)

    initial = FakeXPMClient(
        firm_id,
        [_client("c-1", "Acme Pty Ltd")],
        {},
    )
    async with sm() as session, firm_context(firm_id):
        first = await sync_xpm_clients_to_kg(initial, session)
    assert first.entities_created == 1

    # Same xpm_client_id, different name — should update, not create new.
    second_run = FakeXPMClient(
        firm_id,
        [_client("c-1", "Acme Pty Ltd (renamed)")],
        {},
    )
    async with sm() as session, firm_context(firm_id):
        second = await sync_xpm_clients_to_kg(second_run, session)
    assert second.entities_created == 0
    assert second.entities_updated == 1

    async with sm() as session, firm_context(firm_id):
        ents = (
            await session.execute(
                select(Entity).where(Entity.xpm_client_id == "c-1")
            )
        ).scalars().all()
        assert len(ents) == 1
        assert ents[0].name == "Acme Pty Ltd (renamed)"


async def test_resync_does_not_duplicate_active_edges(xpm_sync_env) -> None:
    """The unique partial index ensures the same active edge is one row."""
    sm = xpm_sync_env["sm"]
    firm_id = await _seed_firm(sm)
    xpm_sync_env["created"].append(firm_id)

    clients = [
        _client("c-1", "Acme", "Company"),
        _client("c-2", "Alice", "Individual"),
    ]
    edges = {"c-2": [_edge("e-1", "c-2", "c-1", "Director")]}
    fake = FakeXPMClient(firm_id, clients, edges)

    async with sm() as session, firm_context(firm_id):
        await sync_xpm_clients_to_kg(fake, session)

    # Re-sync identical data: edge UPSERT must update provenance, not
    # insert a second row.
    async with sm() as session, firm_context(firm_id):
        stats = await sync_xpm_clients_to_kg(fake, session)
    assert stats.edges_upserted == 1

    async with sm() as session, firm_context(firm_id):
        rows = (
            await session.execute(
                select(EntityRelationship)
                .where(EntityRelationship.firm_id == firm_id)
                .where(EntityRelationship.is_active.is_(True))
            )
        ).scalars().all()
        assert len(rows) == 1


async def test_inactive_xpm_edge_deactivates_kg_edge(xpm_sync_env) -> None:
    sm = xpm_sync_env["sm"]
    firm_id = await _seed_firm(sm)
    xpm_sync_env["created"].append(firm_id)

    clients = [
        _client("c-1", "Acme", "Company"),
        _client("c-2", "Alice", "Individual"),
    ]
    fake_active = FakeXPMClient(
        firm_id, clients,
        {"c-2": [_edge("e-1", "c-2", "c-1", "Director", active=True)]},
    )
    async with sm() as session, firm_context(firm_id):
        await sync_xpm_clients_to_kg(fake_active, session)

    fake_inactive = FakeXPMClient(
        firm_id, clients,
        {"c-2": [_edge("e-1", "c-2", "c-1", "Director", active=False)]},
    )
    async with sm() as session, firm_context(firm_id):
        await sync_xpm_clients_to_kg(fake_inactive, session)

    async with sm() as session, firm_context(firm_id):
        active = (
            await session.execute(
                select(EntityRelationship)
                .where(EntityRelationship.firm_id == firm_id)
                .where(EntityRelationship.is_active.is_(True))
            )
        ).scalars().all()
        all_edges = (
            await session.execute(
                select(EntityRelationship)
                .where(EntityRelationship.firm_id == firm_id)
            )
        ).scalars().all()
        # Active count drops to zero; the row still exists but
        # is_active = FALSE.
        assert len(active) == 0
        assert len(all_edges) == 1
        assert all_edges[0].is_active is False


async def test_unknown_entity_type_falls_through_normalised(xpm_sync_env) -> None:
    sm = xpm_sync_env["sm"]
    firm_id = await _seed_firm(sm)
    xpm_sync_env["created"].append(firm_id)

    # XPM admins can configure custom types — sync should not crash.
    fake = FakeXPMClient(
        firm_id,
        [_client("c-1", "Special Vehicle", entity_type="Bare Trust")],
        {},
    )
    async with sm() as session, firm_context(firm_id):
        await sync_xpm_clients_to_kg(fake, session)

    async with sm() as session, firm_context(firm_id):
        ent = (
            await session.execute(
                select(Entity).where(Entity.xpm_client_id == "c-1")
            )
        ).scalar_one()
        assert ent.entity_type == "bare_trust"


async def test_unknown_relationship_type_falls_through_normalised(
    xpm_sync_env,
) -> None:
    sm = xpm_sync_env["sm"]
    firm_id = await _seed_firm(sm)
    xpm_sync_env["created"].append(firm_id)

    clients = [
        _client("c-1", "Acme", "Company"),
        _client("c-2", "Alice", "Individual"),
    ]
    edges = {
        "c-2": [_edge("e-1", "c-2", "c-1", "Authorised Signatory")],
    }
    fake = FakeXPMClient(firm_id, clients, edges)

    async with sm() as session, firm_context(firm_id):
        await sync_xpm_clients_to_kg(fake, session)

    async with sm() as session, firm_context(firm_id):
        edge = (
            await session.execute(
                select(EntityRelationship)
                .where(EntityRelationship.firm_id == firm_id)
            )
        ).scalar_one()
        assert edge.relationship_type == "authorised_signatory"


async def test_self_loop_edges_recorded_as_errors(xpm_sync_env) -> None:
    sm = xpm_sync_env["sm"]
    firm_id = await _seed_firm(sm)
    xpm_sync_env["created"].append(firm_id)

    # XPM data exporting a relationship from c-1 to c-1 — the DB
    # CHECK rejects self-loops; the sync must record this as a soft
    # error rather than crash.
    clients = [_client("c-1", "Acme", "Company")]
    edges = {"c-1": [_edge("e-1", "c-1", "c-1", "Director")]}
    fake = FakeXPMClient(firm_id, clients, edges)

    async with sm() as session, firm_context(firm_id):
        stats = await sync_xpm_clients_to_kg(fake, session)

    assert stats.entities_created == 1
    assert stats.edges_upserted == 0
    assert len(stats.errors) == 1
    assert "self-loop" in stats.errors[0]


async def test_edge_to_unknown_entity_recorded_as_error(xpm_sync_env) -> None:
    """An edge whose to_client_id wasn't in list_clients shouldn't crash."""
    sm = xpm_sync_env["sm"]
    firm_id = await _seed_firm(sm)
    xpm_sync_env["created"].append(firm_id)

    clients = [_client("c-1", "Acme", "Company")]
    edges = {"c-1": [_edge("e-1", "c-1", "c-missing", "Director")]}
    fake = FakeXPMClient(firm_id, clients, edges)

    async with sm() as session, firm_context(firm_id):
        stats = await sync_xpm_clients_to_kg(fake, session)

    assert stats.edges_upserted == 0
    assert any("unknown to-entity" in e for e in stats.errors)


async def test_empty_client_list_returns_zero_stats(xpm_sync_env) -> None:
    sm = xpm_sync_env["sm"]
    firm_id = await _seed_firm(sm)
    xpm_sync_env["created"].append(firm_id)

    fake = FakeXPMClient(firm_id, clients=[], relationships={})
    async with sm() as session, firm_context(firm_id):
        stats = await sync_xpm_clients_to_kg(fake, session)
    assert stats.clients_seen == 0
    assert stats.entities_created == 0
    assert stats.edges_upserted == 0
