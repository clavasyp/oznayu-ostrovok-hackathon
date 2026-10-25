"""Словарь характеристик для разбора запроса и ответов отельера (`features.json`).

Здесь только смысл: ID, поля значений с единицами, уровень, синонимы. Значений характеристик
нет — их считает код и хранит база фактов (D8). Названия — из расчёта v9.

Уровень решает привязку условия (D17): `room_type`, `facade`, `placement` — уровень номера,
проверяется на одном размещении (`same_placement`); остальное — уровень отеля.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).parent / "features.json"
ROOM_LEVELS = frozenset({"room_type", "facade", "placement"})


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str  # bool | number | integer | string | enum | list | time
    unit: str | None = None
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class Feature:
    id: str
    title: str
    short: str
    level: str
    source: str  # computed | hotelier | placement
    fields: dict[str, FieldSpec]
    default_field: str | None
    ops: tuple[str, ...]
    params: tuple[str, ...]
    synonyms: tuple[str, ...]

    @property
    def room_level(self) -> bool:
        return self.level in ROOM_LEVELS

    def field_spec(self, name: str | None) -> FieldSpec | None:
        name = name or self.default_field
        if name is None:
            return None
        return self.fields.get(name)


@dataclass(frozen=True)
class Catalog:
    features: dict[str, Feature]
    version: str
    walk_speed_m_per_min: float

    def get(self, feature_id: str) -> Feature | None:
        return self.features.get(feature_id)

    def prompt_listing(self) -> str:
        """Каталог для системного промпта: одна строка на характеристику, порядок стабильный."""
        lines = []
        for f in self.features.values():
            fields = ", ".join(
                f"{s.name}:{s.type}"
                + (f"[{s.unit}]" if s.unit else "")
                + (f"{{{'|'.join(s.values)}}}" if s.values else "")
                for s in f.fields.values()
            )
            lines.append(
                f"{f.id} | {f.short} | уровень {f.level} | поля: {fields or 'значение'}"
                f" | по умолчанию: {f.default_field or '—'} | операторы: {','.join(f.ops)}"
                + (f" | параметры: {','.join(f.params)}" if f.params else "")
                + f" | напр.: {'; '.join(f.synonyms[:4])}"
            )
        return "\n".join(lines)


@lru_cache(maxsize=1)
def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    raw = path.read_bytes()
    doc = json.loads(raw)
    features = {}
    for f in doc["features"]:
        fields = {
            name: FieldSpec(name, s["type"], s.get("unit"), tuple(s.get("values", ())))
            for name, s in f["fields"].items()
        }
        features[f["id"]] = Feature(
            id=f["id"],
            title=f["title"],
            short=f["short"],
            level=f["level"],
            source=f["source"],
            fields=fields,
            default_field=f["default_field"],
            ops=tuple(f["ops"]),
            params=tuple(f["params"]),
            synonyms=tuple(f["synonyms"]),
        )
    return Catalog(
        features=features,
        version=hashlib.sha256(raw).hexdigest()[:12],
        walk_speed_m_per_min=float(doc["walk_speed_m_per_min"]),
    )


# --- Типизация значений: строка от модели или правил → значение поля в его единицах ---

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")
_TRUE = {"true", "да", "yes", "1", "есть"}
_FALSE = {"false", "нет", "no", "0"}
# Единицы гостя → (единица поля, множитель). Минуты в метрах — по скорости пешехода (отдельно).
_UNITS: dict[str, dict[str, float]] = {
    "m": {"m": 1, "м": 1, "метр": 1, "km": 1000, "км": 1000, "километр": 1000},
    "min": {"min": 1, "мин": 1, "минут": 1, "h": 60, "ч": 60, "час": 60},
    "h": {"h": 1, "ч": 1, "час": 1, "min": 1 / 60, "мин": 1 / 60},
    "mm": {"mm": 1, "мм": 1, "см": 10, "cm": 10, "м": 1000, "m": 1000},
    "kg": {"kg": 1, "кг": 1, "килограмм": 1},
    "Mbit/s": {"mbit/s": 1, "мбит": 1, "мегабит": 1, "mbps": 1},
    "RUB": {"rub": 1, "руб": 1, "₽": 1, "р": 1},
    "kW": {"kw": 1, "квт": 1, "киловатт": 1},
    "years": {"year": 1, "год": 1, "лет": 1},
    "m2": {"m2": 1, "м2": 1, "м²": 1, "кв": 1},
    "ms": {"ms": 1, "мс": 1, "миллисекунд": 1},
    "%": {"%": 1, "процент": 1},
    "dB(A)": {"db": 1, "дб": 1, "децибел": 1},
}
_WALK = ("мин", "min", "минут")


def _unit_factor(field_unit: str, unit: str, walk_speed: float) -> float:
    u = unit.strip().lower().rstrip(".")
    if not u or u == field_unit.lower():
        return 1.0
    if field_unit == "m" and u.startswith(_WALK):
        return walk_speed  # «10 минут пешком» → метры маршрута по стандартной скорости
    table = _UNITS.get(field_unit, {})
    for alias, factor in table.items():
        if u.startswith(alias):
            return factor
    raise ValueError(f"единица «{unit}» не переводится в {field_unit}")


def coerce_value(spec: FieldSpec | None, value: str, unit: str, walk_speed: float) -> Any:
    """Строка значения → типизированное значение поля. Неразбираемое — `ValueError`."""
    v = value.strip()
    if spec is None or spec.type == "bool":
        low = v.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        if spec is None and (m := _NUM.fullmatch(v.replace(" ", ""))):
            return float(m.group().replace(",", "."))
        raise ValueError(f"«{value}» — не да / нет")
    if spec.type in ("number", "integer"):
        m = _NUM.search(v.replace(" ", ""))
        if not m:
            raise ValueError(f"«{value}» — не число")
        num = float(m.group().replace(",", "."))
        if spec.unit:
            num *= _unit_factor(spec.unit, unit, walk_speed)
        if spec.type == "integer":
            return round(num)
        num = round(num, 3)
        return int(num) if num.is_integer() else num
    if spec.type == "enum":
        if v not in spec.values:
            raise ValueError(f"«{value}» не из {spec.values}")
        return v
    if spec.type == "list":
        items = [x.strip() for x in v.split(",") if x.strip()]
        if spec.values and any(x not in spec.values for x in items):
            raise ValueError(f"«{value}» не из {spec.values}")
        return items
    if spec.type == "time":
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError(f"«{value}» — не время ЧЧ:ММ")
        return v
    return v
