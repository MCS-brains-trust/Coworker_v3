"""XPM → knowledge graph sync.

Pulls every Client from Xero Practice Manager (with optional
``modifiedsince`` for incremental delta), upserts the row in
``entities`` keyed by ``xpm_client_id``, then walks
``list_relationships`` for each client and upserts directed edges
into ``entity_relationships``.

Called by Phase 6's APScheduler on a nightly schedule. The
function itself is single-process safe; APScheduler uses a Redis
lock to ensure only one worker runs the sync at a time, which is
good enough — the entity find-or-update path tolerates concurrent
duplicates by collapsing them at the next sync.

Provenance shape on every edge::

    {
      "source": "xpm",
      "xpm_relationship_id": "<XPM's id>",
      "synced_at": "<ISO timestamp>",
      "first_seen": "<ISO timestamp>"   # only set on insert
    }

Mappings live near the top so a Xero schema change is a single-file
edit. Entity types or relationship types outside the mapping pass
through lowercased + space-replaced (``"Sole Trader"`` → ``"sole_trader"``);
the consumer can handle "unknown" types by category rather than
crash.
"""
import datetime as _dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.xpm_client import (
    XPMClient,
    XPMClientRecord,
    XPMRelationship,
)
from coworker.db.models import Entity, EntityRelationship

# XPM "Type" → KG entity_type. Lowercase keys (we lowercase the
# incoming value before lookup) so the mapping is case-insensitive.
_ENTITY_TYPE_MAP: dict[str, str] = {
    "individual": "individual",
    "company": "company",
    "trust": "trust",
    "partnership": "partnership",
    "smsf": "smsf",
    "sole trader": "individual",  # legally a person under a trading name
}

# XPM relationship "Type" → KG relationship_type. Same case-insensitive
# lookup. Unknown types pass through normalised (lowercase + underscores)
# so the KG can still ingest them; downstream callers filter by type
# as needed.
_RELATIONSHIP_TYPE_MAP: dict[str, str] = {
    "director": "director_of",
    "trustee": "trustee_of",
    "beneficiary": "beneficiary_of",
    "appointor": "appointor_of",
    "shareholder": "shareholder_of",
    "secretary": "secretary_of",
    "spouse": "spouse_of",
    "parent": "parent_of",
    "child": "child_of",
    "member": "member_of",
}


@dataclass
class SyncStats:
    """Result of one ``sync_xpm_clients_to_kg`` run.

    Both counts on the entity side (created + updated) sum to the
    XPM client count; the relationship side counts ``edges_upserted``
    (inserts + updates collapsed because the partial unique index
    UPSERT does both in one statement). ``errors`` is a list of
    short strings, one per skipped row, so a bad source row doesn't
    abort the whole sync.
    """

    clients_seen: int = 0
    entities_created: int = 0
    entities_updated: int = 0
    edges_upserted: int = 0
    errors: list[str] = field(default_factory=list)


async def sync_xpm_clients_to_kg(
    xpm_client: XPMClient,
    session: AsyncSession,
    *,
    updated_since: _dt.datetime | None = None,
) -> SyncStats:
    """Pull XPM clients (+ their relationships) and reflect them in the KG.

    Args:
        xpm_client: a ready ``XPMClient`` (already firm-scoped). The
            access token is auto-refreshed via the client; this
            function does not touch credentials directly.
        session: AsyncSession inside ``firm_context(xpm_client.firm.id)``.
            Caller's responsibility.
        updated_since: tz-aware datetime for incremental sync.
            ``None`` performs a full sync.

    Returns:
        ``SyncStats`` summarising what changed. Callers (the
        scheduler, ops dashboards) record this for trend tracking.
    """
    stats = SyncStats()

    clients = await xpm_client.list_clients(updated_since=updated_since)
    stats.clients_seen = len(clients)
    if not clients:
        return stats

    firm_id = xpm_client.firm.id
    now = _dt.datetime.now(_dt.UTC)

    # First pass: upsert all entities. Build a map xpm_client_id ->
    # entity_id so the relationship pass can resolve both endpoints
    # without an extra round-trip.
    xpm_to_entity: dict[str, uuid.UUID] = {}
    for raw in clients:
        try:
            entity_id, was_created = await _upsert_entity_from_xpm(
                session, firm_id=firm_id, record=raw,
            )
        except Exception as exc:
            stats.errors.append(f"entity {raw.id}: {exc}")
            continue
        xpm_to_entity[raw.id] = entity_id
        if was_created:
            stats.entities_created += 1
        else:
            stats.entities_updated += 1

    await session.flush()

    # Second pass: pull relationships per client and UPSERT edges
    # against the (firm_id, from, to, relationship_type) unique
    # active partial index. Endpoints whose entity isn't in
    # xpm_to_entity (because the entity sync above errored) are
    # skipped with an error note rather than silently dropped.
    for raw in clients:
        from_entity_id = xpm_to_entity.get(raw.id)
        if from_entity_id is None:
            continue
        try:
            edges = await xpm_client.list_relationships(raw.id)
        except Exception as exc:
            stats.errors.append(
                f"list_relationships({raw.id}): {exc}"
            )
            continue
        for edge in edges:
            to_entity_id = xpm_to_entity.get(edge.to_client_id)
            if to_entity_id is None:
                stats.errors.append(
                    f"edge {edge.id}: unknown to-entity "
                    f"xpm_client_id={edge.to_client_id}"
                )
                continue
            if from_entity_id == to_entity_id:
                # The DB CHECK rejects self-loops; surface that as
                # a soft error rather than letting the INSERT crash.
                stats.errors.append(
                    f"edge {edge.id}: refusing self-loop on "
                    f"xpm_client_id={raw.id}"
                )
                continue
            try:
                await _upsert_relationship_from_xpm(
                    session,
                    firm_id=firm_id,
                    from_entity_id=from_entity_id,
                    to_entity_id=to_entity_id,
                    record=edge,
                    now=now,
                )
            except Exception as exc:
                stats.errors.append(
                    f"edge {edge.id}: {exc}"
                )
                continue
            stats.edges_upserted += 1

    await session.commit()
    return stats


async def _upsert_entity_from_xpm(
    session: AsyncSession,
    *,
    firm_id: uuid.UUID,
    record: XPMClientRecord,
) -> tuple[uuid.UUID, bool]:
    """Find-or-insert the Entity for an XPM client.

    Returns ``(entity_id, was_created)``. The find step uses the
    non-unique index ``ix_entities_firm_xpm``; under the nightly
    sync the lock prevents concurrent duplicates, but if a
    duplicate ever appears the find picks the first one and the
    next sync can dedupe.
    """
    entity_type = _map_entity_type(record.entity_type)
    existing = await session.execute(
        select(Entity).where(
            Entity.firm_id == firm_id,
            Entity.xpm_client_id == record.id,
        )
    )
    entity = existing.scalar_one_or_none()
    if entity is None:
        entity = Entity(
            id=uuid.uuid4(),
            firm_id=firm_id,
            entity_type=entity_type,
            name=record.name,
            display_name=record.name,
            xpm_client_id=record.id,
            kg_metadata={
                "source": "xpm",
                "first_seen": _dt.datetime.now(_dt.UTC).isoformat(),
            },
        )
        session.add(entity)
        await session.flush()
        return entity.id, True

    entity.entity_type = entity_type
    entity.name = record.name
    if not entity.display_name:
        entity.display_name = record.name
    return entity.id, False


async def _upsert_relationship_from_xpm(
    session: AsyncSession,
    *,
    firm_id: uuid.UUID,
    from_entity_id: uuid.UUID,
    to_entity_id: uuid.UUID,
    record: XPMRelationship,
    now: _dt.datetime,
) -> None:
    """UPSERT an active relationship edge from an XPM record.

    Targets the partial unique index
    ``ix_entity_relationships_unique_active`` so concurrent re-syncs
    converge on one active edge per (firm, from, to, type).
    """
    relationship_type = _map_relationship_type(record.relationship_type)
    provenance = {
        "source": "xpm",
        "xpm_relationship_id": record.id,
        "synced_at": now.isoformat(),
    }
    # is_active mirrors XPM's flag; an XPM-inactive edge becomes a
    # KG-inactive edge (and therefore falls outside the partial
    # unique index, allowing the next active edge to coexist).
    is_active = record.is_active

    if is_active:
        stmt = pg_insert(EntityRelationship).values(
            firm_id=firm_id,
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            relationship_type=relationship_type,
            provenance={**provenance, "first_seen": now.isoformat()},
            confidence=1.0,
            is_active=True,
        )
        stmt = stmt.on_conflict_do_update(
            # PostgreSQL's ON CONFLICT must match the partial unique
            # index's WHERE clause SYNTACTICALLY, not just semantically.
            # The 4A migration created the index with
            # ``WHERE is_active = TRUE`` (raw text); we therefore pass
            # the same literal text here. SQLAlchemy's
            # ``column.is_(True)`` renders as ``IS true`` which
            # Postgres rejects as a non-matching predicate.
            index_elements=[
                "firm_id",
                "from_entity_id",
                "to_entity_id",
                "relationship_type",
            ],
            index_where=text("is_active = TRUE"),
            set_={
                "provenance": stmt.excluded.provenance,
                "confidence": stmt.excluded.confidence,
                "updated_at": now,
            },
        )
        await session.execute(stmt)
        return

    # Inactive in XPM — deactivate the active KG edge of this type
    # between the same pair so the partial unique index permits a
    # later re-add as a new active edge. INSERT-only on a separate
    # inactive row is also acceptable but would leave a trail of
    # rows; updating in place keeps the row count bounded.
    await session.execute(
        update(EntityRelationship)
        .where(
            EntityRelationship.firm_id == firm_id,
            EntityRelationship.from_entity_id == from_entity_id,
            EntityRelationship.to_entity_id == to_entity_id,
            EntityRelationship.relationship_type == relationship_type,
            EntityRelationship.is_active.is_(True),
        )
        .values(is_active=False, updated_at=now, provenance=provenance)
    )


def _map_entity_type(xpm_type: str | None) -> str:
    if not xpm_type:
        return "other"
    key = xpm_type.strip().lower()
    return _ENTITY_TYPE_MAP.get(key, key.replace(" ", "_"))


def _map_relationship_type(xpm_type: str | None) -> str:
    if not xpm_type:
        return "other"
    key = xpm_type.strip().lower()
    if key in _RELATIONSHIP_TYPE_MAP:
        return _RELATIONSHIP_TYPE_MAP[key]
    # Forward-compatible normalisation. ``"Authorised Signatory"`` →
    # ``"authorised_signatory"`` rather than crashing.
    return key.replace(" ", "_")
