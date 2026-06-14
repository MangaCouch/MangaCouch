"""initial schema (baseline)

The baseline is built directly from the ORM metadata so it can never drift from the models. Later
revisions use explicit ``op`` operations (and ``--autogenerate``) against this baseline.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from mangacouch.db import models  # noqa: F401 — register all tables on Base.metadata
from mangacouch.db.base import Base

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
