"""Phase 8b: chat conversations and chat messages

Revision ID: 00137dbf9caf
Revises: f8a9b0c1d2e3
Create Date: 2026-05-29 04:23:12.804963

Threaded chat surface. ``chat_conversations`` is the per-user thread
(one row per conversation); ``chat_messages`` is the append-only
history of user and assistant turns within a thread. Token counts on
assistant messages are persisted for observability; the orchestrator
does not enforce spend limits today.

Both tables FORCE ROW LEVEL SECURITY with the standard four-policy
firm-isolation pattern (SELECT / INSERT / UPDATE / DELETE), identical
to Phase 8a's ``_enable_rls`` helper. ``firm_id`` is denormalised onto
``chat_messages`` so the RLS predicate does not need to join through
``chat_conversations`` on every row scan.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "00137dbf9caf"
down_revision: str | Sequence[str] | None = "f8a9b0c1d2e3"
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
        CREATE TABLE chat_conversations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_chat_conversations_firm_updated_at "
        "ON chat_conversations (firm_id, updated_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_chat_conversations_user_id "
        "ON chat_conversations (user_id)"
    )
    _enable_rls("chat_conversations")

    op.execute(
        """
        CREATE TABLE chat_messages (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_id UUID NOT NULL
                REFERENCES chat_conversations(id) ON DELETE CASCADE,
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_chat_messages_role
                CHECK (role IN ('user', 'assistant', 'system'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_chat_messages_firm_id ON chat_messages (firm_id)"
    )
    op.execute(
        "CREATE INDEX ix_chat_messages_conversation_created "
        "ON chat_messages (conversation_id, created_at)"
    )
    _enable_rls("chat_messages")


def downgrade() -> None:
    for action in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS chat_messages_firm_isolation_{action} "
            f"ON chat_messages"
        )
    op.execute("DROP TABLE IF EXISTS chat_messages CASCADE")
    for action in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS chat_conversations_firm_isolation_{action} "
            f"ON chat_conversations"
        )
    op.execute("DROP TABLE IF EXISTS chat_conversations CASCADE")
