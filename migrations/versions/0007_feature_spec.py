"""Характеристики, включённые по описанию текстом (задача 4): таблица `feature_spec`.

Миграция 0001 создаёт схему из текущих моделей, поэтому на чистой базе таблица уже есть. Здесь —
догонка для баз, созданных раньше.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.feature_spec (
            id TEXT PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            title TEXT NOT NULL,
            yaml TEXT NOT NULL,
            feature JSONB NOT NULL,
            cities JSONB NOT NULL DEFAULT '{}'::jsonb,
            source_text TEXT,
            activated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.feature_spec")
