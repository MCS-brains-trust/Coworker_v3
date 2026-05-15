"""KG-category builtin tools: entity lookup and relationship traversal.

Two tools, both pure-DB (no external HTTP):

- ``kg_entity_lookup`` — fuzzy-find an entity by name via pg_trgm.
- ``kg_get_relationships`` — return active edges for an entity.

Both operate under the caller's ``firm_context``; RLS scopes
results to the active firm without any extra WHERE clause in the
handler.
"""
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from coworker.db.models import Entity, EntityRelationship
from coworker.knowledge_graph.sharepoint_resolver import (
    resolve_folder_candidates,
)
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import (
    ToolDefinition,
    ToolError,
    ToolRegistry,
)
from coworker.security.sanitise import sanitise_and_wrap


class KGEntityLookupInput(BaseModel):
    name: str = Field(
        description=(
            "The name to look up. Case-insensitive; minor typos "
            "tolerated via trigram similarity."
        )
    )
    threshold: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum pg_trgm similarity (0.0-1.0) for a row to be "
            "returned. Default 0.4 tolerates case and minor typo "
            "variation; raise toward 1.0 for strict matching."
        ),
    )
    top_k: int = Field(
        default=5, ge=1, le=20,
        description="Maximum candidates to return.",
    )


async def _kg_entity_lookup_handler(
    inp: KGEntityLookupInput, ctx: AgentContext
) -> dict[str, Any]:
    """Lookup entities by name; wrap each candidate's name.

    Sanitised: ``name`` (entity names that originated in an
    inbound email's extraction pass count as user-supplied).
    Untouched: ``entity_id`` (UUID), ``similarity`` (float).
    """
    result = await resolve_folder_candidates(
        ctx.session,
        folder_name=inp.name,
        threshold=inp.threshold,
        top_k=inp.top_k,
    )
    candidates: list[dict[str, Any]] = []
    for c in result.candidates:
        wrapped_name, _ = sanitise_and_wrap(c.entity_name, max_length=200)
        candidates.append({
            "entity_id": str(c.entity_id),
            "name": wrapped_name,
            "similarity": c.similarity,
        })
    return {"candidates": candidates}


class KGGetRelationshipsInput(BaseModel):
    entity_id: str = Field(
        description="UUID of the entity to look up relationships for."
    )
    direction: Literal["out", "in", "both"] = Field(
        default="both",
        description=(
            "'out' returns edges where the entity is the from-side; "
            "'in' returns where it's the to-side; 'both' merges them."
        ),
    )
    active_only: bool = Field(
        default=True,
        description=(
            "When true (default) skip edges with is_active=false. "
            "Set false to include historical edges (e.g. former "
            "directors)."
        ),
    )


async def _kg_get_relationships_handler(
    inp: KGGetRelationshipsInput, ctx: AgentContext
) -> dict[str, Any]:
    try:
        entity_uuid = uuid.UUID(inp.entity_id)
    except ValueError as exc:
        raise ToolError(
            f"entity_id is not a valid UUID: {inp.entity_id!r}"
        ) from exc

    # Confirm the entity exists (and is visible under RLS — protects
    # against the agent asking for a cross-firm UUID).
    existing = (
        await ctx.session.execute(
            select(Entity.id).where(Entity.id == entity_uuid)
        )
    ).scalar_one_or_none()
    if existing is None:
        raise ToolError(
            f"entity {inp.entity_id} not found"
        )

    stmt = select(EntityRelationship)
    if inp.direction == "out":
        stmt = stmt.where(EntityRelationship.from_entity_id == entity_uuid)
    elif inp.direction == "in":
        stmt = stmt.where(EntityRelationship.to_entity_id == entity_uuid)
    else:
        stmt = stmt.where(
            or_(
                EntityRelationship.from_entity_id == entity_uuid,
                EntityRelationship.to_entity_id == entity_uuid,
            )
        )
    if inp.active_only:
        stmt = stmt.where(EntityRelationship.is_active.is_(True))

    edges = (await ctx.session.execute(stmt)).scalars().all()
    return {
        "edges": [
            {
                "id": str(e.id),
                "from_entity_id": str(e.from_entity_id),
                "to_entity_id": str(e.to_entity_id),
                "relationship_type": e.relationship_type,
                "confidence": e.confidence,
                "is_active": e.is_active,
            }
            for e in edges
        ]
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="kg_entity_lookup",
            description=(
                "Fuzzy-find KG entities by name. Returns "
                "(entity_id, name, similarity) candidates sorted "
                "by descending similarity. Use to resolve a name "
                "in an email or document to a canonical entity "
                "id before traversing relationships or attaching "
                "documents."
            ),
            category="kg",
            input_model=KGEntityLookupInput,
            handler=_kg_entity_lookup_handler,
            cost_estimate_cents=0,
        )
    )
    registry.register(
        ToolDefinition(
            name="kg_get_relationships",
            description=(
                "Return relationship edges for a given entity. "
                "Choose direction='out' (edges starting at the "
                "entity), 'in' (edges pointing at it), or 'both'. "
                "Use to walk the KG ('who are the directors of "
                "Acme Pty Ltd?', 'which trusts is Alice a "
                "beneficiary of?')."
            ),
            category="kg",
            input_model=KGGetRelationshipsInput,
            handler=_kg_get_relationships_handler,
            cost_estimate_cents=0,
        )
    )
