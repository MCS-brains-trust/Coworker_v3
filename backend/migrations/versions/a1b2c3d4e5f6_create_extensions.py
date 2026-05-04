"""Create required Postgres extensions

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-05-04 14:30:00.000000

Enables the three Postgres extensions CoWorker depends on:

- vector   (pgvector): embedding storage / similarity search (Phase 4).
- pg_trgm: trigram similarity for fuzzy text matching.
- pgcrypto: cryptographic primitives used by some helpers.

Runs before the Phase 2 schema migration so a fresh checkout can go
straight from `createdb coworker` to `alembic upgrade head` with no
manual psql step.

Host requirement (one-time, not part of this migration):
    pgvector must have `trusted = true` set in
    /usr/share/postgresql/{version}/extension/vector.control
    so a non-superuser role (the coworker app role) can create it.
    pg_trgm and pgcrypto are trusted by default in PostgreSQL 13+.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
