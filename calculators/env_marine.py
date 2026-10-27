"""B02 — среднемесячная модельная температура верхнего слоя моря (перенос из v9).

Источник — архив Open-Meteo Marine (`meteofrance_currents`, переменная `thetao`, глубина
0,494 м), 2023–2025, только нативные 6-часовые шаги: часовая сетка API — интерполяция,
а не 24 независимых наблюдения в сутки.

Значение зависит от выбранного пляжа: морская ячейка берётся не у отеля, а у пляжа из
геометрического отбора (`calculators` Севы) — отель → выбранный пляж → ячейка модели.
Поэтому калькулятор читает два входа отбора:

- `marine_targets` — JSONL, по строке на отель: `hotel_id`, `beach_id`, `beach_kind`,
  `marine_classification.accepted`, `selection_*`;
- `marine_mapping` — JSON: `beaches[beach_id]` → ячейка модели + `source_beach_targets_sha256`.

Если отбора ещё нет, факт не выдумывается: `unknown` с причиной `marine_targets_missing`
(путь к файлу — только в `recipe_attempts`, в коде причины его нет). Так расчёт готов к данным,
но не подменяет их догадкой.

`fact_id` считается по параметрам без служебных путей (`core.packs.PROVENANCE_PARAMS`): тот же
отбор из другой папки или на другой машине даёт тот же ID, и он же сходится после
`move_provenance_params` при сборке пакета.

Статус — `estimate` (D18): три года модели, не норма и не термометр на пляже.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import struct
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from calculators.env_climate import TRIP_START, wgs84_distance_m
from core.facts import Confidence, Fact, RecipeAttempt, Source, Status, make_fact_id
from core.packs import PROVENANCE_PARAMS, repo_relative
from core.plugins import CityContext, Subject, register_calculator

MONTHS = (7, 11)
YEARS = (2023, 2024, 2025)
DEPTH_M = 0.49402499198913574
NATIVE_STEP_HOURS = 6
NATIVE_OFFSET_DEG = 1 / 24
REFRESH_AFTER = "2027-10-24T00:00:00+00:00"
UNIT = "°C"
# Внутренние города: моря нет. Озеро или река — другой отбор, а не «море рядом».
COASTAL_CITIES = frozenset({"sochi", "spb", "istanbul", "dubai", "kaliningrad"})
WORLDCOVER_WATER, WORLDCOVER_MISSING = 80, 0
SCOPE_NOTE = (
    "Архивная модельная thetao за 3 года на глубине 0,494 м; не климатическая норма, "
    "не термометр на пляже, не skin-SST и не прогноз на 2026/2027."
)
CONFIDENCE = {
    "level": "low",
    "reason": (
        "D18: короткий 3-летний архив; грубая прибрежная ячейка, "
        "смена версии модели и округление API."
    ),
}
ACCESS_CLAIM = (
    "Публичный доступ и принадлежность пляжа отелю не утверждаются: отбор даёт только "
    "морской контекст."
)


API_ENTITLEMENT = "D22 non-commercial hackathon; production Professional/self-host required"


def float32(value: float) -> float:
    """Округление до float32 — как в импортёре v9 (numpy.float32), но без numpy."""
    return struct.unpack("f", struct.pack("f", value))[0]


def native_cf_coordinate(api_lat: float, api_lon: float) -> dict[str, Any]:
    """Нативная координата центра ячейки CF по точке, которую вернул API.

    API сообщает координату со сдвигом в полшага сетки (1/24°) от нативного центра CF.
    Смешивать эти две опоры нельзя: расстояние до пляжа считается по нативной точке.
    """
    iy = round((api_lat + 80 - NATIVE_OFFSET_DEG) * 12)
    ix = round((api_lon + 180 - NATIVE_OFFSET_DEG) * 12)
    return {
        "native_cf_latitude": float32(-80 + iy / 12),
        "native_cf_longitude": float32(-180 + ix / 12),
        "native_latitude_index": iy,
        "native_longitude_index": ix,
        "api_reported_latitude": api_lat,
        "api_reported_longitude": api_lon,
        "api_to_native_offset_deg": NATIVE_OFFSET_DEG,
    }


def worldcover_class(tiles_dir: Path | None, lat: float, lon: float) -> int | None:
    """Класс WorldCover 2021 в точке — независимая проверка «это вода».

    Нет тайла, нет rasterio, точка за краем растра или пиксель без данных (0) — `None`:
    отсутствие проверки не равно «суша». Вырезка `data/worldcover` названа как полный тайл ESA,
    а за её краем `sample` отдаёт 0 — это не класс, а «нет данных».
    """
    if tiles_dir is None:
        return None
    tile = (
        tiles_dir / f"ESA_WorldCover_10m_2021_v200_N{math.floor(lat / 3) * 3:02}"
        f"E{math.floor(lon / 3) * 3:03}_Map.tif"
    )
    if not tile.is_file():
        return None
    try:
        import rasterio
    except ImportError:
        return None
    with rasterio.open(tile) as ds:
        left, bottom, right, top = ds.bounds
        if not (left <= lon < right and bottom < lat <= top):
            return None
        value = int(next(ds.sample([(lon, lat)]))[0])
    return None if value == WORLDCOVER_MISSING else value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def marine_cache_directory(ctx: CityContext) -> Path:
    explicit = ctx.params.get("marine_cache")
    if explicit:
        return Path(explicit)
    nested = ctx.data_dir / "marine" / "raw"
    if nested.is_dir():
        return nested
    return ctx.data_dir


def _cells_files(cache_dir: Path, city_id: str, year: int, month: int) -> list[Path]:
    pattern = f"marine_{city_id}_{year}{month:02}_cells_*.json"
    return [p for p in sorted(cache_dir.glob(pattern)) if not p.name.endswith(".meta.json")]


def monthly_mean(
    cache_dir: Path, city_id: str, cell: tuple[float, float], month: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """Среднемесячная температура по 2023–2025. Возвращает (значение, источники, причина)."""
    annual: list[float] = []
    sources: list[dict[str, Any]] = []
    completeness: dict[str, dict[str, int]] = {}
    for year in YEARS:
        found = False
        for path in _cells_files(cache_dir, city_id, year, month):
            meta_path = path.with_suffix(".meta.json")
            if not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text("utf-8"))
            if meta.get("http_status") != 200:
                continue
            payload = json.loads(path.read_text("utf-8"))
            series = payload if isinstance(payload, list) else [payload]
            for index, point in enumerate(series):
                if (float(point["latitude"]), float(point["longitude"])) != cell:
                    continue
                hourly = point["hourly"]
                # Только нативные 6-часовые шаги: часовая сетка — интерполяция.
                native = [
                    value
                    for stamp, value in zip(
                        hourly["time"], hourly["sea_surface_temperature"], strict=True
                    )
                    if int(stamp[11:13]) % NATIVE_STEP_HOURS == 0
                ]
                expected = _month_length(year, month) * (24 // NATIVE_STEP_HOURS)
                valid = [float(v) for v in native if v is not None and math.isfinite(float(v))]
                completeness[str(year)] = {
                    "valid_native_steps": len(valid),
                    "expected_native_steps": expected,
                }
                if len(valid) != expected:
                    return None, sources, f"incomplete_native_6hour_month:{year}-{month:02}"
                annual.append(statistics.mean(valid))
                sources.append(
                    {
                        "provider": "Open-Meteo Marine API",
                        "url": meta.get("url"),
                        "dataset_sha256": meta.get("sha256"),
                        "dataset_version": "meteofrance_currents (MFSST) via Open-Meteo",
                        "license": "CC BY 4.0",
                        "model_selector": "meteofrance_currents",
                        "underlying_model": (
                            "MERCATOR GLO12; CMEMS GLOBAL_ANALYSISFORECAST_PHY_001_024"
                        ),
                        "variable": "thetao (как sea_surface_temperature)",
                        "depth_m": DEPTH_M,
                        "snapshot_file": path.name,
                        "json_pointer": f"/{index}" if isinstance(payload, list) else "",
                        "fetched_at": meta.get("requested_at"),
                    }
                )
                found = True
                break
            if found:
                break
        if not found:
            return None, sources, f"no_cached_series:{year}-{month:02}"
    return (
        {
            "mean_C": statistics.mean(annual),
            "stddev_across_years_C": statistics.pstdev(annual),
            "annual_monthly_means_C": dict(zip((str(y) for y in YEARS), annual, strict=True)),
            "n_years": len(YEARS),
            "native_6hour_completeness": completeness,
        },
        sources,
        None,
    )


def _month_length(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def load_targets(path: Path) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                record = json.loads(line)
                targets[record["hotel_id"]] = record
    return targets


def _source(entry: dict[str, Any]) -> Source:
    """Провайдер — из записи: пляж приходит из OSM, а не из Open-Meteo (D8, D14).

    Запись не Open-Meteo (пляж из OSM) переносится целиком — версия объекта, дата правки,
    ссылка на лицензию, как в v9 — и без отметки D22: условие тарифа относится к API Open-Meteo,
    а не к OSM."""
    provider = entry.get("provider") or "Open-Meteo Marine API"
    if not provider.startswith("Open-Meteo"):
        return Source(**{**entry, "provider": provider})
    return Source(
        provider=provider,
        url=entry.get("url"),
        license=entry.get("license"),
        dataset_version=entry.get("dataset_version"),
        dataset_sha256=entry.get("dataset_sha256"),
        **{
            k: entry[k]
            for k in (
                "model_selector",
                "underlying_model",
                "variable",
                "depth_m",
                "snapshot_file",
                "json_pointer",
                "fetched_at",
            )
            if entry.get(k) is not None
        },
        api_entitlement=API_ENTITLEMENT,
    )


@register_calculator
class MarineTemperature:
    """B02: температура моря у выбранного пляжа, июль и ноябрь, 2023–2025."""

    id = "env.marine_temperature"
    version = "B02-openmeteo-marine-2023-2025@1"
    feature_ids = ("B02",)

    def run(self, subjects: Sequence[Subject], ctx: CityContext) -> list[Fact]:
        if ctx.city_id not in COASTAL_CITIES:
            return [self._not_applicable(s, ctx, month) for s in subjects for month in MONTHS]

        targets_path = Path(
            ctx.params.get(
                "marine_targets", ctx.data_dir / "marine" / f"{ctx.city_id}_targets.jsonl"
            )
        )
        ctx.params["marine_targets"] = str(targets_path)  # путь попадёт в паспорт факта
        note = repo_relative(str(targets_path))
        mapping_path = Path(
            ctx.params.get(
                "marine_mapping", ctx.data_dir / "marine" / f"marine_mapping_{ctx.city_id}.json"
            )
        )
        cache_dir = marine_cache_directory(ctx)
        tiles_dir = Path(ctx.params.get("worldcover_dir", ctx.data_dir / "worldcover"))
        if not tiles_dir.is_dir():
            tiles_dir = None

        # Код причины — без пути: путь с машины разработчика не идёт в пакет (PackError).
        if not targets_path.is_file():
            return [
                self._unknown(
                    s, ctx, month, "marine_targets_missing", f"marine_targets_missing: {note}"
                )
                for s in subjects
                for month in MONTHS
            ]
        if not mapping_path.is_file():
            mapping_note = repo_relative(str(mapping_path))
            return [
                self._unknown(
                    s,
                    ctx,
                    month,
                    "marine_mapping_missing",
                    f"marine_mapping_missing: {mapping_note}",
                )
                for s in subjects
                for month in MONTHS
            ]
        targets = load_targets(targets_path)
        mapping = json.loads(mapping_path.read_text("utf-8"))
        expected_sha = mapping.get("source_beach_targets_sha256")
        if expected_sha and expected_sha != sha256_file(targets_path):
            # Отбор изменился после расчёта карты ячеек: числа были бы от другого пляжа.
            return [
                self._unknown(s, ctx, month, "marine_targets_changed_since_mapping")
                for s in subjects
                for month in MONTHS
            ]
        facts: list[Fact] = []
        for s in subjects:
            for month in MONTHS:
                facts.append(self._fact(s, ctx, month, targets, mapping, cache_dir, tiles_dir))
        return facts

    def _fact(
        self,
        subject: Subject,
        ctx: CityContext,
        month: int,
        targets: dict[str, dict[str, Any]],
        mapping: dict[str, Any],
        cache_dir: Path,
        tiles_dir: Path | None,
    ) -> Fact:
        target = targets.get(subject.hotel_id)
        if target is None:
            return self._unknown(subject, ctx, month, "hotel_not_in_marine_targets")
        if target.get("beach_kind") != "sea" or not (target.get("marine_classification") or {}).get(
            "accepted"
        ):
            return self._unknown(subject, ctx, month, "selected_target_is_not_accepted_sea_beach")
        beach = (mapping.get("beaches") or {}).get(target["beach_id"])
        if not beach:
            return self._unknown(subject, ctx, month, "beach_not_in_marine_mapping")
        if not beach.get("eligible"):
            return self._unknown(subject, ctx, month, "marine_cell_not_eligible")
        cell = (float(beach["model_latitude"]), float(beach["model_longitude"]))
        value, sources, error = monthly_mean(cache_dir, ctx.city_id, cell, month)
        params = self._params(subject, month, target, beach, cell, tiles_dir)
        targets_path = ctx.params.get("marine_targets")
        if targets_path:
            # Служебное поле: сборщик пакета уносит его из params в evidence_files.
            params["target_source_path"] = repo_relative(str(targets_path))
            params["target_source_sha256"] = sha256_file(Path(targets_path))
        attempts = [
            RecipeAttempt(
                recipe_id="B02-R1",
                data_kind="Б",
                state="rejected",
                reason=(
                    "D22 запрещает прямой Copernicus как первичный источник; R2 — Open-Meteo "
                    "с той же переменной и глубиной за явный период 2023–2025."
                ),
            )
        ]
        if value is None:
            attempts.append(
                RecipeAttempt(recipe_id="B02-R2", data_kind="Б", state="failed", reason=error)
            )
            fact = self._unknown(subject, ctx, month, error or "marine_series_unavailable")
            fact.params = params
            fact.recipe_attempts = attempts
            return fact
        attempts.append(RecipeAttempt(recipe_id="B02-R2", data_kind="Б", state="accepted"))
        # ID — без служебных путей: тот же вход из другой папки — тот же факт.
        id_params = {k: v for k, v in params.items() if k not in PROVENANCE_PARAMS}
        beach_source = beach.get("source")
        return Fact(
            fact_id=make_fact_id(
                subject.hotel_id, subject.subject_id, "B02", id_params, self.version
            ),
            hotel_id=subject.hotel_id,
            city_id=ctx.city_id,
            subject_level="hotel",
            subject_id=subject.subject_id,
            feature_id="B02",
            params=params,
            value=value,
            unit=UNIT,
            status=Status.estimate,
            sources=[_source(s) for s in sources]
            + ([_source(beach_source)] if beach_source else []),
            recipe_id="B02-R2",
            algorithm_version=self.version,
            confidence=Confidence(**CONFIDENCE),
            calculated_at=datetime.now(UTC),
            refresh_after=REFRESH_AFTER,
            scope_note=SCOPE_NOTE,
            recipe_attempts=attempts,
        )

    def _params(
        self,
        subject: Subject,
        month: int,
        target: dict[str, Any],
        beach: dict[str, Any],
        cell: tuple[float, float],
        tiles_dir: Path | None,
    ) -> dict[str, Any]:
        native = native_cf_coordinate(*cell)
        beach_lat = beach.get("requested_beach_latitude")
        beach_lon = beach.get("requested_beach_longitude")
        params: dict[str, Any] = {
            "month": month,
            "period_start": "2023-01-01",
            "period_end": "2025-12-31",
            "depth_m": DEPTH_M,
            "variable": "sea_water_potential_temperature",
            "model": "MERCATOR GLO12",
            "sampling": "native6hourUTCsteps",
            "archive": "Open-MeteoMarine meteofrance_currents (MFSST)",
            "trip_start": TRIP_START[month],
            "beach_id": target["beach_id"],
            "grid_latitude": native["native_cf_latitude"],
            "grid_longitude": native["native_cf_longitude"],
            "api_reported_latitude": cell[0],
            "api_reported_longitude": cell[1],
            "beach_to_api_point_m": beach.get("beach_to_cell_m"),
            "selection_protocol": target.get("selection_protocol"),
            "selection_method": target.get("selection_method"),
            "selection_distance_m": target.get("selection_distance_m"),
            "access_claim": ACCESS_CLAIM,
        }
        if beach_lat is not None and beach_lon is not None:
            params["beach_to_native_cf_m"] = wgs84_distance_m(
                beach_lat, beach_lon, native["native_cf_latitude"], native["native_cf_longitude"]
            )
        klass = worldcover_class(
            tiles_dir, native["native_cf_latitude"], native["native_cf_longitude"]
        )
        where = "в нативной точке"
        if klass is None and _same_native_point(beach, native):
            # Своего тайла нет (или точка за краем вырезки): класс из карты ячеек — та же
            # проверка той же нативной точки, сделанная при расчёте карты (как в v9).
            klass = beach.get("native_coordinate_worldcover2021_class") or None
            where = "в нативной точке (по карте ячеек)"
        if klass is not None:
            params["native_cf_worldcover2021_class"] = klass
            if klass == WORLDCOVER_WATER:
                params["wet_cell_evidence"] = (
                    "Open-Meteo finite native 6h values; независимая проверка — класс 80 "
                    f"WorldCover 2021 {where}."
                )
            else:
                params["wet_cell_evidence"] = (
                    f"Open-Meteo finite native 6h values; WorldCover 2021 {where} "
                    f"дал класс {klass}, а не 80 — независимого подтверждения воды нет."
                )
        # Начальная ячейка модели оказалась сушей и заменена (Дубай, Стамбул) — след замены,
        # как в v9: без него паспорт не говорит, что ячейка не ближайшая к пляжу.
        if beach.get("rejected_initial_cell"):
            params["wet_cell_selection_method"] = beach.get(
                "wet_cell_selection_method", "No validated replacement for initial landpoint"
            )
            params["initial_wet_cell_rejection"] = {
                k: v for k, v in beach["rejected_initial_cell"].items() if k != "probe_source"
            }
        return params

    def _not_applicable(self, subject: Subject, ctx: CityContext, month: int) -> Fact:
        params = {"month": month, "selection": "marine beach"}
        return Fact(
            fact_id=make_fact_id(subject.hotel_id, subject.subject_id, "B02", params, self.version),
            hotel_id=subject.hotel_id,
            city_id=ctx.city_id,
            subject_level="hotel",
            subject_id=subject.subject_id,
            feature_id="B02",
            params=params,
            value=None,
            unit=UNIT,
            status=Status.not_applicable,
            algorithm_version=self.version,
            calculated_at=datetime.now(UTC),
            scope_note="У города нет морского побережья; озеро и река — другой отбор.",
        )

    def _unknown(
        self, subject: Subject, ctx: CityContext, month: int, reason: str, detail: str | None = None
    ) -> Fact:
        """`detail` — подробность для `recipe_attempts` (например, какой файл не найден)."""
        params = {"month": month, "selection": "marine beach"}
        return Fact(
            fact_id=make_fact_id(subject.hotel_id, subject.subject_id, "B02", params, self.version),
            hotel_id=subject.hotel_id,
            city_id=ctx.city_id,
            subject_level="hotel",
            subject_id=subject.subject_id,
            feature_id="B02",
            params=params,
            value=None,
            unit=UNIT,
            status=Status.unknown,
            unknown_reason=reason,
            algorithm_version=self.version,
            calculated_at=datetime.now(UTC),
            recipe_attempts=[
                RecipeAttempt(
                    recipe_id="B02-R2", data_kind="Б", state="failed", reason=detail or reason
                )
            ],
        )


def _same_native_point(beach: dict[str, Any], native: dict[str, Any]) -> bool:
    """Карта ячеек описывает ту же нативную точку, что и наш расчёт."""
    try:
        return (
            abs(float(beach["native_cf_latitude"]) - native["native_cf_latitude"]) < 1e-6
            and abs(float(beach["native_cf_longitude"]) - native["native_cf_longitude"]) < 1e-6
        )
    except (KeyError, TypeError, ValueError):
        return False
