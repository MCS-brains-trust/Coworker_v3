"""Approval-category builtin tools.

One tool today: ``approval_mark_delivery_failed`` — the side-
effect a ``delivery_status_handler`` run takes when it correlates
an incoming NDR back to the row that produced the failed send.

The tool deliberately does NOT take an approval_item_id: the
correlation is via the proposed Message-ID Graph stamped on the
draft when the dispatcher created it (persisted to
``approval_items.executed_internet_message_id``). Asking the
plugin to look up the row first would be a second tool call for
no gain; the handler does the lookup atomically with the update.

Uncorrelated NDRs (no row matched the supplied Message-ID — OWA
likely regenerated the Message-ID at send time) return
``correlated=False`` with the Message-ID echoed. The plugin
includes that in the run's final text so the trace surfaces the
miss; the WARN log inside the handler is the principal-side
signal.
"""
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from coworker.approval.delivery import mark_delivery_failed
from coworker.orchestrator.context import AgentContext
from coworker.orchestrator.tools import (
    ToolDefinition,
    ToolRegistry,
)


class ApprovalMarkDeliveryFailedInput(BaseModel):
    original_internet_message_id: str = Field(
        description=(
            "The RFC 5322 Message-ID of the message that failed to "
            "deliver. Parse this out of the NDR's In-Reply-To "
            "header (preferred — single value) or the first "
            "<...>-bracketed token of References. Include angle "
            "brackets if present in the header; the comparison is "
            "exact against the value the dispatcher persisted."
        )
    )
    detail: str = Field(
        description=(
            "Short failure summary — typically the NDR's "
            "Diagnostic-Code header value (e.g. '550 5.1.1 user "
            "unknown'), or a one-line description if the NDR did "
            "not include one. Truncated to 500 chars by the "
            "handler."
        ),
        max_length=1000,
    )


async def _approval_mark_delivery_failed_handler(
    inp: ApprovalMarkDeliveryFailedInput, ctx: AgentContext,
) -> dict[str, Any]:
    """Correlate an NDR to its approval_item and flip
    ``delivery_status='failed'``.

    Uncorrelated NDRs are logged at WARN with the supplied
    Message-ID (the carry-forward documented in §9 of the
    discovery doc — OWA can regenerate the Message-ID at send).
    """
    outcome = await mark_delivery_failed(
        ctx.session,
        internet_message_id=inp.original_internet_message_id,
        detail=inp.detail,
    )
    if not outcome.correlated:
        logger.warning(
            "ndr uncorrelated firm_id={} internet_message_id={!r} "
            "detail={!r}",
            ctx.firm.id, inp.original_internet_message_id,
            inp.detail[:120],
        )
        return {
            "correlated": False,
            "original_internet_message_id": inp.original_internet_message_id,
        }
    return {
        "correlated": True,
        "approval_item_id": str(outcome.approval_item_id),
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="approval_mark_delivery_failed",
            description=(
                "Record that an outbound message recorded in the "
                "approval queue failed to deliver. Use this ONLY "
                "when the inbound message is a Non-Delivery Report "
                "(NDR) — detect by Content-Type containing "
                "'multipart/report' and 'report-type=delivery-"
                "status'. The original_internet_message_id must "
                "come from the NDR's In-Reply-To or References "
                "header — that's the Message-ID of the message "
                "that failed, which the dispatcher stamped onto "
                "the approval_items row at send time."
            ),
            category="approval",
            input_model=ApprovalMarkDeliveryFailedInput,
            handler=_approval_mark_delivery_failed_handler,
            cost_estimate_cents=0,
            side_effect=True,
        )
    )
