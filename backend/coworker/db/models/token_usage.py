"""Per-firm / per-model / per-day token usage row.

Permanent home for the counters that Phase 3B-4 records in Redis with
a 35-day TTL. Phase 3H-2's flush function reads the Redis hashes and
UPSERTs into this table; Phase 3H-3's CLI report queries it for
monthly summaries.
"""
import datetime as _dt
import uuid

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base


class TokenUsageRow(Base):
    """One row per (firm, model, UTC date).

    Composite primary key — there's nothing to reference these rows
    by other than the natural tuple. The flush function uses
    ``ON CONFLICT (firm_id, model, day) DO UPDATE`` so re-runs are
    idempotent.

    All counters are BIGINT because a firm running specialists with
    extended thinking can clear 100M tokens / month per model;
    BIGINT removes the headroom question entirely.
    """

    __tablename__ = "token_usage"

    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    day: Mapped[_dt.date] = mapped_column(Date, primary_key=True)

    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    calls: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
