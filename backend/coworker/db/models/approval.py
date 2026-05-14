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
    )
