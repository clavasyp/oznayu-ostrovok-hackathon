"""Хранилище для API и поиска: один интерфейс над заглушками (`app/stub_store.py`) и над базой
(`core/store_db.py`). Поиск (`core/search.py`) и маршруты API видят только этот интерфейс,
поэтому режим данных (`DATA_MODE`) меняет источник, но не ответы.

Формы словарей — те, на которых строится интерфейс (Андрей): отель, город, покрытие, вопрос,
сверка с Островком. Новые ключи добавлять можно, переименовывать — только вместе с интерфейсом.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Protocol

from core.facts import Fact


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

    def conflicts(self, city_id: str | None = None) -> list[dict[str, Any]]:
        """Сверка с карточкой Островка: совпало, расходится, сравнить нечем."""
        ...
