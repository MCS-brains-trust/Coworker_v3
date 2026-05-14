"""Phase 11-1: graph_subscriptions

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-05-14 11:30:00.000000

Persistent record of every Microsoft Graph change-notification
subscription we hold. Until this lands, the webhook receiver
returns 202 for any well-formed payload posted to a known firm
slug — there's no way to validate that the inbound notification
actually corresponds to a subscription we created.

What this table buys us:

1. **clientState validation**: each subscription's secret is
   stored encrypted (with firm-AAD); the webhook receiver looks
   up the row by ``subscription_id`` from the notification body
   and asserts the encrypted ``client_state`` decrypts to the
   value Microsoft echoed back.

2. **Renewal job**: Graph caps message subscriptions at ~3 days.
   The Phase 11-3 scheduler scans for rows with
   ``expiration_date_time < now + buffer`` and renews them.

3. **Bootstrap idempotency**: ``ensure_subscription`` checks for
   an existing (firm_id, user_id, resource) row before creating
   a fresh one — a worker restart doesn't double-subscribe.

UNIQUE(subscription_id) is enforced globally because Microsoft
guarantees the id is a UUID and identifies the lookup path
the webhook uses (no firm_id is available before the lookup).
UNIQUE(firm_id, user_id, resource) prevents accidental
duplicates within a firm.

RLS+FORCE so the renewal worker / firm-scoped views can iterate
safely under the standard firm_context pattern.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RLS_MATCH = "firm_id = NULLIF(current_setting('app.firm_id', true), '')::uuid"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for action in ("select", "insert", "update", "delete"):
        if action == "insert":
            op.execute(
                f"CREATE POLICY {table}_firm_isolation_{action} ON {table} "
                f"FOR INSERT WITH CHECK ({_RLS_MATCH})"
            )
        elif action == "update":
            op.execute(
                f"CREATE POLICY {table}_firm_isolation_{action} ON {table} "
                f"FOR UPDATE USING ({_RLS_MATCH}) WITH CHECK ({_RLS_MATCH})"
            )
        else:
            op.execute(
                f"CREATE POLICY {table}_firm_isolation_{action} ON {table} "
                f"FOR {action.upper()} USING ({_RLS_MATCH})"
            )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE graph_subscriptions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            subscription_id VARCHAR(100) NOT NULL,
            resource VARCHAR(500) NOT NULL,
            notification_url VARCHAR(500) NOT NULL,
            change_type VARCHAR(100) NOT NULL,
            client_state_ciphertext BYTEA NOT NULL,
            expiration_date_time TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_renewed_at TIMESTAMPTZ,
            CONSTRAINT graph_subscriptions_subscription_id_unique
                UNIQUE (subscription_id),
            CONSTRAINT graph_subscriptions_firm_user_resource_unique
                UNIQUE (firm_id, user_id, resource)
        )
        """
    )
    # Lookup index for the renewal job — find rows expiring soon.
    op.execute(
        "CREATE INDEX ix_graph_subscriptions_expiration "
        "ON graph_subscriptions (expiration_date_time)"
    )
    # The webhook receiver looks up by subscription_id. UNIQUE
    # already creates an index, so no extra index needed.
    _enable_rls("graph_subscriptions")


def downgrade() -> None:
    for op_name in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS "
            f"graph_subscriptions_firm_isolation_{op_name} "
            f"ON graph_subscriptions"
        )
    op.execute("DROP TABLE IF EXISTS graph_subscriptions CASCADE")
