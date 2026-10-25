"""Пакеты из расчётов v9: покрытие городов, сверка с Островком, поля вопроса отельеру;
необязательные JSON-поля факта и кандидата ручной обработки (value, refresh_after, corrected_value)

Миграция 0001 создаёт схему из текущих моделей, поэтому на чистой базе эти таблицы уже есть.
Здесь — догонка для баз, созданных раньше: всё через IF NOT EXISTS.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Mapped[Any] без явного nullable давал NOT NULL: факт без refresh_after не записывался.
    op.execute("ALTER TABLE fact ALTER COLUMN value DROP NOT NULL")
    op.execute("ALTER TABLE fact ALTER COLUMN refresh_after DROP NOT NULL")
    op.execute("ALTER TABLE curation_claim ALTER COLUMN corrected_value DROP NOT NULL")
    op.execute("ALTER TABLE question ADD COLUMN IF NOT EXISTS fields JSONB DEFAULT '[]'::jsonb")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ota_comparison (
            id BIGSERIAL PRIMARY KEY,
            city_id TEXT NOT NULL,
            hotel_id TEXT NOT NULL,
            feature_id TEXT NOT NULL,
            ota_value TEXT,
            other_value JSONB,
            classification TEXT NOT NULL,
            numeric_comparison TEXT,
            resolution TEXT,
            source JSONB,
            pack_version TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ota_comparison_city_id ON ota_comparison (city_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ota_comparison_hotel_id ON ota_comparison (hotel_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS city_coverage (
            city_id TEXT NOT NULL,
            feature_id TEXT NOT NULL,
            feature_name TEXT,
            n_hotels INTEGER NOT NULL,
            computed INTEGER,
            estimate INTEGER,
            unknown INTEGER,
            not_applicable INTEGER,
            PRIMARY KEY (city_id, feature_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS city_coverage")
    op.execute("DROP TABLE IF EXISTS ota_comparison")
    op.execute("ALTER TABLE question DROP COLUMN IF EXISTS fields")
