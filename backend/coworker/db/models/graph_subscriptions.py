"""Microsoft Graph change-notification subscriptions model.

One row per (firm, user, resource) tracks the active Graph
subscription Microsoft will post change notifications to. The
client_state secret is encrypted with firm-AAD so a DB dump
can't be replayed back at the webhook as a valid notification.
"""
import datetime as _dt
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base


class GraphSubscription(Base):
    """A persistent Microsoft Graph subscription record.

    The Phase 11-2 bootstrap function looks up by
    (firm_id, user_id, resource) to decide create-vs-renew. The
    webhook receiver looks up by ``subscription_id`` to validate
    incoming notifications' clientState. The Phase 11-3 renewal
    job scans by ``expiration_date_time``.

    Encryption: ``client_state_ciphertext`` is bound to the
    firm via AAD so a row exported to another firm can't be
    decrypted. We don't store it in plaintext because a DB dump
    would otherwise let an attacker fabricate notifications.
    """

    __tablename__ = "graph_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Microsoft's subscription id — UUID-shaped but stored as a
    # string so the column can hold whatever Graph hands us.
    subscription_id: Mapped[str] = mapped_column(
        String(100), nullable=False
    )

    # Graph resource path being monitored, e.g.
    # ``users/{userId}/mailFolders('Inbox')/messages``.
    resource: Mapped[str] = mapped_column(String(500), nullable=False)

    notification_url: Mapped[str] = mapped_column(
        String(500), nullable=False
    )
    change_type: Mapped[str] = mapped_column(String(100), nullable=False)

    # Encrypted with firm-AAD; decrypted only when validating an
    # incoming notification.
    client_state_ciphertext: Mapped[bytes] = mapped_column(nullable=False)

    expiration_date_time: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_renewed_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # When Microsoft most recently told us this subscription dropped
    # notifications (lifecycleEvent="missed"). Cleared by the backfill
    # function once it has re-enqueued the catch-up window.
    last_missed_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            name="graph_subscriptions_subscription_id_unique",
        ),
        UniqueConstraint(
            "firm_id", "user_id", "resource",
            name="graph_subscriptions_firm_user_resource_unique",
        ),
    )
