"""Specialist agents and their versioned system prompts (Phase 8).

A ``Specialist`` row is the registry entry for one narrow technical
domain (GST, SMSF, Division 7A, trust tax, CGT). Its active prompt
lives in a ``SpecialistPromptVersion`` row; updates insert a new
version and flip ``Specialist.active_version_id`` so the prior
version stays in the table with ``status='retired'`` for
reproducibility. ``agent_trace_steps`` records the
``specialist_prompt_version_id`` it consulted so the exact prompt
text can be reconstructed later.

Both tables FORCE RLS with the standard firm-isolation policies;
``firm_id`` is denormalised onto the versions table so the
policy predicate doesn't need a join.
"""
import datetime as _dt
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base


class Specialist(Base):
    """One specialist per (firm, name). ``active_version_id`` points
    at the current ``SpecialistPromptVersion``; NULL only between
    creating the row and seeding its first version (i.e. never
    visible to GET prompt — the route 404s on NULL active).
    """

    __tablename__ = "specialists"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    extended_thinking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # The FK constraint is created in the migration after the versions
    # table exists, so we declare the column without a ForeignKey() here
    # to avoid a chicken-and-egg in Base.metadata at create_all() time.
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("firm_id", "name", name="uq_specialist_firm_name"),
    )


class SpecialistPromptVersion(Base):
    """One row per historical prompt text. Exactly one row per
    specialist has ``status='active'`` (enforced by partial unique
    index in the migration); all others are ``'retired'``.
    """

    __tablename__ = "specialist_prompt_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
    )
    specialist_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("specialists.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    change_summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "specialist_id", "version_number",
            name="uq_version_specialist_number",
        ),
        CheckConstraint(
            "status IN ('active', 'retired')", name="ck_version_status"
        ),
        Index(
            "uq_one_active_per_specialist",
            "specialist_id",
            unique=True,
            postgresql_where="status = 'active'",
        ),
    )
