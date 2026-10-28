"""Индекс фактов по дате (закат по датам — тысячи фактов на отель): поиск берёт одну дату или
«весь год» по индексу (`feature_id`, `hotel_id`, `params ->> 'date'`).

Миграция 0001 создаёт схему из текущих моделей, поэтому на чистой базе индекс уже есть. Здесь —
догонка для баз, созданных раньше.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_fact_feature_hotel_date "
        "ON public.fact (feature_id, hotel_id, (params ->> 'date'))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_fact_feature_hotel_date")
