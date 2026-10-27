"""Места рядом — запросом PostGIS (D41): точка места `geog` с индексом GiST вместо таблицы
пар «отель → место»; код подзоны отеля `subzone_id` рядом с названием `subzone`.

Миграция 0001 создаёт схему из текущих моделей, поэтому на чистой базе колонки и индекс уже
есть. Здесь — догонка для баз, созданных раньше: всё через IF NOT EXISTS. Таблицу пар из
прежней ревизии 0004 ветки Севы (`hotel_place`, до 17 млн строк) удаляем.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

from core.db import PLACE_GEOG

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.hotel_place")
    op.execute(
        "ALTER TABLE public.place ADD COLUMN IF NOT EXISTS geog geography(POINT,4326) "
        f"GENERATED ALWAYS AS ({PLACE_GEOG}) STORED"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_place_geog ON public.place USING gist (geog)")
    op.execute("ALTER TABLE public.hotel ADD COLUMN IF NOT EXISTS subzone_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE public.hotel DROP COLUMN IF EXISTS subzone_id")
    op.execute("DROP INDEX IF EXISTS public.ix_place_geog")
    op.execute("ALTER TABLE public.place DROP COLUMN IF EXISTS geog")
