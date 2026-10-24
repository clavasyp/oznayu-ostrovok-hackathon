"""Начальная схема: PostGIS + таблицы из core/db.py

Первая миграция создаёт схему прямо из моделей, чтобы у неё был один источник правды.
Дальнейшие изменения — обычными ревизиями: `alembic revision --autogenerate -m "..."`.

Revision ID: 0001
Revises:
Create Date: 2026-10-24
"""

from alembic import op

from core.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
