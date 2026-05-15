"""Principal-facing approval queue model.

A plugin that produces a side effect (an email draft, a proposed
client_interactions row, an entity update) writes one row here.
The Phase 10 web frontend renders pending rows; the Phase 9-3
in-place edit + Phase 9-4 dispatch confirmation extend the state
machine.

The model is intentionally narrow: ``category`` selects the
shape of ``payload``, which is opaque JSONB. Per-category
schema validation happens at the call site (e.g. an
``EmailDraftPayload`` Pydantic model in the smart_responder
plugin) so this table can grow new categories without a
migration.
"""
import datetime as _dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base


class ApprovalItem(Base):
    """One row per side-effect awaiting principal review.

    ``status`` is a small enum the migration's CHECK constraint
    keeps honest; application-side helpers in
    ``coworker.approval.items`` enforce the legal transitions.

    ``trace_id`` is nullable because a manual-trigger flow (Phase
    9-3) may produce an approval item without a backing agent
    trace. CASCADE is SET NULL so we keep the audit even if the
    trace row is pruned.
    """

    __tablename__ = "approval_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_traces.id", ondelete="SET NULL"),
    )

    plugin_name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
        server_default="pending",
    )

    decided_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    decision_notes: Mapped[str | None] = mapped_column(Text)

    # Most recent in-place edit (Phase 9-3). The status stays
    # ``pending`` across edits; these columns track *who* and *when*
    # only — full versioned history is deferred. NULL until the
    # principal touches the payload.
    last_edited_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_edited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    # Two-person approval (Phase 9-6). High-sensitivity categories
    # (engagement_letter, formal_demand, fusesign_envelope_new_client,
    # memory_purge) need ``required_approvals=2``; everything else
    # defaults to 1.
    required_approvals: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default="1",
    )
    # JSONB array of {user_id, signed_at, notes}. Each successful
    # approve() appends one entry; the helper enforces no-double-sign.
    approval_signatures: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )

    # Plugin's self-rated confidence (Phase 9-7). NULL means the
    # producing plugin chose not to self-rate, in which case the
    # row always routes to human approval. When set, the helper
    # auto-approves at insert time if confidence >= threshold and
    # the category isn't two-person.
    confidence: Mapped[float | None] = mapped_column()

    # Delivery-side state machine (pre-pilot Task 3). Separate
    # from ``status`` which tracks the approval-side machine.
    #
    #   'unknown'   — default; pre-dispatch.
    #   'sent'      — dispatcher created the Outlook draft (or the
    #                 send-side equivalent).
    #   'delivered' — 4h elapsed without an NDR — presumed delivered.
    #                 The confirmation sweep flips items here.
    #   'failed'    — an NDR was correlated to
    #                 ``executed_internet_message_id``.
    delivery_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown",
        server_default="unknown",
    )
    delivery_status_detail: Mapped[str | None] = mapped_column(Text)
    delivery_status_updated_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Message-ID header Graph proposed when the draft was created;
    # NDRs reference this in In-Reply-To / References so the
    # delivery_status_handler plugin can correlate.
    executed_internet_message_id: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', "
            "'sent', 'dispatch_failed')",
            name="approval_items_status_check",
        ),
        CheckConstraint(
            "delivery_status IN ('unknown', 'sent', 'delivered', 'failed')",
            name="approval_items_delivery_status_check",
        ),
    )
