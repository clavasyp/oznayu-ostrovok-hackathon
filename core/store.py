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

    def facts_for(self, feature_id: str, hotel_ids: Collection[str]) -> dict[str, list[Fact]]:
        """Действующие факты одной характеристики для набора отелей — один запрос на условие."""
        ...

    def room_types(self, hotel_ids: Collection[str]) -> dict[str, list[dict[str, Any]]]:
        """Категории отелей с размещениями (D17):
        `{room_type_id, name, placement_set: {completeness, guarantee, status, source} | None,
        placements: [{placement_id, building_id, floor, floor_label, windows: [facade_id]}]}`.
        """
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
