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
                "payload": item.payload,
            }
            for item in items
        ]
    }


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
