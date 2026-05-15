"""Memory-category builtin tool: ``memory_query``.

Wraps ``HybridRetriever`` from Phase 4C with a tool-shaped interface
the agent can call. Returns the top hits across
``client_interactions``, ``lessons``, and ``documents`` with the
fields the model needs to reason about relevance (kind, score,
payload snippet).
"""
from typing import Any, Literal

from pydantic import BaseModel, Field

from coworker.memory.retriever import HybridRetriever
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import (
    ToolDefinition,
    ToolError,
    ToolRegistry,
)
from coworker.security.sanitise import sanitise_and_wrap

# Payload string fields that may carry externally-sourced text
# (interaction bodies, lesson text from past emails, document
# extracts). These get sanitised + wrapped in <user_data>.
# Other payload fields (ids, dates, enums, priorities, scores)
# pass through untouched.
_PAYLOAD_TEXT_FIELDS: tuple[str, ...] = (
    "subject", "summary", "body", "text", "title", "content",
)


class MemoryQueryInput(BaseModel):
    query: str = Field(
        description="The natural-language search string."
    )
    kinds: list[Literal["client_interactions", "lessons", "documents"]] | None = Field(
        default=None,
        description=(
            "Which memory tables to search. Omit to search all "
            "three. Use a narrower set when the question is "
            "obviously confined (e.g. ['lessons'] for "
            "'what should I check before lodging a BAS?')."
        ),
    )
    k: int = Field(
        default=8,
        ge=1,
        le=50,
        description="Maximum hits to return.",
    )


async def _memory_query_handler(
    inp: MemoryQueryInput, ctx: AgentContext
) -> dict[str, Any]:
    """Search memory; sanitise + wrap text payload fields.

    Sanitised payload fields (per ``_PAYLOAD_TEXT_FIELDS``):
    subject, summary, body, text, title, content — any of which
    may have originated in an inbound email and round-trip back
    to Claude via tool_result.

    Untouched: kind, id, score (floats / strings), priority, dates,
    and any other structured payload fields. They're emitted
    verbatim.
    """
    if ctx.embedder is None:
        raise ToolError(
            "memory_query is unavailable in this context "
            "(no embedder configured)"
        )
    retriever = HybridRetriever(
        ctx.session, ctx.embedder, firm_id=ctx.firm.id
    )
    items = await retriever.retrieve(
        inp.query, kinds=inp.kinds, k=inp.k
    )
    return {
        "hits": [
            {
                "kind": item.kind,
                "id": str(item.row_id),
                "score": item.score,
                "payload": _sanitise_payload(item.payload),
            }
            for item in items
        ]
    }


def _sanitise_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap user-content fields in ``<user_data>`` tags.

    Pass-through for keys that aren't in ``_PAYLOAD_TEXT_FIELDS``
    so identifier / metadata fields stay unchanged.
    """
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in _PAYLOAD_TEXT_FIELDS and isinstance(v, str):
            wrapped, _ = sanitise_and_wrap(v, max_length=4000)
            out[k] = wrapped
        else:
            out[k] = v
    return out


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="memory_query",
            description=(
                "Search the firm's memory layer (past client "
                "interactions, learned lessons, and indexed "
                "documents) for items relevant to a query. Returns "
                "top hits ranked by a hybrid of BM25 and vector "
                "similarity, with lesson priority boosting. Use "
                "early in any reasoning task to surface "
                "firm-specific context the model wouldn't know."
            ),
            category="memory",
            input_model=MemoryQueryInput,
            handler=_memory_query_handler,
            cost_estimate_cents=1,
        )
    )
