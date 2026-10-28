"""geo.place_count — сколько мест категории в радиусе вокруг отеля (по местам пакета города).

Для характеристик, которых нет в каталоге: «сколько кафе в 500 м», «рестораны в 10 минутах»
(задача 4, D34). Спецификация собирается в `agents/specgen` и ссылается сюда как
`geo.place_count@1`. Считает база (PostGIS, расстояние по прямой по сфероиду WGS84), поэтому
калькулятор работает и в образе приложения, без геобиблиотек.

Параметры (`ctx.params`):

- `category` — ID категории из словаря мест `agents/catalog/places.json`;
- `radius_m` — радиус вокруг отеля, 100–2000 м;
- `store` — хранилище с `place_counts` (передаёт пробный расчёт или включение).

Места в данных карт — не перепись: число — «не меньше», поэтому спецификация разрешает только
«не меньше N» и «есть» (`validate.py`). Мест этой категории в городе нет вовсе — «неизвестно»
с причиной, а не ноль.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from agents.catalog.places import load_places
from core.facts import Confidence, Fact, RecipeAttempt, Source, Status, make_fact_id
from core.plugins import CityContext, Subject, register_calculator

RADIUS_M = (100, 2000)


@register_calculator
class PlaceCount:
    """Сколько мест категории в радиусе — для спецификаций из specgen."""

    id = "geo.place_count"
    version = "1"
    feature_ids: tuple[str, ...] = ()  # характеристику задаёт спецификация

    def run(self, subjects: Sequence[Subject], ctx: CityContext) -> list[Fact]:
        category = str(ctx.params.get("category") or "")
        radius = float(ctx.params.get("radius_m") or 0)
        feature_id = str(ctx.params.get("feature_id") or "place_count")
        store = ctx.params.get("store")
        problem = None
        if load_places().get(category) is None:
            problem = "place_category_unknown"
        elif not RADIUS_M[0] <= radius <= RADIUS_M[1]:
            problem = "radius_out_of_range"
        elif store is None:
            problem = "place_store_missing"
        counts, known = ({}, 0)
        if problem is None:
            counts, known = store.place_counts([s.hotel_id for s in subjects], category, radius)
            if not known:
                problem = "place_no_data"
        params = {"category": category, "radius_m": round(radius)}
        now = datetime.now(UTC)
        facts = []
        for subject in subjects:
            got = counts.get(subject.hotel_id)
            common = {
                "fact_id": make_fact_id(
                    subject.hotel_id, subject.subject_id, feature_id, params, self.version
                ),
                "hotel_id": subject.hotel_id,
                "city_id": ctx.city_id,
                "subject_level": "hotel",
                "subject_id": subject.subject_id,
                "feature_id": feature_id,
                "params": params,
                "unit": "count",
                "algorithm_version": f"{self.id}@{self.version}",
                "calculated_at": now,
                "recipe_attempts": [
                    RecipeAttempt(
                        recipe_id="place_count-R1",
                        data_kind="Б",
                        state="accepted" if problem is None and got else "failed",
                        reason=problem,
                    )
                ],
            }
            if problem is not None or got is None:
                facts.append(
                    Fact(
                        **common,
                        status=Status.unknown,
                        unknown_reason=problem or "hotel_coordinates_missing",
                    )
                )
                continue
            facts.append(
                Fact(
                    **common,
                    value=got["count"],
                    status=Status.computed,
                    recipe_id="place_count-R1",
                    sources=[
                        Source(
                            provider="OpenStreetMap (места пакета города)",
                            license="ODbL-1.0",
                            method="nearby-postgis.1",
                        )
                    ],
                    depends_on=[{"nearest_m": got["nearest_m"], "category": category}],
                    confidence=Confidence(
                        level="medium",
                        reason="Места из данных карт: отмечены не все — число «не меньше».",
                    ),
                    scope_note="Расстояние по прямой от точки отеля; не пеший путь.",
                    hard_condition_eligible=True,
                )
            )
        return facts
