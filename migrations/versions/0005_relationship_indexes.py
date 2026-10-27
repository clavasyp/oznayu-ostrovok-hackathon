"""Обратный индекс D34 для проверки FK при обновлении пакета.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
from sqlalchemy import Index

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

INDEXES = (("ix_hotel_station_station_id", "hotel_station", "station_id"),)


def upgrade():
    from sqlalchemy import MetaData, Table

    bind = op.get_bind()
    for name, table, column in INDEXES:
        target = Table(table, MetaData(), autoload_with=bind)
        Index(name, target.c[column]).create(bind, checkfirst=True)


def downgrade():
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)
