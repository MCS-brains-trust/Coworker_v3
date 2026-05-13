"""Resolve a SharePoint folder name to a KG entity.

SharePoint folders for a typical firm follow conventions like
``Smith Family Trust`` or ``Acme Pty Ltd``. The Phase 4E indexer
walks every folder under the firm's Clients root and wants to
attach each indexed document to the right ``Entity``. This module
does the resolution via pg_trgm similarity (the GIN trigram index
created in the Phase 4A migration as ``ix_entities_name_trgm``).

A folder maps to at most one entity. The resolver returns the top
match if similarity clears a configurable threshold, otherwise
``None`` — the indexer treats unresolved folders as "stage and
queue for approval" so a fuzzy match never silently associates a
document with the wrong client.
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_DEFAULT_THRESHOLD = 0.4
_DEFAULT_TOP_K = 5
_MIN_THRESHOLD = 0.0
_MAX_THRESHOLD = 1.0


@dataclass(frozen=True)
class ResolverMatch:
    """A folder→entity match with its trigram similarity score.

    ``similarity`` is bounded [0.0, 1.0]; the index uses pg_trgm's
    canonical 3-gram intersection ratio. 1.0 means an exact match.
    Values above 0.4 typically reflect a near-match (case / minor
    typo differences); below 0.4 is usually a coincidence and the
    resolver returns None to leave it for human review.
    """

    entity_id: uuid.UUID
    entity_name: str
    similarity: float


@dataclass(frozen=True)
class ResolverCandidates:
    """Top-K candidate matches for an ambiguous folder.

    Returned when the indexer wants to surface a "did you mean..."
    list to a human reviewer rather than auto-attach. Each candidate
    is sorted by descending similarity.
    """

    candidates: list[ResolverMatch]


async def resolve_folder_to_entity(
    session: AsyncSession,
    *,
    folder_name: str,
    threshold: float = _DEFAULT_THRESHOLD,
    top_k: int = _DEFAULT_TOP_K,
) -> ResolverMatch | None:
    """Return the best entity match for a SharePoint folder name.

    Args:
        session: AsyncSession already inside ``firm_context(firm_id)``.
            RLS scopes the trigram lookup to the firm; we don't need
            to filter on ``firm_id`` in the WHERE clause.
        folder_name: the raw folder name, e.g. ``"Smith Family Trust"``.
            Empty / whitespace returns None.
        threshold: minimum pg_trgm similarity (0.0-1.0) for a match
            to qualify as the resolver's return. The default 0.4
            tolerates case + minor typo variation while rejecting
            coincidental overlaps.
        top_k: how many rows to consider. Most resolutions have a
            single dominant match; top_k = 5 lets the surrounding
            code surface alternatives when the second-best is close
            (handled by ``resolve_folder_candidates`` below).

    Returns:
        ``ResolverMatch`` for the top entity if its similarity
        clears the threshold; otherwise ``None``.

    Raises:
        ValueError: ``threshold`` outside [0, 1] or ``top_k`` < 1.
    """
    _validate_threshold(threshold)
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    rows = await _query_candidates(
        session, folder_name=folder_name, top_k=top_k
    )
    if not rows:
        return None
    top = rows[0]
    if top.similarity < threshold:
        return None
    return top


async def resolve_folder_candidates(
    session: AsyncSession,
    *,
    folder_name: str,
    threshold: float = _DEFAULT_THRESHOLD,
    top_k: int = _DEFAULT_TOP_K,
) -> ResolverCandidates:
    """Return all candidate matches above ``threshold``.

    Use this when the caller wants to disambiguate manually (the
    Phase 13 onboarding wizard for the SharePoint folder-mapping
    step, or a Phase 9 approval item for a fuzzy auto-attach). The
    list is sorted by descending similarity and may be empty.
    """
    _validate_threshold(threshold)
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    rows = await _query_candidates(
        session, folder_name=folder_name, top_k=top_k
    )
    return ResolverCandidates(
        candidates=[r for r in rows if r.similarity >= threshold]
    )


async def _query_candidates(
    session: AsyncSession,
    *,
    folder_name: str,
    top_k: int,
) -> list[ResolverMatch]:
    """Shared pg_trgm query for both resolver entry points.

    The ``name % :q`` operator hits the trigram GIN index; the
    ORDER BY uses ``similarity()`` for the precise score. Empty
    input bypasses the round-trip — pg_trgm would happily return
    zero rows but the early return is cheaper.
    """
    if not folder_name or not folder_name.strip():
        return []

    sql = text(
        """
        SELECT id, name, similarity(name, :q) AS sim
        FROM entities
        WHERE name % :q
        ORDER BY sim DESC
        LIMIT :k
        """
    )
    # text() uses named-style binding (:q, :k), so the % operator
    # passes through verbatim without escaping. (asyncpg's $1/$2
    # numbering is generated by the driver from named parameters.)
    result = await session.execute(
        sql, {"q": folder_name, "k": top_k}
    )
    return [
        ResolverMatch(
            entity_id=row.id,
            entity_name=row.name,
            similarity=float(row.sim),
        )
        for row in result.all()
    ]


def _validate_threshold(threshold: float) -> None:
    if not (_MIN_THRESHOLD <= threshold <= _MAX_THRESHOLD):
        raise ValueError(
            f"threshold must be in [{_MIN_THRESHOLD}, {_MAX_THRESHOLD}]; "
            f"got {threshold}"
        )
