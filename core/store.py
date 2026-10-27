"""Хранилище для API и поиска: один интерфейс над заглушками (`app/stub_store.py`) и над базой
(`core/store_db.py`). Поиск (`core/search.py`) и маршруты API видят только этот интерфейс,
поэтому режим данных (`DATA_MODE`) меняет источник, но не ответы.

Формы словарей — те, на которых строится интерфейс (Андрей): отель, город, покрытие, вопрос,
сверка с Островком. Новые ключи добавлять можно, переименовывать — только вместе с интерфейсом.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from core.facts import Fact

if TYPE_CHECKING:
    from core.hotelier import AnswerResult


class Store(Protocol):
    stub: bool  # True — заглушки из app/fixtures, False — база

    def cities(self) -> list[dict[str, Any]]: ...

    def coverage(self, city_id: str) -> list[dict[str, Any]] | None:
        """None — такого города нет."""
        ...

    def hotels(self, city_id: str | None = None) -> list[dict[str, Any]]: ...

    def hotel(self, hotel_id: str) -> dict[str, Any] | None: ...

    def hotel_facts(self, hotel_id: str) -> dict[str, list[Fact]]:
        """Все действующие факты отеля по характеристикам."""
        ...

    def fact(self, fact_id: str) -> Fact | None: ...

    def facts_for(
        self,
        feature_id: str,
        hotel_ids: Collection[str],
        params: dict[str, Any] | None = None,
        key_params: Collection[str] = (),
    ) -> dict[str, list[Fact]]:
        """Действующие факты одной характеристики для набора отелей — один запрос на условие.
        Для поиска и сверки ответа: паспорт (источники, попытки рецептов) хранилище может не
        отдавать — полный факт даёт `fact` по id.

        `params` — подсказка, а не фильтр смысла: хранилище может отдать только факты с этими
        параметрами (и без `key_params`, которых нет в запросе), но у отеля, где фактов с ними
        нет, а с другими есть, оставляет один такой — поиск скажет «посчитано на другие даты»,
        а не «нет данных». Сравнение по параметрам всё равно делает поиск (`_params_match`)."""
        ...

    def room_types(self, hotel_ids: Collection[str]) -> dict[str, list[dict[str, Any]]]:
        """Категории отелей с размещениями (D17):
        `{room_type_id, name, placement_set: {completeness, guarantee, status, source} | None,
        placements: [{placement_id, building_id, floor, floor_label, windows: [facade_id]}]}`.
        """
        ...

    def placement_geometry(self, hotel_id: str) -> dict[str, Any]:
        """Корпуса и фасады отеля для размещений D17 — как `GET /api/hotels/{id}/geometry`:
        `facades`, `placement_building_aliases`, `placement_facades`,
        `placement_building_choices` (`core.placement_catalog`)."""
        ...

    def add_building_alias(self, hotel_id: str, building_id: str, alias: dict[str, Any]) -> bool:
        """Название корпуса со слов отеля → `hotel_building.evidence.hotel_aliases`:
        `{name, status: hotelier | simulated_hotelier, source: {question_id, answered_at,
        quote…}}`. False — у отеля нет такого корпуса."""
        ...

    def questions(self, hotel_id: str) -> list[dict[str, Any]]: ...

    def question(
        self, hotel_id: str, question_id: int | None, feature_id: str | None
    ) -> dict[str, Any] | None:
        """Вопрос отелю по ID или по характеристике."""
        ...

    def set_question_state(self, question_id: int, state: str, at: datetime) -> None: ...

    def apply_answer(self, result: AnswerResult) -> None:
        """Записать ответ отельера целиком: вопрос, факты, замены, противоречия, размещения.
        В базе — одной транзакцией. `core.hotelier.AnswerError`, если категории нет."""
        ...

    def fact_conflicts(self, city_id: str | None = None) -> list[dict[str, Any]]:
        """Противоречия между фактами: наш расчёт против ответа отеля и т. п."""
        ...

    def conflicts(self, city_id: str | None = None) -> list[dict[str, Any]]:
        """Сверка с карточкой Островка: совпало, расходится, сравнить нечем."""
        ...

    def record_unmatched(self, events: list[dict[str, Any]]) -> None:
        """Журнал «вне каталога» (D34): события `unmatched_atom` из разбора запроса."""
        ...

    def unmatched_events(
        self, city_id: str | None = None, stratum: str | None = None, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        """События журнала за окно — для экрана «Что спрашивают, а мы не знаем»."""
        ...
