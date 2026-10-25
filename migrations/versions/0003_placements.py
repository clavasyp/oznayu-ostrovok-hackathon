"""Размещения категорий (D17): placement_set → placement (здание и этаж) → placement_window

На чистой базе таблицы создаёт 0001 из текущих моделей; здесь — догонка для баз, созданных раньше.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS placement_set (
            room_type_id TEXT PRIMARY KEY REFERENCES room_type (id),
            completeness TEXT NOT NULL,
            guarantee TEXT NOT NULL,
            status TEXT NOT NULL,
            source JSONB,
            version INTEGER,
            fact_id TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS placement (
            id TEXT PRIMARY KEY,
            room_type_id TEXT NOT NULL REFERENCES placement_set (room_type_id),
            building_id TEXT NOT NULL,
            building_part_id TEXT,
            floor INTEGER NOT NULL,
            floor_label TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_placement_room_type_id ON placement (room_type_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS placement_window (
            placement_id TEXT NOT NULL REFERENCES placement (id),
            facade_id TEXT NOT NULL,
            azimuth_deg DOUBLE PRECISION,
            PRIMARY KEY (placement_id, facade_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS placement_window")
    op.execute("DROP TABLE IF EXISTS placement")
    op.execute("DROP TABLE IF EXISTS placement_set")
