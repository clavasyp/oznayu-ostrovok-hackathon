"""Общий сборщик: v9 + полный каталог Островка, географический отбор и покрытие.

Результат создаётся в отдельном каталоге; штатные packs/ не заменяются до этапа 10.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from connectors.city_geography import SUBZONE_NAMES, CityGeography
from connectors.ostrovok_site import HotelRecord, read_snapshot
from core.facts import Fact
from core.packs import (
    Manifest,
    PackFile,
    PackSource,
    coverage_rows,
    iter_jsonl,
    sha256_file,
    verify,
)
from pipeline.build_v9_packs import CITY_NAMES, PACKS, ROOT, write_jsonl_gz
from pipeline.hotelier_questions import build_questions

# .2: subzone — название для гостя, код — subzone_id; .3: вопросы отельеру всем отелям по
# шаблонам, ссылка v9 без `mid` — по masterId с пометкой «не проверена»
VERSION = "city-catalog.3"
MID_URL = re.compile(r"/mid\d+/")
CITY_PAGE_URL = re.compile(r"^https://ostrovok\.ru/hotel/[^/]+/[^/]+/$")
DERIVED_URL = "derived_from_masterId_unverified"
FOCUS = ("moscow", "sochi")


def new_output(out: Path, *inputs: Path) -> Path:
    out = out.resolve()
    if out.is_relative_to(ROOT.resolve()) or ROOT.resolve().is_relative_to(out):
        raise ValueError("Создавайте результат вне репозитория; штатные packs/ защищены")
    for source in inputs:
        source = source.resolve()
        if out.is_relative_to(source) or source.is_relative_to(out):
            raise ValueError("Выход пересекается с входным каталогом")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("Результату нужен новый или пустой каталог")
    return out


def pack_parts(pack: Path) -> dict[str, list[dict]]:
    """Строки пакета по видам. Файлы-слои (`"layer": true`, вид и солнце) — отдельная сборка:
    в общий `facts` не вливаются, их переносит публикация (`keep_layers`)."""
    manifest = verify(pack)
    parts = defaultdict(list)
    for entry in manifest.files:
        if entry.kind != "other" and not entry.layer:
            parts[entry.kind].extend(iter_jsonl(pack / entry.path))
    return dict(parts)


def derived_page_url(hotel_id: str, source: dict) -> dict:
    """Ссылка v9 без `mid` ведёт на страницу города, а не отеля. Если страницы отеля нет ни в
    одном снимке, адрес собирается из masterId и `ota_slug` v9 — с пометкой «не проверен»."""
    url, slug = source.get("url") or "", source.get("ota_slug")
    if MID_URL.search(url) or not CITY_PAGE_URL.match(url) or not slug:
        return source
    return source | {
        "url": f"{url}mid{hotel_id}/{slug}/",
        "url_previous": url,
        "url_method": DERIVED_URL,
    }


def coverage(hotels: list[dict], facts: list[dict], templates: list[dict]) -> list[dict]:
    """Покрытие тех же 54 вычисляемых ID v9, новый знаменатель — весь принятый город."""
    return coverage_rows((h["id"] for h in hotels), facts, templates)


def validate_parts(parts: dict[str, list[dict]]) -> None:
    hotels = {h["id"] for h in parts["hotels"]}
    rooms = {r["id"] for r in parts.get("room_types", [])}
    if len(hotels) != len(parts["hotels"]) or len(rooms) != len(parts.get("room_types", [])):
        raise ValueError("Повтор ID отеля или категории в пакете")
    for room in parts.get("room_types", []):
        if room["hotel_id"] not in hotels or room["id"] != f"{room['hotel_id']}:{room['rg_hash']}":
            raise ValueError("Категория с неправильной ссылкой/ID")
    seen = set()
    for raw in parts["facts"]:
        fact = Fact.model_validate(raw)
        if fact.fact_id in seen or fact.hotel_id not in hotels:
            raise ValueError("Повтор факта или факт без отеля")
        if fact.subject_level == "room_type" and fact.subject_id not in rooms:
            raise ValueError("Факт без категории")
        seen.add(fact.fact_id)
    buildings = {r["id"] for r in parts.get("buildings", [])}
    places = [r["id"] for r in parts.get("places", [])]
    stations = {r["id"] for r in parts.get("transit_stations", [])}
    if len(set(places)) != len(places):
        raise ValueError("Повтор ID места в пакете")
    keys = set()
    for link in parts.get("hotel_buildings", []):
        if link["hotel_id"] not in hotels or link["building_id"] not in buildings:
            raise ValueError("Связь корпуса без отеля/здания")
    for f in parts.get("facades", []):
        key = (f["building_id"], f["facade_id"])
        if key in keys or f["building_id"] not in buildings:
            raise ValueError("Повтор фасада или фасад без здания")
        if not 0 <= f["azimuth_deg"] < 360 or f["side"] not in {
            "N",
            "NE",
            "E",
            "SE",
            "S",
            "SW",
            "W",
            "NW",
        }:
            raise ValueError("Некорректная ориентация фасада")
        keys.add(key)
    for row in parts.get("places", []):
        if not row.get("categories"):  # D41: место без категории в пакет не идёт
            raise ValueError("Место без категории словаря или каталога: " + row["id"])
    for kind, foreign_key, parent in (("hotel_stations", "station_id", stations),):
        seen_links = set()
        for row in parts.get(kind, []):
            key = (row["hotel_id"], row[foreign_key])
            if key in seen_links or row["hotel_id"] not in hotels or row[foreign_key] not in parent:
                raise ValueError("Повтор/потерянная ссылка в " + kind)
            if not 0 <= row["distance_m"] < float("inf"):
                raise ValueError("Некорректное расстояние в " + kind)
            seen_links.add(key)
    for row in parts.get("transit_times", []):
        departure = datetime.fromisoformat(row["departure_at"])
        if departure.tzinfo is None or row["duration_s"] < 0 or row["transfers"] < 0:
            raise ValueError("Время транспорта без временного контекста/корректной метрики")
        if row["source_station_id"] not in stations or row["target_station_id"] not in stations:
            raise ValueError("Время транспорта без станции")
    candidate_ids = set()
    for row in parts.get("curation_items", []):
        if row["id"] in candidate_ids or row["subject_id"] not in hotels:
            raise ValueError("Повтор кандидата или кандидат без отеля")
        if row["state"] != "proposed" or row.get("fact_id") or row.get("reviewer"):
            raise ValueError("Пакет кандидатов не заменяет решение человека")
        candidate_ids.add(row["id"])


def write_pack(
    out: Path,
    city: str,
    parts: dict[str, list[dict]],
    *,
    sources: list[PackSource],
    built_at: datetime,
    code_version: str,
    notes: str,
) -> Manifest:
    validate_parts(parts)
    folder = out / city
    (folder / "data").mkdir(parents=True, exist_ok=True)
    files = []
    for kind, records in sorted(parts.items()):
        if not records and kind != "transit_times":
            continue
        sort_fields = {
            "facades": ("building_id", "facade_id"),
            "hotel_buildings": ("hotel_id", "building_id"),
            "hotel_stations": ("hotel_id", "station_id"),
            "facts": ("fact_id",),
            "coverage": ("feature_id",),
        }.get(kind, ("id",))
        rows = sorted(records, key=lambda r: tuple(str(r.get(k, "")) for k in sort_fields))
        # 2ГИС — отдельные файлы даже при совпадении схемы с OSM (D37): и `source` строк,
        # и любой паспорт в `sources` факта.
        groups = defaultdict(list)
        for row in rows:
            passports = [row.get("source") or {}] + list(row.get("sources") or [])
            gis = any(isinstance(p, dict) and p.get("provider") == "2gis" for p in passports)
            groups["_2gis" if gis else ""].append(row)
        if not rows:
            groups[""] = []
        for suffix, grouped in sorted(groups.items()):
            for offset in range(0, max(1, len(grouped)), 250000):
                chunk = grouped[offset : offset + 250000]
                part = f"-{offset // 250000:04d}" if len(grouped) > 250000 else ""
                path = folder / "data" / f"{kind}{suffix}{part}.jsonl.gz"
                write_jsonl_gz(path, chunk)
                files.append(
                    PackFile(
                        path=f"data/{path.name}",
                        kind=kind,
                        rows=len(chunk),
                        bytes=path.stat().st_size,
                        sha256=sha256_file(path),
                    )
                )
    identity = code_version + "".join(f.sha256 for f in files)
    manifest = Manifest(
        city_id=city,
        city_name=CITY_NAMES[city],
        timezone="Europe/Moscow",
        depth="full",
        pack_version=f"{code_version}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}",
        built_at=built_at,
        code_version=code_version,
        catalog_version="ostrovok-2026-10-24+v9",
        sources=sources,
        files=files,
        notes=notes,
    )
    (folder / "manifest.json").write_text(
        manifest.model_dump_json(indent=2, exclude_none=True) + "\n", "utf-8"
    )
    verify(folder)
    return manifest


def build_catalog(snapshot: Path, osm_root: Path, out: Path, *, v9_packs: Path = PACKS) -> dict:
    out = new_output(out, snapshot, osm_root, v9_packs)
    for city in FOCUS:  # в packs/ Москва и Сочи — уже полный каталог, не v9
        code = verify(v9_packs / city).code_version or ""
        if code.startswith(("seva-full", "category-supplement", VERSION.split(".")[0])):
            raise ValueError(
                f"{v9_packs / city}: пакет {code}, а нужен v9 — "
                "python -m pipeline.build_v9_packs --out /tmp/v9, затем --v9-packs /tmp/v9"
            )
    geography = CityGeography(osm_root)
    accepted = {city: {"hotels": {}, "room_types": [], "facts": []} for city in FOCUS}
    excluded = []
    report = {"algorithm_version": VERSION, "input_pages": {}, "cities": {}, "excluded": []}
    for market in FOCUS:
        catalog = read_snapshot(snapshot, market, limit=None)
        report["input_pages"][market] = len(catalog.hotels)
        excluded.extend(catalog.excluded)
        by_city = {}
        for hotel in catalog.hotels:
            city = geography.city_for(hotel.longitude, hotel.latitude)
            if not city:
                excluded.append(
                    {
                        "hotel_id": hotel.id,
                        "url": hotel.source["url"],
                        "reason": "outside_focus_boundaries",
                        "market": market,
                    }
                )
                continue
            if hotel.id in accepted[city]["hotels"]:
                excluded.append(
                    {"hotel_id": hotel.id, "reason": "duplicate_masterId", "market": market}
                )
                continue
            zone, zone_name = (
                geography.subzone_for(hotel.longitude, hotel.latitude)
                if city == "sochi"
                else (None, None)
            )
            hotel = hotel.model_copy(
                update={
                    "city_id": city,
                    "subzone": SUBZONE_NAMES[zone] if zone else None,
                    "subzone_id": zone,
                    "source": hotel.source
                    | {
                        "geography_verified": True,
                        "source_market": market,
                        "geography": geography.sources[city],
                        "subzone_osm_name": zone_name,
                        "subzone_method": "named_osm_polygon"
                        if zone_name
                        else "other_or_not_applicable",
                    },
                }
            )
            accepted[city]["hotels"][hotel.id] = hotel.model_dump()
            by_city[hotel.id] = city
        for room in catalog.room_types:
            if room.hotel_id in by_city:
                accepted[by_city[room.hotel_id]]["room_types"].append(room.model_dump())
        for fact in catalog.facts:
            if fact.hotel_id in by_city:
                raw = fact.model_dump(mode="json", exclude_none=True)
                raw["city_id"] = by_city[fact.hotel_id]
                accepted[by_city[fact.hotel_id]]["facts"].append(raw)
        print(f"{market}: прочитано {len(catalog.hotels)} отелей снимка", flush=True)
    for city in FOCUS:
        base = pack_parts(v9_packs / city)
        target = accepted[city]
        catalog_count = len(target["hotels"])
        legacy_only = []
        for old in base["hotels"]:
            if old["id"] in target["hotels"]:
                continue
            if geography.city_for(old["longitude"], old["latitude"]) != city:
                excluded.append(
                    {"hotel_id": old["id"], "reason": "v9_outside_focus_boundary", "city": city}
                )
                continue
            h = HotelRecord.model_validate(old).model_dump()
            if city == "sochi":
                h["subzone_id"], _ = geography.subzone_for(h["longitude"], h["latitude"])
                h["subzone"] = SUBZONE_NAMES[h["subzone_id"]]
            h["source"] = derived_page_url(h["id"], h["source"]) | {
                "legacy_v9_only": True,
                "geography_verified": True,
                "geography": geography.sources[city],
            }
            target["hotels"][h["id"]] = h
            legacy_only.append(h["id"])
        ids = set(target["hotels"])
        parts = {
            "hotels": list(target["hotels"].values()),
            "room_types": target["room_types"],
            "facts": [f for f in base["facts"] if f["hotel_id"] in ids] + target["facts"],
            "ota_comparisons": [r for r in base.get("ota_comparisons", []) if r["hotel_id"] in ids],
        }
        # Вопросы когорты v9 — как есть (предзаполнение дополняется заявлениями Островка),
        # остальным отелям — по шаблонам: иначе цикл отельера для них отвечает 404.
        parts["questions"], question_stats = build_questions(
            parts["hotels"],
            parts["facts"],
            [q for q in base.get("questions", []) if q["hotel_id"] in ids],
        )
        parts["coverage"] = coverage(parts["hotels"], parts["facts"], base["coverage"])
        sources = verify(v9_packs / city).sources + [
            PackSource(
                id="ostrovok_site",
                provider="ostrovok.ru",
                release=snapshot.name,
                sha256=sha256_file(snapshot / "manifests/pages.jsonl"),
            ),
            PackSource(
                id="city_boundary",
                provider="OpenStreetMap/Geofabrik",
                license="ODbL-1.0",
                sha256=geography.sources[city]["boundary_sha256"],
                release=geography.sources[city]["source_date"],
            ),
        ]
        dates = [
            datetime.fromisoformat(h["source"]["fetched_at"])
            for h in parts["hotels"]
            if h["source"].get("fetched_at")
        ]
        manifest = write_pack(
            out,
            city,
            parts,
            sources=sources,
            built_at=max(dates),
            code_version=VERSION,
            notes="Полный принятый снимок sitemap + географически принятые отели v9; "
            "подзоны — полигоны OSM (subzone — название, subzone_id — код), "
            "other_greater_sochi («Большой Сочи») — вне именованных подзон. "
            "Сетевые графы/растры не входят. Новый георасчёт — отдельный этап 6.",
        )
        report["cities"][city] = {
            "snapshot_hotels_in_boundary": catalog_count,
            "hotels": len(ids),
            "legacy_v9_only": sorted(legacy_only),
            "room_types": len(parts["room_types"]),
            "facts": len(parts["facts"]),
            "questions": question_stats,
            "subzones": dict(Counter(h.get("subzone_id") for h in parts["hotels"])),
            "pack_version": manifest.pack_version,
        }
    for pack in sorted(v9_packs.glob("*/manifest.json")):
        if pack.parent.name not in FOCUS:
            shutil.copytree(pack.parent, out / pack.parent.name)
    report["excluded"] = excluded
    (out / "catalog_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--osm-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--v9-packs",
        required=True,
        type=Path,
        help="пакеты v9: python -m pipeline.build_v9_packs --out /tmp/v9",
    )
    args = parser.parse_args()
    report = build_catalog(args.snapshot, args.osm_root, args.out, v9_packs=args.v9_packs)
    print(json.dumps(report["cities"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
