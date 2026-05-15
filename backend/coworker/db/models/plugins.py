"""Per-firm plugin installation model.

Tracks which plugins each firm has enrolled, the firm-specific
config (validated against the plugin's ``config_schema``), and
the operational flags (enabled, dry_run). The plugin's static
metadata (declared triggers, tool categories, system prompt,
goal-construction logic) lives in Python — this table only stores
firm decisions.

UNIQUE(firm_id, plugin_name) enforces at most one installation
per firm per plugin. To re-install (after an uninstall) the
record is reused with updated ``installed_at`` / ``updated_at``.
"""
import datetime as _dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base


class PluginInstallation(Base):
    """One firm-plugin enrolment row.

    Phase 6-2's PluginExecutor reads this row before every run to
    pick up the firm's current config / dry_run flag. A firm that
    flips dry_run mid-day affects subsequent runs immediately; in-
    flight runs use the config snapshot taken at run-start time.
    """

    __tablename__ = "plugin_installations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plugin_name: Mapped[str] = mapped_column(String(100), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(50), nullable=False)

    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    installed_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # When this installation most recently fired a ``scheduled``
    # event (Phase 12-3). NULL until the first fire. The Phase 12-3
    # scheduler reads this to compute the next CronTrigger fire
    # time and enqueues when it's elapsed.
    last_fired_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        UniqueConstraint(
            "firm_id", "plugin_name",
            name="plugin_installations_firm_plugin_unique",
        ),
    )
