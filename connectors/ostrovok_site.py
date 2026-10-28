"""Офлайн-импорт сохранённого __NEXT_DATA__; без новых запросов к Островку.

Идентичность и происхождение — как в cohort/build_cohort.py v9. Вход адаптирован
к manifests/pages.jsonl снимка 24.10. Правила пилота описаны в connectors/README.md.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from core.facts import Fact, make_fact_id
from core.packs import iter_jsonl, sha256_file
from core.plugins import CityContext, DatasetRef, register_connector

# .2: значения меток — в полях каталога того же смысла (кровати, балкон, кухня, сейф); одна
# запись на категорию и характеристику; явный допуск питомцев; без R01 и M04 (D10, D15).
VERSION = "ostrovok-site.2"
HOTEL_POINTER = "/props/pageProps/hotel"
CityId = Literal["moscow", "sochi"]
# Метка категории → поля каталога того же смысла. Чего метка не доказывает, в значение не идёт:
# patio — терраса, а не балкон; холодильник и микроволновка — не кухня (они только в
# `appliances` при кухне); «есть интернет» — не измеренная скорость (R01 метками не ставится);
# блэкаут-шторы — «есть», а полнота закрытия окон не указана (S05: `present`, не `full`).
# Метки `kettle` на страницах нет, а `tea` чайник не доказывает: M04 метками не ставится.
ROOM_TAGS: dict[str, dict[str, dict[str, Any]]] = {
    "S04": {"air-conditioning": {"hvac_type": "present"}},  # тип и управление неизвестны
    "S05": {"blackout-blinds": {"opaque_coverage": "present"}},
    "M02": {"balcony": {"private_balcony": True}, "patio": {"private_terrace": True}},
    "M03": {"kitchen": {"private_kitchenette": True}, "kitchenette": {"private_kitchenette": True}},
    "M05": {"safe": {"access": "present"}},
}
KITCHEN_APPLIANCES = ("dishwasher", "fridge", "microwave", "oven", "stove")
# Тип кровати из названия категории: только однозначные надписи; прочее — текстом без поля.
BEDDING = {"двуспальная кровать": "double", "2 отдельные кровати": "twin"}
PET_DENIALS = {
    "размещение с домашними животными не допускается",
    "домашние животные не допускаются",
}
# Явный допуск в группе «Животные» — точный список фраз страницы. Вид, масса и число не
# указаны: значение `present` («можно, вид не назван», D39) — «с собакой» остаётся «неизвестно».
PET_ADMISSIONS = {
    "размещение с домашними животными",
    "бесплатное размещение с домашними животными",
    "размещение с домашними животными (меньше 5 кг)",
    "размещение с домашними животными (меньше 5 кг) (за дополнительную плату)",
}
PET_TAG = "pets-allowed"
NOTE_ROOM = "Опубликованный признак категории; полнота вариантов и гарантия неизвестны."
NOTE_NOT_KITCHEN = (
    "Кухни или мини-кухни на странице нет; холодильник и микроволновка кухней не считаются."
)


@dataclass(frozen=True)
class Claim:
    """Утверждение страницы до построения факта: его же сверяет аудит снимка.

    `fields` — пары «JSON pointer → опубликованный текст» всех подтверждающих полей; первое
    задаёт паспорт факта. Без полей — заглушка `unknown` по месту `pointer`."""

    feature_id: str
    pointer: str
    value: dict[str, Any] | None
    fields: tuple[tuple[str, str], ...] = ()
    hard: bool = False
    note: str = NOTE_ROOM
    room_type_ids: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)


def room_claims(base: str, tags: list[str], bedding: str | None) -> list[Claim]:
    """Утверждения одной категории: одна запись на характеристику, как место факта в поиске."""
    claims = []
    for fid, mapping in ROOM_TAGS.items():
        found = [(j, tag) for j, tag in enumerate(tags) if tag in mapping]
        if not found:
            note = NOTE_NOT_KITCHEN if fid == "M03" else NOTE_ROOM
            claims.append(Claim(fid, base, None, note=note))
            continue
        value: dict[str, Any] = {}
        for _, tag in found:
            value |= mapping[tag]
        if fid == "M03":
            value["appliances"] = sorted({t for t in tags if t in KITCHEN_APPLIANCES})
        value["published_room_equipment_tags"] = [tag for _, tag in found]
        fields = tuple((f"{base}/roomAmenityData/{j}", tag) for j, tag in found)
        claims.append(Claim(fid, fields[0][0], value, fields))
    if bedding:
        value = {"published_bedding_text": bedding}
        if bedding.casefold() in BEDDING:
            value["bed_configuration"] = BEDDING[bedding.casefold()]
        pointer = f"{base}/nameStruct/beddingType"
        claims.append(Claim("M01", pointer, value, ((pointer, bedding),)))
    else:
        claims.append(Claim("M01", base, None))
    return claims


def pet_claims(raw: dict[str, Any], hotel_id: str) -> list[Claim]:
    """P01 отеля: явный запрет, явный допуск, метка `pets-allowed` у категорий.

    Пустая pets-policy и NOT_INCLUDED не означают ни запрет, ни допуск. Запрет вместе с меткой
    категории — противоречие: запрет не закрывает обязательное условие, а факт метки рядом
    даёт поиску «источники расходятся»."""
    denials, admissions = [], []
    for i, group in enumerate(raw.get("amenityGroups") or []):
        for j, amenity in enumerate(group.get("amenities") or []):
            name = text(amenity.get("name"))
            phrase = name.casefold().rstrip(". ") if name else None
            pointer = f"{HOTEL_POINTER}/amenityGroups/{i}/amenities/{j}/name"
            if phrase in PET_DENIALS:
                denials.append((pointer, name))
            elif phrase in PET_ADMISSIONS:
                admissions.append((pointer, name))
    tagged = []
    for i, group in enumerate(raw.get("roomGroups") or []):
        tags = group.get("roomAmenityData") or []
        if PET_TAG in tags:
            pointer = f"{HOTEL_POINTER}/roomGroups/{i}/roomAmenityData/{tags.index(PET_TAG)}"
            tagged.append((pointer, f"{hotel_id}:{text(group.get('rgHash'))}"))
    claims = [
        Claim(
            "P01",
            pointer,
            {"species": "none"},
            ((pointer, name),),
            hard=not tagged,
            note="Явный запрет питомцев на странице."
            if not tagged
            else "Противоречие: запрет отеля и pets-allowed категории; требуется проверка.",
        )
        for pointer, name in denials
    ]
    claims += [
        Claim(
            "P01",
            pointer,
            {"species": "present"},
            ((pointer, name),),
            hard=True,
            note=f"Островок: «{name}»; вид, масса и число животных не указаны — уточним у отеля.",
        )
        for pointer, name in admissions
    ]
    if tagged and not admissions:
        rooms = tuple(rid for _, rid in tagged)
        claims.append(
            Claim(
                "P01",
                tagged[0][0],
                {"species": "present", "room_type_ids": list(rooms)},
                tuple((pointer, PET_TAG) for pointer, _ in tagged),
                note="Островок: pets-allowed у части категорий; для всех номеров не обещано.",
                room_type_ids=rooms,
            )
        )
    if not claims:
        claims.append(
            Claim("P01", HOTEL_POINTER, None, note="Нет явного запрета; допуск неизвестен.")
        )
    return claims


# Удобства отеля (группы удобств страницы): заявление отеля о себе (D26), поэтому годится для
# обязательного условия. Слой пакета `hotel_amenities` (pipeline/build_hotel_amenities.py), а не
# сборка каталога: в parse_page их нет, иначе при пересборке факты задвоятся.
AMENITY_VERSION = "ostrovok-amenities.1"
LIFT_YES = {"лифт", "работают лифты для доступа к верхним этажам"}
LIFT_NO = {"отсутствие лифтов"}
# Парковка: «Парковка» без «рядом» — у самого отеля; «рядом с отелем» — чужая, «общественная
# поблизости» — городская; «отсутствует» — своей нет.
PARKING = {
    "парковка": "own",
    "бесплатная парковка": "own",
    "парковка (по запросу)": "own",
    "парковка рядом с отелем": "third_party",
    "бесплатная парковка рядом с отелем": "third_party",
    "бесплатная общественная парковка поблизости": "street",
    "парковка отсутствует": "none",
}
PARKING_ORDER = ("own", "third_party", "street", "none")
FRONT_DESK_24 = {"круглосуточная стойка регистрации"}
EV_CHARGING = {"зарядка электромобилей"}
# «В отеле есть» — категории словаря мест (agents/catalog/places.json): «отель с бассейном»
# проверяется по странице отеля, а не по картам. Детский бассейн — F-характеристика каталога.
ONSITE = "onsite"
ONSITE_NAMES = {
    "pool": {
        "бассейн", "открытый бассейн", "бассейн с подогревом", "сезонный открытый бассейн",
        "крытый бассейн", "открытый бассейн с подогревом", "сезонный крытый бассейн",
        "бассейн на крыше", "закрытый бассейн с подогревом",
    },
    "spa": {"спа-центр", "сауна", "баня", "хаммам", "паровая баня"},
    "gym": {"фитнес-центр", "тренажерный зал"},
    "restaurant": {"ресторан", "ресторан («шведский стол»)"},
    "coffee": {"кафе"},
    "laundry": {"прачечная", "химчистка"},
    "bike_rental": {"прокат велосипедов", "бесплатный прокат велосипедов"},
    "car_rental": {"прокат автомобилей"},
}  # fmt: skip


def amenity_claims(raw: dict[str, Any]) -> list[Claim]:
    """Удобства отеля со страницы: лифт A03, парковка T04, круглосуточная стойка L04, зарядка
    T05 и «в отеле есть» (`onsite`, по категории словаря мест). Чего на странице нет, о том не
    пишем ничего: отсутствие строки — не «нет» (D26); «нет» — только явная строка страницы."""
    found: dict[str, list[tuple[str, str]]] = {}
    for i, group in enumerate(raw.get("amenityGroups") or []):
        for j, amenity in enumerate(group.get("amenities") or []):
            name = text(amenity.get("name"))
            if name:
                pointer = f"{HOTEL_POINTER}/amenityGroups/{i}/amenities/{j}/name"
                found.setdefault(name.casefold().rstrip(". "), []).append((pointer, name))

    def fields(phrases: Iterable[str]) -> tuple[tuple[str, str], ...]:
        return tuple(pair for p in sorted(phrases) for pair in found.get(p, []))

    claims = []
    if lift := fields(LIFT_YES & found.keys()):
        note = "Островок: лифт есть; размеры кабины и этажи не указаны."
        claims.append(Claim("A03", lift[0][0], {"lift_id": "present"}, lift, hard=True, note=note))
    if no_lift := fields(LIFT_NO & found.keys()):
        claims.append(Claim("A03", no_lift[0][0], {"lift_id": "none"}, no_lift, hard=True,
                            note="Островок: «Отсутствие лифтов»."))  # fmt: skip
    if parking := PARKING.keys() & found.keys():
        owner = min((PARKING[p] for p in parking), key=PARKING_ORDER.index)
        value: dict[str, Any] = {"parking_owner": owner}
        if owner == "own" and "бесплатная парковка" in parking:
            value["charging_basis"] = "free"
        if owner == "own" and "парковка (по запросу)" in parking:
            value["reservation"] = True
        f = fields(parking)
        note = {
            "own": "Островок: парковка у отеля; габариты и наличие мест на даты не указаны.",
            "third_party": "Островок: парковка рядом с отелем, своей не указано.",
            "street": "Островок: только общественная парковка поблизости.",
            "none": "Островок: «Парковка отсутствует».",
        }[owner]
        claims.append(Claim("T04", f[0][0], value, f, hard=True, note=note))
    if desk := fields(FRONT_DESK_24 & found.keys()):
        claims.append(Claim("L04", desk[0][0], {"front_desk_hours": "24/7"}, desk, hard=True,
                            note="Островок: круглосуточная стойка регистрации."))  # fmt: skip
    if ev := fields(EV_CHARGING & found.keys()):
        note = "Островок: зарядка электромобилей есть; разъём и мощность не указаны."
        claims.append(Claim("T05", ev[0][0], {"connector": "present"}, ev, hard=True, note=note))
    for category, names in ONSITE_NAMES.items():
        if f := fields(names & found.keys()):
            shown = [n for _, n in f]
            value = {"present": True, "names": shown}
            if all(n.casefold().startswith("сезонный") for n in shown):
                value["seasonal"] = True
            note = "Островок: " + ", ".join(f"«{n}»" for n in shown) + "."
            params = {"category": category}
            claims.append(Claim(ONSITE, f[0][0], value, f, hard=True, note=note, params=params))
    return claims


class SnapshotError(ValueError):
    """Непригодный вход; сообщения не содержат содержимое страниц или секреты."""


class HotelRecord(BaseModel):
    """Строка hotels (packs/FORMAT.md). Подзона Сочи: название `subzone`, код `subzone_id`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    id: str = Field(pattern=r"^[1-9][0-9]*$")
    city_id: CityId
    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    kind: str | None = None
    stars: float | None = Field(default=None, gt=0, le=5)
    subzone: str | None = None
    subzone_id: str | None = None
    source: dict[str, Any]


class RoomTypeRecord(BaseModel):
    """rgHash обязателен: индекс или название не подменяют ID категории."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str
    hotel_id: str = Field(pattern=r"^[1-9][0-9]*$")
    rg_hash: str = Field(min_length=1)
    name: str = Field(min_length=1)
    amenities: list[str]
    source: dict[str, Any]

    @model_validator(mode="after")
    def _identity(self) -> RoomTypeRecord:
        if self.id != f"{self.hotel_id}:{self.rg_hash}":
            raise ValueError("ID категории должен быть <masterId>:<rgHash>")
        return self


@dataclass
class Catalog:
    hotels: list[HotelRecord] = field(default_factory=list)
    room_types: list[RoomTypeRecord] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)


def snapshot_path(root: Path, relative: str) -> Path:
    """Даже пути из манифеста должны оставаться внутри снимка (включая симлинки)."""
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise SnapshotError("snapshot_path_must_be_relative")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise SnapshotError("snapshot_path_outside_root")
    return path


def text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and value.upper() != "UNSPECIFIED" else None


def _source(entry: dict, root: Path, digest: str, pointer: str) -> dict:
    try:
        observed = datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, ValueError, TypeError) as exc:
        raise SnapshotError("snapshot_date_missing_or_invalid") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise SnapshotError("snapshot_date_requires_timezone")
    url = entry.get("final_url") or entry.get("url")
    if not isinstance(url, str) or not re.fullmatch(
        r"https://ostrovok\.ru/hotel/[^/]+/[^/]+/mid[0-9]+/[^/?]+/?", url
    ):
        raise SnapshotError("not_a_hotel_page_url")
    html_sha = entry.get("sha256")
    if not isinstance(html_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", html_sha):
        raise SnapshotError("html_sha256_missing_or_invalid")
    return {
        "provider": "ostrovok.ru",
        "url": url,
        "original_url": entry.get("url"),
        "dataset_version": root.name,
        "dataset_sha256": digest,  # Хеш несжатого JSON, не HTML и не gzip.
        "html_sha256": html_sha,  # Заявлен сборщиком; сам HTML в пилоте не требуется.
        "json_pointer": pointer,
        "snapshot_file": entry["next_file"],
        "fetched_at": observed.isoformat(),
        "mid_url": entry.get("mid_url"),
    }


def _fact(
    hotel: HotelRecord,
    claim: Claim,
    source: dict,
    *,
    room_type: RoomTypeRecord | None = None,
    version: str = VERSION,
) -> Fact:
    """Факт из утверждения: паспорт на каждое подтверждающее поле, первый — главный."""
    subject = room_type.id if room_type else f"hotel:{hotel.id}"
    sources = [
        source | {"json_pointer": pointer, "source_value": shown} for pointer, shown in claim.fields
    ] or [source | {"json_pointer": claim.pointer}]
    identity = f"{version}:{source['dataset_sha256']}:{claim.pointer}:{source['fetched_at']}"
    value, note = claim.value, claim.note
    return Fact(
        fact_id=make_fact_id(hotel.id, subject, claim.feature_id, claim.params, identity),
        hotel_id=hotel.id,
        city_id=hotel.city_id,
        subject_level="room_type" if room_type else "hotel",
        subject_id=subject,
        feature_id=claim.feature_id,
        params=claim.params,
        value=value,
        status="ostrovok_content" if value is not None else "unknown",
        unknown_reason="ostrovok_field_unknown_or_missing" if value is None else None,
        sources=sources,
        recipe_id=f"{claim.feature_id}-OSTROVOK",
        algorithm_version=version,
        confidence={"level": "medium" if value is not None else "unknown", "reason": note},
        observed_at=source["fetched_at"],
        scope_note=note,
        hard_condition_eligible=claim.hard and value is not None,
        recipe_attempts=[
            {
                "recipe_id": f"{claim.feature_id}-OSTROVOK",
                "state": "accepted" if value is not None else "rejected",
                "reason": note if value is not None else "Нет пригодного явного значения D26.",
                "data_kind": "А",
            }
        ],
    )


def parse_page(root: Path, entry: dict) -> Catalog:
    """Одна страница целиком: ошибка категории не оставляет частичный отель."""
    path = snapshot_path(root, entry["next_file"])
    payload = gzip.decompress(path.read_bytes())
    doc = json.loads(payload)
    try:
        raw = doc["props"]["pageProps"]["hotel"]
        location = raw["location"]
    except (KeyError, TypeError) as exc:
        raise SnapshotError("hotel_or_location_missing") from exc
    if not isinstance(raw, dict) or not isinstance(location, dict):
        raise SnapshotError("hotel_or_location_invalid")
    mid = raw.get("masterId")
    if isinstance(mid, bool) or not isinstance(mid, (int, str)):
        raise SnapshotError("masterId_missing_or_invalid")
    digest = hashlib.sha256(payload).hexdigest()
    source = _source(entry, root, digest, HOTEL_POINTER)
    raw_facts = raw.get("facts") or {}
    if not isinstance(raw_facts, dict):
        raise SnapshotError("hotel_facts_invalid")
    stars = raw_facts.get("starRating")
    if not isinstance(stars, (int, float)) or isinstance(stars, bool) or stars == 0:
        stars = None
    for coordinate in (location.get("latitude"), location.get("longitude")):
        if (
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            or coordinate == 0
        ):
            raise SnapshotError("coordinates_missing_or_invalid")
    hotel = HotelRecord(
        id=str(mid),
        city_id=entry["city"],
        name=text(raw.get("name")),
        address=text(location.get("address")),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        kind=text(raw_facts.get("kind")),
        stars=stars,
        source=source | {"geography_verified": False},
    )
    result = Catalog(hotels=[hotel])
    groups = raw.get("roomGroups") or []
    if not isinstance(groups, list):
        raise SnapshotError("roomGroups_invalid")
    seen_groups = set()
    for i, group in enumerate(groups):
        if not isinstance(group, dict):
            raise SnapshotError("room_group_invalid")
        rg_hash = text(group.get("rgHash"))
        if not rg_hash:
            raise SnapshotError("rgHash_missing")
        if rg_hash in seen_groups:
            raise SnapshotError("rgHash_duplicate")
        seen_groups.add(rg_hash)
        pointer = f"{HOTEL_POINTER}/roomGroups/{i}"
        group_source = source | {"json_pointer": pointer}
        tags = group.get("roomAmenityData") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise SnapshotError("room_amenities_invalid")
        names = group.get("nameStruct") or {}
        if not isinstance(names, dict):
            raise SnapshotError("room_name_invalid")
        room = RoomTypeRecord(
            id=f"{hotel.id}:{rg_hash}",
            hotel_id=hotel.id,
            rg_hash=rg_hash,
            name=text(names.get("mainName")) or text(group.get("name")),
            amenities=tags,
            source=group_source,
        )
        result.room_types.append(room)
        bedding = text(names.get("beddingType"))
        for claim in room_claims(pointer, tags, bedding):
            result.facts.append(_fact(hotel, claim, group_source, room_type=room))
    amenity_groups = raw.get("amenityGroups") or []
    if not isinstance(amenity_groups, list):
        raise SnapshotError("amenityGroups_invalid")
    for group in amenity_groups:
        if not isinstance(group, dict):
            raise SnapshotError("amenity_group_invalid")
        amenities = group.get("amenities") or []
        if not isinstance(amenities, list):
            raise SnapshotError("hotel_amenities_invalid")
        if not all(isinstance(amenity, dict) for amenity in amenities):
            raise SnapshotError("hotel_amenity_invalid")
    for claim in pet_claims(raw, hotel.id):
        result.facts.append(_fact(hotel, claim, source))
    return result


def amenity_facts(root: Path, city_id: CityId, hotel_ids: set[str]) -> list[Fact]:
    """Факты удобств отеля (`amenity_claims`) для отелей пакета: страница отеля — как в
    `read_snapshot`, паспорт — поле страницы. Непригодные страницы пропускаются: их уже отметил
    журнал исключений сборки каталога."""
    facts: list[Fact] = []
    for entry in iter_jsonl(root / "manifests/pages.jsonl"):
        if entry.get("city") != city_id or entry.get("status") != 200 or not entry.get("next_file"):
            continue
        try:
            hotel = parse_page(root, entry).hotels[0]
        except (OSError, ValueError, TypeError, KeyError, EOFError, zlib.error):
            continue
        if hotel.id not in hotel_ids:
            continue
        payload = gzip.decompress(snapshot_path(root, entry["next_file"]).read_bytes())
        raw = json.loads(payload)["props"]["pageProps"]["hotel"]
        source = _source(entry, root, hashlib.sha256(payload).hexdigest(), HOTEL_POINTER)
        for claim in amenity_claims(raw):
            facts.append(_fact(hotel, claim, source, version=AMENITY_VERSION))
    return facts


def read_snapshot(root: Path, city_id: CityId, limit: int | None = 5) -> Catalog:
    """Пилот с лимитом или весь журнал при limit=None; город пока только метка рынка."""
    if limit is not None and limit < 1:
        raise SnapshotError("limit_must_be_positive")
    result = Catalog()
    seen = set()
    for entry in iter_jsonl(root / "manifests/pages.jsonl"):
        if entry.get("city") != city_id:
            continue
        reason = None
        if entry.get("status") != 200:
            reason = "non_hotel_response"
        elif not entry.get("next_file"):
            reason = "next_data_missing"
        else:
            try:
                page = parse_page(root, entry)
                hotel_id = page.hotels[0].id
                if hotel_id in seen:
                    reason = "duplicate_masterId"
                else:
                    seen.add(hotel_id)
                    result.hotels.extend(page.hotels)
                    result.room_types.extend(page.room_types)
                    result.facts.extend(page.facts)
            except (OSError, ValueError, TypeError, KeyError, EOFError, zlib.error) as exc:
                reason = str(exc) if isinstance(exc, SnapshotError) else type(exc).__name__
                if isinstance(exc, ValidationError):
                    reason = "record_validation_failed"
        if reason:
            result.excluded.append({"url": entry.get("url"), "city_id": city_id, "reason": reason})
        if limit is not None and len(result.hotels) >= limit:
            break
    return result


@register_connector
class OstrovokSiteConnector:
    id = "ostrovok_site"
    source_id = "ostrovok_site"
    layers = ("hotels", "room_types", "facts")

    def root(self, ctx: CityContext) -> Path:
        ref = ctx.datasets.get(self.source_id)
        return ref.path if ref else ctx.data_dir

    def available(self, ctx: CityContext) -> bool:
        return (self.root(ctx) / "manifests/pages.jsonl").is_file()

    def fetch(self, ctx: CityContext, workdir: Path) -> list[DatasetRef]:
        # Вход уже скачан: fetch лишь описывает существующий снимок, ничего не пишет.
        root = self.root(ctx)
        if not self.available(ctx):
            raise SnapshotError("snapshot_manifest_missing")
        return [
            DatasetRef(
                id=self.id,
                path=root,
                provider="ostrovok.ru",
                version=root.name,
                sha256=sha256_file(root / "manifests/pages.jsonl"),
            )
        ]
