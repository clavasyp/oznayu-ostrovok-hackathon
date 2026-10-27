"""Очередь D33: сомнительная связь не становится фактом или названием корпуса."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from agents.address.normalize import Address, parse_address
from core.packs import iter_jsonl, sha256_file, stable_id


def items(hotels: list[dict], links: list[dict], report: dict, dataset_sha: str) -> list[dict]:
    matched = {link["hotel_id"] for link in links}
    proposed = {}
    for row in report["review_candidates"]:
        target = proposed.setdefault(
            row["hotel_id"], {"candidate_building_ids": set(), "evidence": []}
        )
        target["candidate_building_ids"].update(row.get("candidate_building_ids", []))
        target["evidence"].append(row)
    for row in proposed.values():
        row["candidate_building_ids"] = sorted(row["candidate_building_ids"])
        row["evidence"].sort(key=lambda r: json.dumps(r, sort_keys=True))
    result = []
    for h in sorted(hotels, key=lambda h: h["id"]):
        if h["id"] in matched:
            continue
        row = proposed.get(h["id"], {})
        bids = row.get("candidate_building_ids", [])
        proposal = {
            "hotel_id": h["id"],
            "building_id": bids[0] if len(bids) == 1 else None,
            "candidate_building_ids": bids,
            "reason": "address_or_geometry_requires_human_review"
            if bids
            else "no_reliable_full_address_footprint_candidate",
            "automatic_acceptance": False,
        }
        evidence = [
            {"kind": "hotel_address", "value": h.get("address"), "source": h.get("source", {})},
            {
                "kind": "osm_candidate_addresses",
                "values": row.get("evidence", {}),
                "dataset_sha256": dataset_sha,
            },
        ]
        identity = hashlib.sha256(
            json.dumps([proposal, evidence], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        result.append(
            {
                "id": stable_id("seva-building-review.1", h["city_id"], h["id"], identity),
                "city_id": h["city_id"],
                "kind": "match_hotel_building",
                "subject_level": "hotel",
                "subject_id": h["id"],
                "feature_id": None,
                "proposal": proposal,
                "evidence": evidence,
                "proposed_by": "script:seva-building-review.1",
                "state": "proposed",
                "estimated_review_minutes": 3.0 if bids else 5.0,
                "note": "Проверить адрес и геометрию; не факт curated и не название корпуса отеля.",
            }
        )
    return result


DUPLICATE_VERSION = "seva-duplicate-listing.1"
DUPLICATE_RADIUS_M = 30.0


def _name_key(name: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (name or "").casefold().replace("ё", "е")).split())


def _same_address(a: Address, b: Address) -> bool:
    """Дом совпадает, улицы совместимы: равны, одна не разобрана или одна — часть другой
    («3 Мякининская» и «3 дом Мякининская»). Квартиры одного оператора с общей точкой на
    карте, но разными домами, кандидатами не становятся."""
    if not a.house_key or a.house_key != b.house_key:
        return False
    if not a.street_key or not b.street_key:
        return True
    left, right = set(a.street_key.split()), set(b.street_key.split())
    return left <= right or right <= left


def _distance_m(a: dict, b: dict) -> float:
    lat1, lat2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    dlon = math.radians(b["longitude"] - a["longitude"])
    value = (
        math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 6371008.8 * 2 * math.asin(min(1.0, math.sqrt(value)))


def duplicate_listings(hotels: list[dict], room_types: list[dict]) -> list[dict]:
    """Вероятные дубли одного объекта под разными `masterId` → кандидаты D33 `duplicate_listing`.

    Не схлопываются: каждый `masterId` — отдельное предложение Островка. Кандидат — одинаковое
    нормализованное название, не дальше 30 м и тот же дом в адресе. После проверки человеком —
    журнал слияний (D17); основной карточкой предлагается та, где больше категорий номеров."""
    rooms: dict[str, int] = {}
    for room in room_types:
        rooms[room["hotel_id"]] = rooms.get(room["hotel_id"], 0) + 1
    by_name: dict[str, list[dict]] = {}
    for hotel in sorted(hotels, key=lambda h: h["id"]):
        if key := _name_key(hotel.get("name")):
            by_name.setdefault(key, []).append(hotel)
    result = []
    for group in by_name.values():
        parsed = {h["id"]: parse_address(h.get("address") or "") for h in group}
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                distance = _distance_m(a, b)
                if distance > DUPLICATE_RADIUS_M or not _same_address(
                    parsed[a["id"]], parsed[b["id"]]
                ):
                    continue
                pair = sorted([a, b], key=lambda h: (-rooms.get(h["id"], 0), h["id"]))
                proposal = {
                    "hotel_ids": sorted([a["id"], b["id"]]),
                    "primary_hotel_id": pair[0]["id"],
                    "action": "mark_duplicate_listing",
                    "merge_automatically": False,
                    "automatic_acceptance": False,
                }
                evidence = [
                    {
                        "kind": "ostrovok_listing",
                        "hotel_id": h["id"],
                        "name": h.get("name"),
                        "address": h.get("address"),
                        "latitude": h["latitude"],
                        "longitude": h["longitude"],
                        "room_types": rooms.get(h["id"], 0),
                        "url": (h.get("source") or {}).get("url"),
                    }
                    for h in sorted([a, b], key=lambda h: h["id"])
                ] + [
                    {
                        "kind": "rule",
                        "rule": DUPLICATE_VERSION,
                        "distance_m": round(distance, 1),
                        "radius_m": DUPLICATE_RADIUS_M,
                        "name_key": _name_key(a.get("name")),
                        "house_key": parsed[a["id"]].house_key,
                    }
                ]
                result.append(
                    {
                        "id": stable_id(DUPLICATE_VERSION, a["city_id"], *proposal["hotel_ids"]),
                        "city_id": a["city_id"],
                        "kind": "duplicate_listing",
                        "subject_level": "hotel",
                        "subject_id": proposal["hotel_ids"][0],
                        "feature_id": None,
                        "proposal": proposal,
                        "evidence": evidence,
                        "proposed_by": f"script:{DUPLICATE_VERSION}",
                        "state": "proposed",
                        "estimated_review_minutes": 2.0,
                        "note": (
                            "Похоже на одну карточку объекта дважды. Не схлопывать автоматически: "
                            "проверить страницы; подтверждённую пару — в журнал слияний (D17)."
                        ),
                    }
                )
    return sorted(result, key=lambda r: r["id"])


def import_v11(path: Path, expected_sha256: str, hotels: set[str], city: str) -> list[dict]:
    """Вход только канонический D33 JSONL, не свободный отчёт с выводами ИИ.

    Хеш обязателен. Даже строка с accepted импортируется как proposed: решения людей
    принимаются в журнале проекта, статус внешнего файла их не заменяет.
    """
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ValueError("v11 candidate SHA256 mismatch")
    result = []
    for number, row in enumerate(iter_jsonl(path), 1):
        if row.get("city_id") != city or str(row.get("subject_id")) not in hotels:
            raise ValueError("v11 candidate references a hotel outside this city catalog")
        if (
            row.get("kind") != "match_hotel_building"
            or not isinstance(row.get("evidence"), list)
            or not row["evidence"]
            or not isinstance(row.get("proposal"), dict)
        ):
            raise ValueError("v11 building candidate requires canonical proposal and evidence")
        result.append(
            {
                "id": stable_id("v11-building-candidate", actual, str(number)),
                "city_id": city,
                "kind": "match_hotel_building",
                "subject_level": "hotel",
                "subject_id": str(row["subject_id"]),
                "feature_id": None,
                "proposal": row["proposal"],
                "evidence": row["evidence"]
                + [
                    {
                        "kind": "import",
                        "file_sha256": actual,
                        "record_index": number - 1,
                        "json_pointer": "/proposal",
                    }
                ],
                "proposed_by": "codex:v11_import",
                "state": "proposed",
                "estimated_review_minutes": 5.0,
                "note": "Внешний кандидат v11, требуется проверка человеком.",
            }
        )
    return result
