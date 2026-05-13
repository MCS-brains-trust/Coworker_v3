"""Tool registry — the single source of truth for what an agent can call.

A ``ToolDefinition`` packages everything Claude's native tool-use
needs (name, description, JSON-schema input) with everything the
orchestrator needs (handler, category, cost estimate, side-effect
flag). The registry is constructed at startup, populated by every
module that contributes tools (memory, graph, xpm, …), and sliced
per-plugin by ``filter_by_categories`` before being passed to the
engine.

Categories
----------

Locked at the build-plan level. Used by plugins to scope what
they can call: ``smart_responder`` has memory + kg + email +
calendar; ``engagement_letter`` has fusesign + memory + kg; etc.
Adding a new category requires an architecture-doc edit, not a
code change. Unknown categories raise at registration time so
typos surface at startup not run time.

Handler shape
-------------

``ToolHandler`` takes the parsed Pydantic input + the
``AgentContext`` and returns anything JSON-serialisable. The engine
JSON-encodes the return value into the ``tool_result`` block Claude
sees. A handler that raises a ``ToolError`` ends up as a Claude-
visible error (``is_error=true`` on the tool_result); a handler
that raises any other exception is captured by the engine and
recorded as a trace step but does NOT abort the loop.
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from coworker.orchestrator.context import AgentContext

ToolCategory = Literal[
    "memory",
    "kg",
    "xpm",
    "email",
    "calendar",
    "fusesign",
    "teams",
    "vision",
    "approval",
    "reasoning",
]

_KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        "memory",
        "kg",
        "xpm",
        "email",
        "calendar",
        "fusesign",
        "teams",
        "vision",
        "approval",
        "reasoning",
    }
)


# Async handler signature. The first argument is the parsed input
# model (a subclass of BaseModel); the second is the per-run context.
# ``Any`` on the first parameter avoids the typing-system pain of
# function-argument contravariance: a handler typed as
# ``(MemoryQueryInput, AgentContext) -> Awaitable[dict]`` should be
# acceptable here, but ``Callable[[BaseModel, ...], ...]`` rejects it
# because BaseModel is a supertype of MemoryQueryInput. The engine
# validates the model BEFORE invoking, so runtime types are correct
# even with the looser static type.
ToolHandler = Callable[[Any, AgentContext], Awaitable[Any]]


class ToolError(Exception):
    """Raised by a tool handler to surface a Claude-visible error.

    The engine catches this, marks the corresponding tool_result
    block as ``is_error=true``, and continues the loop. The model
    sees the error message and can adjust its plan.

    Use for "expected failures the model should reason about" —
    "client not found", "permission denied", "no data in range".
    Don't use for genuine connector failures (let those bubble up
    via the ``ConnectorError`` family); the engine maps those to
    Claude-visible error tool_results too but distinguishes them
    in the trace.
    """


@dataclass(frozen=True)
class ToolDefinition:
    """One tool the agent can invoke.

    Attributes:
        name: Claude-facing name (``[a-zA-Z0-9_]{1,64}`` per
            Anthropic's tool-use rules). Convention is
            ``<category>_<verb>``: ``memory_query``,
            ``email_create_draft``, ``kg_entity_lookup``.
        description: free-text, included verbatim in the tool's
            Anthropic definition. Treat as a prompt — the model
            reads this when deciding to call the tool. Be specific
            about WHEN to use it.
        category: one of ``ToolCategory``. Plugins scope their
            tool set by including / excluding categories.
        input_model: a Pydantic v2 BaseModel whose JSON schema is
            handed to Claude. Each field's description becomes
            argument documentation for the model.
        handler: async callable that does the work. See
            ``ToolHandler``.
        cost_estimate_cents: rough cents-per-invocation cost
            (Claude tokens + downstream API costs). The engine
            uses this for budget guard pre-checks; a handler may
            return its actual cost via ``ctx`` for fine-grained
            tracking.
        side_effect: True if the tool mutates external state
            (creates a draft, sends a Teams message, writes an
            XPM note). Plugins in dry-run mode (Phase 6) will
            filter side-effect tools out before passing the
            registry to the engine.
    """

    name: str
    description: str
    category: ToolCategory
    input_model: type[BaseModel]
    handler: ToolHandler
    cost_estimate_cents: int = 0
    side_effect: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(
                f"tool name must be alphanumeric+underscore; got {self.name!r}"
            )
        if self.category not in _KNOWN_CATEGORIES:
            raise ValueError(
                f"unknown tool category {self.category!r}; "
                f"expected one of {sorted(_KNOWN_CATEGORIES)}"
            )
        if self.cost_estimate_cents < 0:
            raise ValueError("cost_estimate_cents must be >= 0")


class ToolRegistry:
    """Process-global tool catalogue.

    Mutation surface is small (``register``, ``unregister``); the
    common flow is to construct once at startup, then call
    ``filter_by_categories`` to derive per-plugin slices.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Add a tool. Raises if the name is already taken."""
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def filter_by_categories(
        self,
        categories: set[ToolCategory],
        *,
        exclude_side_effects: bool = False,
    ) -> "ToolRegistry":
        """Return a fresh registry containing only matching tools.

        Args:
            categories: the categories to keep. Empty set returns
                an empty registry.
            exclude_side_effects: when True, drops any tool with
                ``side_effect=True``. Phase 6's dry-run plugin
                installs use this so an agent run can't create
                drafts even by mistake.
        """
        subset = ToolRegistry()
        for tool in self._tools.values():
            if tool.category not in categories:
                continue
            if exclude_side_effects and tool.side_effect:
                continue
            subset._tools[tool.name] = tool
        return subset

    def to_anthropic_definitions(self) -> list[dict[str, Any]]:
        """Render the registry as Anthropic's tool-use schema list.

        Each tool becomes::

            {
              "name": "<name>",
              "description": "<description>",
              "input_schema": <pydantic JSON schema>,
            }

        Pydantic v2's ``model_json_schema(mode="serialization")``
        produces a draft-2020-12 JSON schema. Anthropic accepts it
        directly. ``$defs`` references for nested models are kept
        intact — Claude handles them.
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": _anthropic_input_schema(tool.input_model),
            }
            for tool in self._tools.values()
        ]


def _anthropic_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Coerce a Pydantic JSON schema into Anthropic's tool input shape.

    Anthropic expects an object schema with ``type``, ``properties``,
    and (optional) ``required``. Pydantic emits exactly that for a
    BaseModel. We strip ``title`` so the surface stays clean (the
    model doesn't need Pydantic's auto-titles cluttering the prompt).
    """
    schema = model.model_json_schema(mode="serialization")
    # Pydantic always emits ``"type": "object"`` for BaseModel;
    # belt-and-braces in case a tool uses a RootModel later.
    schema.setdefault("type", "object")
    schema.pop("title", None)
    # Strip per-property titles which Pydantic auto-generates from
    # the field name — they add noise without improving the model's
    # understanding.
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for prop in properties.values():
            if isinstance(prop, dict):
                prop.pop("title", None)
    return schema
