"""documents.external_id до 2048 символов: длинные URL страниц сайта (REVIEW.md: K-15)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Расширение varchar — без перезаписи таблицы и без перестройки индекса.
    op.alter_column(
        "documents",
        "external_id",
        type_=sa.String(2048),
        existing_type=sa.String(512),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Упадёт, если уже есть external_id длиннее 512: данные не удаляем молча (их чанки
    # ещё и в Qdrant) — такие документы убирают вручную до отката.
    op.alter_column(
        "documents",
        "external_id",
        type_=sa.String(512),
        existing_type=sa.String(2048),
        existing_nullable=False,
    )
