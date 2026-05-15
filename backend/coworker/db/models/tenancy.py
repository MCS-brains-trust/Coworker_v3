import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coworker.db.base import Base


class Firm(Base):
    """A tenant. Every firm using CoWorker has one row here."""
    __tablename__ = "firms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    abn: Mapped[str | None] = mapped_column(String(11))
    address: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(50), default="Australia/Melbourne")

    # Mode of operation
    shadow_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Sandbox mode (pre-pilot Task 1). Independent of shadow_mode.
    # When True, connector-layer outbound writes (Graph drafts,
    # FuseSign envelopes) reroute recipients to
    # ``sandbox_outbound_catchall``; the rerouting fires even with
    # shadow_mode=False so we can exercise the whole pipeline
    # against synthetic data without touching real clients. DB-side
    # CHECK constraint enforces catchall presence when sandbox=True.
    is_sandbox: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    sandbox_outbound_catchall: Mapped[str | None] = mapped_column(Text)

    # Per-firm Azure AD application
    azure_tenant_id: Mapped[str | None] = mapped_column(String(100))
    azure_client_id: Mapped[str | None] = mapped_column(String(100))
    azure_client_secret_ciphertext: Mapped[bytes | None] = mapped_column()  # encrypted

    # Anthropic key — by default firms use the platform-provided key,
    # but enterprise firms can BYO their own
    anthropic_api_key_ciphertext: Mapped[bytes | None] = mapped_column()

    # XPM
    xpm_account_id: Mapped[str | None] = mapped_column(String(100))
    xpm_client_id: Mapped[str | None] = mapped_column(String(100))
    xpm_client_secret_ciphertext: Mapped[bytes | None] = mapped_column()
    xpm_access_token_ciphertext: Mapped[bytes | None] = mapped_column()
    xpm_refresh_token_ciphertext: Mapped[bytes | None] = mapped_column()
    xpm_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # FuseSign
    fusesign_api_key_ciphertext: Mapped[bytes | None] = mapped_column()

    # Teams
    teams_webhook_url_ciphertext: Mapped[bytes | None] = mapped_column()

    # SharePoint root
    sharepoint_site_id: Mapped[str | None] = mapped_column(String(200))
    sharepoint_clients_drive_id: Mapped[str | None] = mapped_column(String(200))
    sharepoint_clients_folder_path: Mapped[str] = mapped_column(String(500), default="/Server/Clients")

    # Settings JSON for things that don't deserve their own column
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    users: Mapped[list["User"]] = relationship(back_populates="firm")


class User(Base):
    """A staff member at a firm."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    firm_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("firms.id", ondelete="CASCADE"), nullable=False)

    # Identity from Microsoft
    azure_object_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    upn: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    mail: Mapped[str | None] = mapped_column(String(200), index=True)

    # Role within the firm
    role: Mapped[Literal["owner", "principal", "accountant", "reception", "viewer"]] = mapped_column(String(20), default="accountant")

    # Per-user encrypted Microsoft Graph delegated tokens
    ms_access_token_ciphertext: Mapped[bytes | None] = mapped_column()
    ms_refresh_token_ciphertext: Mapped[bytes | None] = mapped_column()
    ms_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Mailbox this user owns. For Reception users, this is the shared inbox.
    monitored_mailbox: Mapped[str | None] = mapped_column(String(200))
    is_active_processor: Mapped[bool] = mapped_column(Boolean, default=False)
    """If True, plugins fire for this user. False = passive monitoring only."""

    is_reception_mode: Mapped[bool] = mapped_column(Boolean, default=False)

    # Style profile (JSON, see Phase 11)
    style_profile: Mapped[dict | None] = mapped_column(JSONB)
    style_profile_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    firm: Mapped["Firm"] = relationship(back_populates="users")

    __table_args__ = (
        Index("ix_users_firm_id_role", "firm_id", "role"),
    )
