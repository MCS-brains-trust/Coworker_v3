"""SmartResponderPlugin — first builtin plugin.

Triggered by ``email_received`` events from Phase 11's Graph
webhook receiver. The agent loop reads the triggering email,
queries firm memory for context (similar past interactions,
lessons, relevant documents), looks up the sender in the KG,
and produces a draft reply via the ``email_create_draft`` tool.

In shadow mode (firm.shadow_mode=True), the create_draft tool
is gated by ``guard_writable`` at the connector layer and no
draft is actually created — the would-be content lands in the
audit log and Phase 9's approval queue instead. Outside shadow
mode, the draft is created in the user's Drafts folder; Phase 9
gates whether the draft gets queued for approval before sending.

Tool category set (read-only + write):

- memory: ground the response in past interactions / lessons /
  documents.
- kg: identify the sender's entity, walk relationships.
- email: read the triggering message + create the draft reply.
- reasoning: today's date + firm info.

Cost budget defaults to $0.20 per run — enough for ~6-8 Sonnet
iterations including a memory_query + several tool calls. Firms
that want a deeper draft can raise via plugin_installations.config
(``cost_budget_cents`` override) once that knob is wired in
Phase 6.

Config (``SmartResponderConfig``):

- ``confidence_threshold``: minimum self-consistency confidence
  (Phase 9) for auto-execution of the draft. Below this the
  draft routes to approval. Defaults to 0.85; the Phase 9
  approval queue is what reads this.
- ``style_hint``: free-text override for the system prompt's
  voice instructions. Useful when a firm prefers a particular
  tone ("formal", "warm", "concise").
"""

from pydantic import BaseModel, Field

from coworker.plugins.base import OrchestratorPlugin, PluginRun

_DEFAULT_BUDGET_CENTS = 20  # $0.20 per run


class SmartResponderConfig(BaseModel):
    """Per-firm configuration for the smart_responder plugin."""

    confidence_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Self-consistency confidence required for the draft to "
            "auto-execute (sub-threshold drafts route to the Phase 9 "
            "approval queue)."
        ),
    )
    style_hint: str | None = Field(
        default=None,
        description=(
            "Optional voice override appended to the system prompt. "
            "Use to bias the model's tone — 'formal', 'warm', "
            "'concise' all work."
        ),
    )


class SmartResponderPlugin(OrchestratorPlugin):
    """Inbound-email reply drafter.

    The plugin's goal text walks the agent through the expected
    sequence: identify the sender, surface relevant memory and
    KG context, draft a reply. The model decides which tools to
    call in which order; the goal is a starting point, not a
    script.
    """

    name = "smart_responder"
    display_name = "Smart Responder"
    description = (
        "Drafts a reply to every inbound email using firm memory + "
        "knowledge graph context. Sub-threshold drafts route to "
        "human approval; above-threshold drafts ship after a brief "
        "review window."
    )
    version = "0.1.0"
    triggers = frozenset({"email_received"})
    enabled_tool_categories = frozenset({"memory", "kg", "email", "reasoning"})
    config_schema = SmartResponderConfig
    cost_budget_cents = _DEFAULT_BUDGET_CENTS
    allow_side_effects = True  # creates email drafts

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        event = run.event_data
        message_id = event.get("message_id", "<unknown>")
        from_address = event.get("from", "<unknown>")
        subject = event.get("subject", "<unknown>")
        snippet = (event.get("body_preview") or "").strip()
        snippet_block = f"\n\nPreview:\n{snippet}" if snippet else ""

        return (
            "An email has arrived in the monitored mailbox and needs a "
            "drafted reply.\n\n"
            f"- Message ID: {message_id}\n"
            f"- From: {from_address}\n"
            f"- Subject: {subject}"
            f"{snippet_block}\n\n"
            "Steps to follow:\n\n"
            "1. If the preview is enough to draft a reply, you may "
            "skip fetching the full message. Otherwise use "
            "email_get_message to read it.\n"
            "2. Use kg_entity_lookup on the sender's name (extracted "
            "from the from address) to find the matching client "
            "entity. If found, walk kg_get_relationships to "
            "understand who else is connected.\n"
            "3. Use memory_query with two to three targeted queries "
            "(the subject, key entity names, distinctive phrases "
            "from the preview) to surface past interactions, "
            "lessons, and relevant documents.\n"
            "4. Draft the reply using email_create_draft. The draft "
            "should reference the sender by name, address the "
            "specific question or request, and use the firm's "
            "voice. When unsure, prefer to ask a clarifying "
            "question rather than fabricate a definitive answer.\n\n"
            "End the run after the draft is created; no further "
            "actions are needed (Phase 9's approval queue will "
            "route the draft for human review)."
        )

    @classmethod
    def system_prompt(cls, run: PluginRun) -> str | None:
        config = run.config
        style_hint = config.get("style_hint")
        threshold = config.get("confidence_threshold", 0.85)

        base = (
            "You are an accounting practice assistant drafting "
            "replies to client emails on behalf of the firm.\n\n"
            "Voice rules:\n"
            "- Use the firm's standard greeting and signoff (look "
            "these up in memory if uncertain).\n"
            "- Be specific: reference the client's actual question, "
            "name, and any prior interactions you found.\n"
            "- Never invent regulatory or tax facts. If a question "
            "needs a definitive technical answer you don't have, "
            "ask a clarifying question or note that the partner "
            "will follow up.\n"
            "- Don't include disclaimers the firm doesn't already "
            "use; check memory for the firm's standard boilerplate "
            "before adding any.\n\n"
            f"Self-consistency target: {threshold:.2f}. The draft "
            "will be evaluated against this threshold for "
            "auto-execution; below it, a human reviewer sees the "
            "draft before it goes out, so it's acceptable for "
            "uncertain cases to surface the uncertainty rather "
            "than hide it."
        )
        if style_hint:
            base += f"\n\nFirm-specific voice override: {style_hint}"
        return base


def _expected_event_keys() -> tuple[str, ...]:
    """The keys ``event_data`` is expected to carry for this plugin.

    Used by tests and by the Phase 11 webhook receiver to validate
    that an email_received event is well-formed before enqueuing.
    """
    return ("message_id", "from", "subject", "body_preview")
