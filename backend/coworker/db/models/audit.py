import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base


class AuditLogEntry(Base):
    """Tamper-evident append-only audit log.

    Each entry contains a `prev_hash` referring to the hash of the previous entry's
    full payload. Tampering with any historical entry breaks the chain.
    A daily 'anchor' is computed (SHA-256 of the latest entry hash) and emailed
    to the firm principal so even root-on-droplet tampering is detectable.
    """
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    firm_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("firms.id"), index=True, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    """user, system, plugin, agent"""
    actor_id: Mapped[str | None] = mapped_column(String(200))
    """UUID of user, name of plugin, etc."""

    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    """e.g. login.success, draft.created, approval.approved, memory.deleted"""
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(200))

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        Index("ix_audit_firm_action_time", "firm_id", "action", "occurred_at"),
    )
