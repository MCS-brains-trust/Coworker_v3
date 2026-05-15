"""DeliveryStatusHandlerPlugin — NDR / bounce handler.

Triggered by ``email_received`` events. Most runs are no-ops: the
plugin fetches the triggering message, checks whether it's a
Non-Delivery Report (Content-Type contains ``multipart/report``
with ``report-type=delivery-status``), and ends the run if it
isn't.

When it IS an NDR, the plugin parses the original failed
message's RFC 5322 Message-ID out of the NDR's ``In-Reply-To``
header (preferred) or the first angle-bracketed token of
``References``, then calls ``approval_mark_delivery_failed``.
That tool's handler matches the Message-ID against
``approval_items.executed_internet_message_id`` (which the
dispatcher persists on every send) and flips
``delivery_status='failed'`` on the matching row.

Uncorrelated NDRs (no row matched — typically because OWA
regenerated the Message-ID at send time, a documented carry-
forward) are logged at WARN inside the tool handler. The
approval_items row stays in ``delivery_status='sent'`` and will
eventually flip to ``'delivered'`` falsely at the 4h
confirmation sweep. A future ``/sentItems`` polling step would
close that gap.

Why a plugin, not a webhook branch:

- The webhook receiver MUST respond 202 quickly (Microsoft will
  drop the subscription otherwise). Adding a Graph fetch into
  the receiver to discriminate would violate that.
- Plugins go through Phase 6 dedup (no double-fires) and the
  per-firm worker concurrency budget; the webhook layer doesn't.
- Smart Responder already runs on every ``email_received``; if
  the cost ever becomes a concern, a shared HybridRetriever
  cache for the just-fetched message can amortise the Graph
  call across both plugins.
"""

from coworker.plugins.base import OrchestratorPlugin, PluginRun

_BUDGET_CENTS = 5  # Single get_message + at most one tool call


class DeliveryStatusHandlerPlugin(OrchestratorPlugin):
    """Detect NDRs and correlate them back to the failed send.

    Tool categories: ``email`` (read the triggering message via
    ``email_get_message``) + ``approval`` (call
    ``approval_mark_delivery_failed`` when correlated).
    ``allow_side_effects=True`` is required for the approval
    tool; the email category here is read-only (the run does NOT
    create drafts).
    """

    name = "delivery_status_handler"
    display_name = "Delivery Status Handler"
    description = (
        "Inspects every inbound email for Non-Delivery Report "
        "(NDR) signatures. When detected, correlates the NDR "
        "back to the approval_items row that produced the failed "
        "send and flips its delivery_status to 'failed'. "
        "Non-NDR mail is a no-op (fast classifier-only path)."
    )
    version = "0.1.0"
    triggers = frozenset({"email_received"})
    enabled_tool_categories = frozenset({"email", "approval"})
    cost_budget_cents = _BUDGET_CENTS
    allow_side_effects = True  # approval_mark_delivery_failed

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        # The triggering event carries the Graph message id; the
        # plugin's only job is to look that up and discriminate.
        # The goal is intentionally directive so Claude doesn't
        # wander into unrelated tools.
        message_id = run.event_data.get("message_id", "<unknown>")
        return (
            "An inbound email has arrived. Determine whether it is a "
            "Non-Delivery Report (NDR / bounce) and, if so, mark the "
            "originating send as failed.\n\n"
            f"Triggering message id: {message_id}\n\n"
            "Steps:\n\n"
            "1. Call email_get_message with the message id above to "
            "fetch the full message including internet_message_headers.\n"
            "2. Inspect internet_message_headers for a 'Content-Type' "
            "entry. An NDR's Content-Type value contains both "
            "'multipart/report' AND 'report-type=delivery-status' "
            "(case-insensitive). If neither match, end the run with "
            "the final text \"not an NDR\" and do NOT call any other "
            "tools.\n"
            "3. If the message IS an NDR: locate the original failed "
            "message's Message-ID. Prefer the 'In-Reply-To' header "
            "(single value, exact); fall back to the FIRST "
            "angle-bracketed token of the 'References' header (e.g. "
            "for 'References: <a@x> <b@y>' use '<a@x>'). Preserve "
            "the angle brackets. If neither header is present, end "
            "the run with \"NDR without correlatable Message-ID\" "
            "and do not call other tools.\n"
            "4. Compose a short detail string from the NDR. Prefer "
            "the value of the 'Diagnostic-Code' header (e.g. "
            "'smtp; 550 5.1.1 user unknown'). If absent, use the "
            "subject (typically 'Undeliverable: <original subject>') "
            "trimmed to ~200 chars.\n"
            "5. Call approval_mark_delivery_failed with "
            "original_internet_message_id and detail set. End the "
            "run; do not draft a reply, do not call any other tools."
        )

    @classmethod
    def system_prompt(cls, run: PluginRun) -> str | None:
        return (
            "You are a deterministic NDR classifier. Follow the goal's "
            "steps exactly. Most inbound emails are not NDRs; in that "
            "case the right answer is to stop after the first "
            "email_get_message call and emit 'not an NDR'. Do not "
            "draft replies, do not propose approvals, do not call any "
            "tool other than email_get_message and "
            "approval_mark_delivery_failed."
        )
