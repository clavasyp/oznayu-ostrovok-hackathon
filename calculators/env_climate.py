"""C01–C03 — климат по месяцам: ERA5 1991–2020 через Open-Meteo (перенос из расчётов v9).

Перенос `climate_aggregate` / `load_climate` / `climate_cell` из
`research_notes/Расчёт характеристик/scripts/environment.py` (v9) на контракт `Fact`:

- **C01** — средняя месячная температура воздуха на 2 м, °C;
- **C02** — среднее по годам число дней месяца с осадками ≥ 1 мм/сутки;
- **C03** — среднее по годам число дней месяца с Tmax > 30 °C.

Месяцы — июль и ноябрь (даты поездок трека), период — 30 полных лет 1991–2020.
Значение считается только если в каждом из 30 лет месяц полный: любой пропуск отклоняет
результат целиком (`unknown` с причиной), а не превращается в ноль.

Калькулятор читает **только локальный кеш** ответов Open-Meteo (`era5_{city}_*.json`
рядом с `*.meta.json` со `http_status=200`). Сети не касается: сбор кеша — отдельный шаг,
так что расчёт воспроизводим и не тратит квоту (D22).

Правила: D18 (это `computed`, не оценка), D22 (Open-Meteo — разрешённый путь к ERA5),
D5 (неизвестное — не «нет»).
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.facts import Confidence, Fact, RecipeAttempt, Source, Status, make_fact_id
from core.packs import repo_relative
from core.plugins import CityContext, Subject, register_calculator

MONTHS = (7, 11)
YEARS = tuple(range(1991, 2021))
TRIP_START = {7: "2027-07-15", 11: "2026-11-14"}
REFRESH_AFTER = "2027-10-24T00:00:00+00:00"
PERIOD = {"period_start": "1991-01-01", "period_end": "2020-12-31"}

# Как в v9: фиксированный текущий пояс, без исторического перехода на летнее время.
UTC_OFFSET = {
    "sochi": 3,
    "moscow": 3,
    "spb": 3,
    "kazan": 3,
    "kaliningrad": 2,
    "minsk": 3,
    "istanbul": 3,
    "dubai": 4,
}

# Какое поле суточного ряда агрегируется и в каких единицах публикуется значение.
FIELD = {"C01": "mean", "C02": "rain", "C03": "maximum"}
UNITS = {"C01": "°C", "C02": "days/month", "C03": "days/month"}
SCOPE_NOTE = (
    "Историческое распределение месяца по реанализу за 30 полных лет; не прогноз на 2026/2027; "
    "ближайшая ячейка 0,25°, без поправки на рельеф у отеля."
)
CONFIDENCE = {
    "level": "medium",
    "reason": (
        "30/30 полных месяцев; явно ERA5; грубая прибрежная/горная ячейка "
        "и округление API 0,1 °C остаются."
    ),
}

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_B = WGS84_A * (1 - WGS84_F)


@dataclass(frozen=True)
class ClimateDay:
    """Один суточный ряд ячейки: дата, средняя T, максимум T, осадки."""

    date: str
    mean: float | None
    maximum: float | None
    rain: float | None


def climate_cell(latitude: float, longitude: float) -> tuple[float, float]:
    """Ячейка ERA5: регулярная сетка 0,25°, ближайшая точка (как в v9)."""
    return round(latitude * 4) / 4, round(longitude * 4) / 4


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def wgs84_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Геодезическое расстояние на эллипсоиде WGS84 (обратная формула Винсента), метры.

    Нужно для диагностики «насколько далеко отель от ячейки». Совпадает с `pyproj.Geod`
    v9, но не тянет pyproj в окружение приложения.
    """
    if (lat1, lon1) == (lat2, lon2):
        return 0.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    big_l = math.radians(lon2 - lon1)
    u1 = math.atan((1 - WGS84_F) * math.tan(phi1))
    u2 = math.atan((1 - WGS84_F) * math.tan(phi2))
    sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
    sin_u2, cos_u2 = math.sin(u2), math.cos(u2)
    lam = big_l
    for _ in range(200):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(cos_u2 * sin_lam, cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam)
        if sin_sigma == 0:
            return 0.0
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos2_alpha = 1 - sin_alpha**2
        cos2_sigma_m = cos_sigma - 2 * sin_u1 * sin_u2 / cos2_alpha if cos2_alpha else 0.0
        c = WGS84_F / 16 * cos2_alpha * (4 + WGS84_F * (4 - 3 * cos2_alpha))
        lam_prev = lam
        lam = big_l + (1 - c) * WGS84_F * sin_alpha * (
            sigma + c * sin_sigma * (cos2_sigma_m + c * cos_sigma * (-1 + 2 * cos2_sigma_m**2))
        )
        if abs(lam - lam_prev) < 1e-12:
            break
    else:  # почти диаметральные точки: для отелей недостижимо, но пусть будет честно
        radius = 6371008.8
        dphi = phi2 - phi1
        dlmb = big_l
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
        return 2 * radius * math.asin(min(1.0, math.sqrt(a)))
    u_sq = cos2_alpha * (WGS84_A**2 - WGS84_B**2) / WGS84_B**2
    a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    delta_sigma = (
        b
        * sin_sigma
        * (
            cos2_sigma_m
            + b
            / 4
            * (
                cos_sigma * (-1 + 2 * cos2_sigma_m**2)
                - b / 6 * cos2_sigma_m * (-3 + 4 * sin_sigma**2) * (-3 + 4 * cos2_sigma_m**2)
            )
        )
    )
    return WGS84_B * a * (sigma - delta_sigma)


def load_cache(
    directory: Path, city_id: str
) -> tuple[dict[tuple[float, float], list[ClimateDay]], dict[tuple[float, float], list[dict]]]:
    """Читает кеш Open-Meteo: (суточные ряды по ячейкам, происхождение по ячейкам).

    Файл без `meta.json` или с `http_status != 200` не считается данными: обрезанный ответ
    не должен попасть в 30-летнее среднее.
    """
    records: dict[tuple[float, float], list[ClimateDay]] = defaultdict(list)
    provenance: dict[tuple[float, float], list[dict]] = defaultdict(list)
    for path in sorted(directory.glob(f"era5_{city_id}_*.json")):
        if path.name.endswith(".meta.json"):
            continue
        meta_path = path.with_suffix(".meta.json")
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text("utf-8"))
        if meta.get("http_status") != 200:
            continue
        payload = json.loads(path.read_text("utf-8"))
        cells = payload if isinstance(payload, list) else [payload]
        for index, cell_data in enumerate(cells):
            daily = cell_data["daily"]
            key = (float(cell_data["latitude"]), float(cell_data["longitude"]))
            for i, date in enumerate(daily["time"]):
                records[key].append(
                    ClimateDay(
                        date=date,
                        mean=daily["temperature_2m_mean"][i],
                        maximum=daily["temperature_2m_max"][i],
                        rain=daily["precipitation_sum"][i],
                    )
                )
            params = meta.get("params", {})
            provenance[key].append(
                {
                    "provider": "Open-Meteo Historical Weather API",
                    "url": meta.get("url"),
                    "dataset_sha256": meta.get("sha256"),
                    "dataset_version": "ERA5 via Open-Meteo API; upstream release ID not exposed",
                    "license": "CC BY 4.0",
                    "grid_elevation_m": cell_data.get("elevation"),
                    "grid_latitude": key[0],
                    "grid_longitude": key[1],
                    "start_date": params.get("start_date"),
                    "fetched_at": meta.get("requested_at"),
                    "snapshot_file": path.name,
                    "json_pointer": f"/{index}" if isinstance(payload, list) else "",
                }
            )
    return records, provenance


def climate_aggregate(
    days: Sequence[ClimateDay], feature_id: str, month: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Среднее по 30 годам. Возвращает (значение, диагностика неизвестности).

    Полнота строгая: месяц каждого года должен содержать все свои дни с валидными
    значениями, иначе результат — `None` (не «ноль» и не «среднее по тому, что было»).
    """
    field = FIELD[feature_id]
    annual: list[float] = []
    valid_days = 0
    invalid: list[dict[str, Any]] = []
    for year in YEARS:
        prefix = f"{year}-{month:02}-"
        expected = _month_length(year, month)
        unique = {d.date: d for d in days if d.date.startswith(prefix)}
        values = [getattr(d, field) for d in unique.values() if _finite(getattr(d, field))]
        if len(unique) != expected or len(values) != expected:
            invalid.append({"year": year, "expected": expected, "valid": len(values)})
            continue
        valid_days += len(values)
        if feature_id == "C01":
            annual.append(statistics.mean(float(v) for v in values))
        elif feature_id == "C02":
            annual.append(float(sum(1 for v in values if float(v) >= 1)))
        else:
            annual.append(float(sum(1 for v in values if float(v) > 30)))
    if invalid or len(annual) != len(YEARS):
        return None, {
            "missing_years": invalid,
            "valid_year_count": len(annual),
            "valid_days": valid_days,
        }
    return {
        "mean": statistics.mean(annual),
        "stddev_across_years": statistics.pstdev(annual),
        "minimum_annual_value": min(annual),
        "maximum_annual_value": max(annual),
        "n_years": len(YEARS),
        "valid_days": valid_days,
        "annual_values": dict(zip((str(y) for y in YEARS), annual, strict=True)),
    }, None


def _month_length(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _params(
    feature_id: str,
    month: int,
    cell: tuple[float, float],
    subject: Subject,
    timezone: str,
    elevation: float | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "month": month,
        **PERIOD,
        "model": "ERA5",
        "grid_latitude": cell[0],
        "grid_longitude": cell[1],
        "grid_resolution_deg": 0.25,
        "hotel_to_grid_m": wgs84_distance_m(
            subject.latitude or 0.0, subject.longitude or 0.0, cell[0], cell[1]
        ),
        "model_grid_elevation_m": elevation,
        "timezone": timezone,
        "daily_boundary_policy": "fixed local standard offset, no historicalDST",
        "temperature_downscaling": False,
        "trip_start": TRIP_START[month],
    }
    if feature_id == "C02":
        params.update({"threshold_mm_per_day": 1, "comparison": ">="})
    if feature_id == "C03":
        params.update({"threshold_C": 30, "comparison": ">"})
    return params


API_ENTITLEMENT = "D22 non-commercial hackathon; production Professional/self-host required"


def _sources(provenance: Sequence[dict], month: int) -> list[Source]:
    """Источники этого месяца: по снимку на каждый год (как в v9)."""
    sources = []
    for p in provenance:
        start = p.get("start_date") or ""
        if f"-{month:02}-" not in start:
            continue
        sources.append(
            Source(
                provider=p["provider"],
                url=p.get("url"),
                license=p.get("license"),
                dataset_version=p.get("dataset_version"),
                dataset_sha256=p.get("dataset_sha256"),
                **{
                    k: p[k]
                    for k in (
                        "grid_latitude",
                        "grid_longitude",
                        "grid_elevation_m",
                        "start_date",
                        "fetched_at",
                        "snapshot_file",
                        "json_pointer",
                    )
                    if p.get(k) is not None
                },
                model="ERA5",
                resolution_degrees=0.25,
                api_entitlement=API_ENTITLEMENT,
            )
        )
    return sources


def cache_directory(ctx: CityContext) -> Path:
    """Где лежит кеш ERA5: явный параметр, затем `<data>/era5`, затем сам `<data>`."""
    explicit = ctx.params.get("era5_dir")
    if explicit:
        return Path(explicit)
    nested = ctx.data_dir / "era5"
    if nested.is_dir():
        return nested
    return ctx.data_dir


@register_calculator
class ClimateMonthly:
    """C01–C03 для отелей города: июль и ноябрь, ERA5 1991–2020."""

    id = "env.climate_monthly"
    version = "C01C02C03-era5-openmeteo-1991-2020@1"
    feature_ids = ("C01", "C02", "C03")

    def run(self, subjects: Sequence[Subject], ctx: CityContext) -> list[Fact]:
        era5_dir = cache_directory(ctx)
        records, provenance = load_cache(era5_dir, ctx.city_id)
        offset = ctx.params.get("utc_offset_hours", UTC_OFFSET.get(ctx.city_id))
        timezone = f"Etc/GMT-{offset}" if offset is not None else None
        # Код причины — без пути (путь с машины разработчика не идёт в пакет); где искали —
        # в recipe_attempts, в виде пути от папки проекта.
        cache_reason = None if records else "era5_cache_missing"
        cache_note = f"era5_cache_missing: {repo_relative(str(era5_dir))}"
        facts: list[Fact] = []
        for s in subjects:
            for feature_id in self.feature_ids:
                for month in MONTHS:
                    facts.append(
                        self._fact(
                            s,
                            ctx,
                            feature_id,
                            month,
                            records,
                            provenance,
                            timezone,
                            offset,
                            cache_reason,
                            cache_note,
                        )
                    )
        return facts

    def _fact(
        self,
        subject: Subject,
        ctx: CityContext,
        feature_id: str,
        month: int,
        records: dict[tuple[float, float], list[ClimateDay]],
        provenance: dict[tuple[float, float], list[dict]],
        timezone: str | None,
        offset: int | None,
        cache_reason: str | None,
        cache_note: str | None = None,
    ) -> Fact:
        attempts = [
            RecipeAttempt(
                recipe_id=f"{feature_id}-R1",
                data_kind="Б",
                state="rejected",
                reason=(
                    "D22: прямой Copernicus CDS — только контроль; выбран разрешённый путь "
                    "Open-Meteo как первичный источник климата."
                ),
            )
        ]
        if subject.latitude is None or subject.longitude is None:
            reason = "hotel_coordinates_missing"
            cell = None
            params: dict[str, Any] = {"month": month, **PERIOD, "model": "ERA5"}
        else:
            cell = climate_cell(subject.latitude, subject.longitude)
            params = _params(feature_id, month, cell, subject, timezone or "unknown", None)
            reason = None
        if cell is None:
            pass
        elif timezone is None or offset is None:
            reason = f"city_utc_offset_unknown:{ctx.city_id}"
        elif cache_reason is not None:
            reason = cache_reason
        else:
            params["model_grid_elevation_m"] = (provenance.get(cell) or [{}])[0].get(
                "grid_elevation_m"
            )

        value = error = None
        if reason is None and cell is not None:
            if cell not in records:
                # Ячейки нет в кеше вовсе — это не «неполный ряд», и текст причины другой.
                reason = "era5_cell_missing"
            else:
                value, error = climate_aggregate(records[cell], feature_id, month)
                if value is None:
                    reason = "incomplete_authorized_1991_2020_month_series"
        if value is not None:
            attempts.append(
                RecipeAttempt(recipe_id=f"{feature_id}-R2", data_kind="Б", state="accepted")
            )
            return Fact(
                fact_id=make_fact_id(
                    subject.hotel_id, subject.subject_id, feature_id, params, self.version
                ),
                hotel_id=subject.hotel_id,
                city_id=ctx.city_id,
                subject_level="hotel",
                subject_id=subject.subject_id,
                feature_id=feature_id,
                params=params,
                value=value,
                unit=UNITS[feature_id],
                status=Status.computed,
                sources=_sources(provenance.get(cell, []), month) if cell else [],
                recipe_id=f"{feature_id}-R2",
                algorithm_version=self.version,
                confidence=Confidence(**CONFIDENCE),
                calculated_at=datetime.now(UTC),
                refresh_after=REFRESH_AFTER,
                scope_note=SCOPE_NOTE,
                recipe_attempts=attempts,
            )
        attempts.append(
            RecipeAttempt(
                recipe_id=f"{feature_id}-R2",
                data_kind="Б",
                state="failed",
                reason=cache_note if reason == cache_reason and cache_note else reason,
            )
        )
        return Fact(
            fact_id=make_fact_id(
                subject.hotel_id, subject.subject_id, feature_id, params, self.version
            ),
            hotel_id=subject.hotel_id,
            city_id=ctx.city_id,
            subject_level="hotel",
            subject_id=subject.subject_id,
            feature_id=feature_id,
            params=params,
            value=None,
            unit=UNITS[feature_id],
            status=Status.unknown,
            unknown_reason=reason,
            sources=_sources(provenance.get(cell, []), month) if cell else [],
            algorithm_version=self.version,
            calculated_at=datetime.now(UTC),
            recipe_attempts=attempts,
            components=error,
        )
