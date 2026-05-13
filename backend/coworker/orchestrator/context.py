"""``AgentContext`` — the per-run bundle every tool handler sees.

Construct once at the top of ``OrchestratorEngine.run()`` and pass
it through every tool invocation. Tools deconstruct the fields
they need (session, firm, anthropic, retriever, embedder, …);
adding new dependencies requires updating this class so the
boundary stays explicit.
"""
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.anthropic_client import AnthropicClient
from coworker.db.models import Firm

if TYPE_CHECKING:
    from coworker.graph.context import GraphContext
    from coworker.memory.embeddings import Embedder


@dataclass(frozen=True)
class AgentContext:
    """Everything a tool handler may need to do its work.

    Frozen so a handler can't accidentally mutate ``firm`` or swap
    out the session under another handler. ``metadata`` is an
    escape hatch for plugin-specific extras (Phase 6) — the
    engine never reads it, so plugins can stash anything that
    survives the run.

    Cost-guard fields:

    - ``budget_cents`` is the maximum the run may spend on Claude
      calls. ``None`` means no cap.
    - The engine maintains running totals on the trace row; the
      handler can inspect ``budget_cents`` to decide whether to
      decline an expensive sub-call (e.g. specialist consult).

    Optional collaborators (``embedder``, …) are None when the
    enclosing plugin / route doesn't need them. Tools that need
    them check and surface a clear ToolError rather than crashing.
    """

    firm: Firm
    session: AsyncSession
    anthropic: AnthropicClient
    trace_id: uuid.UUID
    embedder: "Embedder | None" = None
    graph_ctx: "GraphContext | None" = None
    budget_cents: int | None = None
    extended_thinking: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
