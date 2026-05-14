"""Phase 9-6: two-person approval columns

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-05-14 14:25:00.000000

Two-person approval for high-sensitivity categories (the
TWO_PERSON_REQUIRED_CATEGORIES setting). The principal approving
once isn't enough — a second different user must also sign off
before the row moves out of ``pending``.

Schema additions:
- ``required_approvals``: how many distinct users must sign. Set
  at insert time by ``create_approval`` from the firm's
  ``TWO_PERSON_REQUIRED_CATEGORIES`` setting. Default 1 covers the
  single-person path (most categories).
- ``approval_signatures``: JSONB array of
  ``[{user_id, signed_at, notes}]``. Each ``approve`` call
  appends a row; once the count of distinct user_ids reaches
  required_approvals the helper transitions the row to
  ``approved``.

A signer can't sign twice — the helper checks
``user_id NOT IN existing signatures`` before append.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | Sequence[str] | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE approval_items "
        "ADD COLUMN required_approvals INT NOT NULL DEFAULT 1 "
        "CHECK (required_approvals >= 1 AND required_approvals <= 5)"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "ADD COLUMN approval_signatures JSONB NOT NULL "
        "DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE approval_items "
        "DROP COLUMN IF EXISTS approval_signatures"
    )
    op.execute(
        "ALTER TABLE approval_items "
        "DROP COLUMN IF EXISTS required_approvals"
    )
