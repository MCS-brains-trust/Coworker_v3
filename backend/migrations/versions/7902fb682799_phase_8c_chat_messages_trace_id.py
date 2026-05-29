"""Phase 8c: chat_messages.trace_id

Revision ID: 7902fb682799
Revises: 00137dbf9caf
Create Date: 2026-05-29 06:23:20.232957

Links a chat assistant message back to its ``agent_traces`` row, so
the audit view can navigate from the user-visible conversation to the
underlying orchestrator + specialist consultation steps recorded
during the turn. Nullable because 003d-1 chat messages (and any
user-role messages, which never have a trace) carry NULL.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7902fb682799"
down_revision: str | Sequence[str] | None = "00137dbf9caf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column(
            "trace_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_chat_messages_trace",
        "chat_messages",
        "agent_traces",
        ["trace_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_chat_messages_trace_id",
        "chat_messages",
        ["trace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_trace_id", table_name="chat_messages")
    op.drop_constraint(
        "fk_chat_messages_trace", "chat_messages", type_="foreignkey"
    )
    op.drop_column("chat_messages", "trace_id")
