"""Корпуса/фасады D39 и предварительно рассчитанные таблицы D34.

Пары «отель → место» в базе не хранятся (D41): их считает запрос по `place.geog`, миграция 0006.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

from core.db import Facade, HotelStation, Place, TransitStation, TransitTime

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TABLES = (Facade, Place, TransitStation, HotelStation, TransitTime)


def upgrade() -> None:
    for model in TABLES:
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for model in reversed(TABLES):
        model.__table__.drop(op.get_bind(), checkfirst=True)
