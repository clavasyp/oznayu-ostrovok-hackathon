"""Синтетический снимок проверяет контракт, а не выдаёт тестовые отели за настоящие."""

import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest

from agents.query_ast import Condition
from connectors.ostrovok_site import (
    OstrovokSiteConnector,
    RoomTypeRecord,
    parse_page,
    read_snapshot,
    snapshot_path,
)
from core.facts import Fact
from core.packs import discover, iter_jsonl, verify
from core.plugins import CONNECTORS, CityContext, DatasetRef
from core.search import evaluate
from core.verdict import Verdict
from pipeline.import_ostrovok import ROOT, build_pilot, load_test_database


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "snapshot_2026-10-24"
    (root / "manifests").mkdir(parents=True)

    def add(city="moscow", hid="101", *, mutate=None, status=200):
        hotel = {
            "masterId": hid,
            "name": "Синтетический тестовый отель",
            "location": {"address": "Тестовая улица, 1Б", "latitude": 55.7, "longitude": 37.6},
            "facts": {"kind": "HOTEL", "starRating": 0, "floorsNumber": 0, "yearBuilt": 0},
            "policies": {"pets": {"type": "PETS", "value": []}},
            "amenityGroups": [],
            "roomGroups": [
                {
                    "rgHash": "rg1",
                    "nameStruct": {"mainName": "Стандарт", "beddingType": ""},
                    "roomAmenityData": ["air-conditioning", "balcony", "wi-fi"],
                },
                {
                    "rgHash": "rg2",
                    "nameStruct": {"mainName": "Стандарт", "beddingType": ""},
                    "roomAmenityData": [],
                },
            ],
        }
        if mutate:
            mutate(hotel)
        payload = json.dumps({"props": {"pageProps": {"hotel": hotel}}}).encode()
        # Имитирует хеш HTML из журнала. Он намеренно отличается от JSON-хеша.
        html_sha = hashlib.sha256((city + hid).encode()).hexdigest()
        relative = f"next/{html_sha[:2]}/{html_sha}.json.gz"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(payload))
        entry = {
            "city": city,
            "status": status,
            "next_file": relative,
            "url": f"https://ostrovok.ru/hotel/russia/{city}/mid555/old/",
            "final_url": f"https://ostrovok.ru/hotel/russia/{city}/mid999/current/",
            "mid_url": "555",
            "masterId": "999",
            "sha256": html_sha,
            "fetched_at": "2026-10-24T16:00:00+00:00",
        }
        with (root / "manifests/pages.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry, hotel

    return root, add


def resolve_pointer(doc, pointer):
    value = doc
    for component in pointer.lstrip("/").split("/"):
        value = value[int(component)] if isinstance(value, list) else value[component]
    return value


def test_identity_categories_and_provenance(snapshot):
    root, add = snapshot
    entry, raw = add()
    catalog = parse_page(root, entry)
    (hotel,) = catalog.hotels
    assert hotel.id == raw["masterId"] != entry["mid_url"] != entry["masterId"]
    assert hotel.stars is None and hotel.kind == "HOTEL"
    assert hotel.source["geography_verified"] is False
    assert [r.id for r in catalog.room_types] == ["101:rg1", "101:rg2"]
    assert len({r.name for r in catalog.room_types}) == 1  # Имя не является ID.
    payload = gzip.decompress((root / entry["next_file"]).read_bytes())
    doc = json.loads(payload)
    for fact in catalog.facts:
        Fact.model_validate(fact.model_dump())
        source = fact.sources[0].model_dump()
        assert source["dataset_sha256"] == hashlib.sha256(payload).hexdigest()
        assert source["html_sha256"] == entry["sha256"] != source["dataset_sha256"]
        assert source["url"] == entry["final_url"]
        assert not Path(source["snapshot_file"]).is_absolute()
        value = resolve_pointer(doc, source["json_pointer"])
        if "source_value" in source:
            assert value == source["source_value"]
        assert fact.observed_at.isoformat() == entry["fetched_at"]


def test_partial_room_claims_cannot_guarantee_all_variants(snapshot):
    root, add = snapshot
    entry, _ = add()
    catalog = parse_page(root, entry)
    air = next(f for f in catalog.facts if f.subject_id == "101:rg1" and f.feature_id == "S04")
    assert air.status == "ostrovok_content" and air.value["hvac_type"] == "present"
    assert not air.usable_in_hard
    assert "individual_control" not in air.value
    balcony = next(f for f in catalog.facts if f.subject_id == "101:rg1" and f.feature_id == "M02")
    assert balcony.value["private_balcony"] is True  # поле каталога, но не гарантия всех номеров
    assert not balcony.usable_in_hard
    # «Есть интернет» — не измеренная скорость (R01); чайника меткой не доказать (M04).
    assert not any(f.feature_id in {"R01", "M04"} for f in catalog.facts)
    assert all(
        f.status == "unknown" and f.value is None and f.unknown_reason
        for f in catalog.facts
        if f.subject_id == "101:rg2"
    )
    assert not any(f.feature_id in {"V01", "H01", "H03"} for f in catalog.facts)


@pytest.mark.parametrize("sentinel", [None, False, 0, [], "UNSPECIFIED"])
def test_d26_policy_sentinels_do_not_become_pet_prohibition(snapshot, sentinel):
    root, add = snapshot
    entry, _ = add(mutate=lambda h: h["policies"]["pets"].update(value=sentinel))
    (pet,) = [f for f in parse_page(root, entry).facts if f.feature_id == "P01"]
    assert pet.status == "unknown" and pet.value is None and not pet.usable_in_hard


@pytest.mark.parametrize("contradiction", [False, True])
def test_explicit_pet_denial_and_category_conflict(snapshot, contradiction):
    root, add = snapshot

    def mutate(hotel):
        hotel["amenityGroups"] = [
            {
                "amenities": [
                    {
                        "name": "Размещение с домашними животными не допускается",
                    }
                ]
            }
        ]
        if contradiction:
            hotel["roomGroups"][0]["roomAmenityData"].append("pets-allowed")

    entry, _ = add(mutate=mutate)
    pets = [f for f in parse_page(root, entry).facts if f.feature_id == "P01"]
    (pet,) = [f for f in pets if f.value["species"] == "none"]
    assert pet.status == "ostrovok_content" and pet.value == {"species": "none"}
    assert pet.usable_in_hard is not contradiction
    tagged = [f for f in pets if f is not pet]
    if not contradiction:
        assert not tagged
        return
    # Метка категории — второй факт того же места: поиск видит «источники расходятся».
    (tag,) = tagged
    assert tag.subject_id == "hotel:101" and not tag.usable_in_hard
    assert tag.value == {"species": "present", "room_type_ids": ["101:rg1"]}
    assert tag.sources[0].model_dump()["source_value"] == "pets-allowed"
    soft = Condition(feature_id="P01", field="species", op="ne", value="none")
    assert evaluate(pets, soft, hard=False)["reason_code"] == "conflict"


ROOM_CASES = [
    # метки категории → значение факта; None — «неизвестно» (заглушка)
    (["balcony", "patio"], "M02", {"private_balcony": True, "private_terrace": True}),
    (["patio"], "M02", {"private_terrace": True}),  # терраса — не балкон
    (["fridge", "microwave", "kitchen-stuff"], "M03", None),  # не кухня
    (
        ["fridge", "kitchenette", "stove"],
        "M03",
        {"private_kitchenette": True, "appliances": ["fridge", "stove"]},
    ),
    (["safe"], "M05", {"access": "present"}),
    (["blackout-blinds"], "S05", {"opaque_coverage": "present"}),
    (["air-conditioning"], "S04", {"hvac_type": "present"}),
]


@pytest.mark.parametrize("tags,feature,expected", ROOM_CASES)
def test_room_tags_become_catalog_fields_of_the_same_meaning(snapshot, tags, feature, expected):
    root, add = snapshot
    entry, raw = add(mutate=lambda h: h["roomGroups"][0].update(roomAmenityData=tags))
    facts = [
        f
        for f in parse_page(root, entry).facts
        if f.subject_id == "101:rg1" and f.feature_id == feature
    ]
    (fact,) = facts  # одна запись на категорию и характеристику — одно место в поиске
    if expected is None:
        assert fact.status == "unknown" and fact.value is None
        return
    value = dict(fact.value)
    shown = value.pop("published_room_equipment_tags")
    assert value == expected
    assert [s.model_dump()["source_value"] for s in fact.sources] == shown
    payload = json.loads(gzip.decompress((root / entry["next_file"]).read_bytes()))
    for source in fact.sources:
        assert resolve_pointer(payload, source.json_pointer) == source.source_value


@pytest.mark.parametrize(
    "bedding,expected",
    [
        ("двуспальная кровать", "double"),
        ("2 отдельные кровати", "twin"),
        ("диван", None),
    ],
)
def test_bedding_text_sets_bed_configuration_only_when_unambiguous(snapshot, bedding, expected):
    root, add = snapshot
    entry, _ = add(mutate=lambda h: h["roomGroups"][0]["nameStruct"].update(beddingType=bedding))
    (bed,) = [
        f
        for f in parse_page(root, entry).facts
        if f.subject_id == "101:rg1" and f.feature_id == "M01"
    ]
    assert bed.value["published_bedding_text"] == bedding
    assert bed.value.get("bed_configuration") == expected
    want = Condition(feature_id="M01", field="bed_configuration", op="eq", value="double")
    verdict = evaluate([bed], want, hard=False)["verdict"]
    assert verdict == {"double": Verdict.yes, "twin": Verdict.no, None: Verdict.unknown}[expected]


@pytest.mark.parametrize(
    "phrase",
    [
        "Размещение с домашними животными",
        "Бесплатное размещение с домашними животными",
        "Размещение с домашними животными (меньше 5 кг) (за дополнительную плату)",
    ],
)
def test_explicit_pet_admission_is_a_yes_without_species(snapshot, phrase):
    root, add = snapshot
    entry, _ = add(mutate=lambda h: h.update(amenityGroups=[{"amenities": [{"name": phrase}]}]))
    (pet,) = [f for f in parse_page(root, entry).facts if f.feature_id == "P01"]
    assert pet.status == "ostrovok_content" and pet.value == {"species": "present"}
    assert pet.usable_in_hard and phrase in pet.scope_note
    any_pet = Condition(feature_id="P01", field="species", op="ne", value="none")
    dog = Condition(feature_id="P01", field="species", op="in", value=["dog", "dog_cat", "any"])
    assert evaluate([pet], any_pet, hard=True)["verdict"] == Verdict.yes
    result = evaluate([pet], dog, hard=True)  # вид не назван — «неизвестно», не «нет» (D5)
    assert result["verdict"] == Verdict.unknown and result["reason_code"] == "value_unspecified"


def test_soft_balcony_condition_compares_on_the_category(snapshot):
    root, add = snapshot
    entry, _ = add()
    facts = [f for f in parse_page(root, entry).facts if f.feature_id == "M02"]
    balcony = Condition(feature_id="M02", field="private_balcony", op="eq", value=True)
    on_rg1 = [f for f in facts if f.subject_id == "101:rg1"]
    assert evaluate(on_rg1, balcony, hard=False)["verdict"] == Verdict.yes
    assert evaluate(on_rg1, balcony, hard=True)["reason_code"] == "ostrovok_partial"


@pytest.mark.parametrize(
    "field,value",
    [
        ("masterId", None),
        ("masterId", False),
        ("masterId", "0"),
        ("roomGroups", [{"nameStruct": {"mainName": "Без ключа"}}]),
    ],
)
def test_bad_identity_is_excluded_without_fabricated_ids(snapshot, field, value):
    root, add = snapshot
    add(mutate=lambda h: h.update({field: value}))
    result = read_snapshot(root, "moscow")
    assert not result.hotels and not result.room_types and not result.facts
    assert len(result.excluded) == 1


@pytest.mark.parametrize("coordinate", [None, False, 0, float("nan"), float("inf"), 91])
def test_bad_coordinates_do_not_produce_catalog_rows(snapshot, coordinate):
    root, add = snapshot
    add(mutate=lambda h: h["location"].update(latitude=coordinate))
    catalog = read_snapshot(root, "moscow")
    assert not catalog.hotels and len(catalog.excluded) == 1


@pytest.mark.parametrize("amenities", ["invalid", ["invalid"], [{"amenities": ["invalid"]}]])
def test_malformed_amenities_are_reported_without_partial_rows(snapshot, amenities):
    root, add = snapshot
    add(mutate=lambda h: h.update(amenityGroups=amenities))
    catalog = read_snapshot(root, "moscow")
    assert not catalog.hotels and not catalog.room_types and not catalog.facts
    assert len(catalog.excluded) == 1


def test_room_id_is_validated_against_its_hotel_and_rg_hash(snapshot):
    root, add = snapshot
    entry, _ = add()
    room = parse_page(root, entry).room_types[0].model_dump()
    room["id"] = "101:different"
    with pytest.raises(ValueError, match="ID категории"):
        RoomTypeRecord.model_validate(room)


def test_source_requires_timestamp_with_timezone_and_hotel_url(snapshot):
    root, add = snapshot
    entry, _ = add()
    for change in ({"fetched_at": "2026-10-24"}, {"final_url": "https://ostrovok.ru/hotels/"}):
        with pytest.raises(ValueError):
            parse_page(root, entry | change)


def test_corrupt_gzip_is_excluded(snapshot):
    root, add = snapshot
    entry, _ = add()
    (root / entry["next_file"]).write_bytes(b"not gzip")
    catalog = read_snapshot(root, "moscow")
    assert not catalog.hotels and catalog.excluded[0]["reason"] == "BadGzipFile"


def test_redirect_duplicate_missing_file_and_limit(snapshot):
    root, add = snapshot
    add(hid="1", status=307)
    missing, _ = add(hid="2")
    (root / missing["next_file"]).unlink()
    add(hid="3")
    add(hid="3")
    add(hid="4")
    add(hid="5")
    result = read_snapshot(root, "moscow", limit=2)
    assert [h.id for h in result.hotels] == ["3", "4"]
    assert [r["reason"] for r in result.excluded] == [
        "non_hotel_response",
        "FileNotFoundError",
        "duplicate_masterId",
    ]


def test_missing_snapshot_and_path_escape(snapshot, tmp_path):
    root, _ = snapshot
    connector = OstrovokSiteConnector()
    ctx = CityContext(city_id="moscow", timezone="Europe/Moscow", data_dir=root)
    assert not connector.available(ctx)
    with pytest.raises(ValueError, match="snapshot_manifest_missing"):
        connector.fetch(ctx, tmp_path)
    for path in ("../other.json.gz", str(tmp_path / "absolute.json.gz")):
        with pytest.raises(ValueError):
            snapshot_path(root, path)
    (root / "external").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        snapshot_path(root, "external/anything")


def test_connector_registration_and_dataset_reference(snapshot, tmp_path):
    root, add = snapshot
    add()
    ctx = CityContext(
        city_id="moscow",
        timezone="Europe/Moscow",
        data_dir=tmp_path / "unused",
        datasets={
            "ostrovok_site": DatasetRef(
                id="ostrovok_site",
                path=root,
                provider="ostrovok.ru",
            )
        },
    )
    connector = CONNECTORS["ostrovok_site"]
    assert connector.available(ctx)
    (ref,) = connector.fetch(ctx, tmp_path / "unused-output")
    assert ref.path == root and ref.sha256
    assert not (tmp_path / "unused-output").exists()


def test_pack_roundtrip_and_determinism(snapshot, tmp_path):
    root, add = snapshot
    add("moscow", "101")
    add("sochi", "202")
    a, b = tmp_path / "out-a", tmp_path / "out-b"
    assert build_pilot(root, a) == build_pilot(root, b)
    assert {str(p.relative_to(a)): p.read_bytes() for p in a.rglob("*") if p.is_file()} == {
        str(p.relative_to(b)): p.read_bytes() for p in b.rglob("*") if p.is_file()
    }
    for pack in discover(a):
        manifest = verify(pack)
        hotels = {r["id"] for r in iter_jsonl(pack / "data/hotels.jsonl.gz")}
        rooms = {r["id"] for r in iter_jsonl(pack / "data/room_types.jsonl.gz")}
        assert len(hotels) == 1 and len(rooms) == 2
        for raw in iter_jsonl(pack / "data/facts.jsonl.gz"):
            fact = Fact.model_validate(raw)
            assert fact.hotel_id in hotels
            assert fact.subject_level != "room_type" or fact.subject_id in rooms
        assert manifest.pack_version.startswith("ostrovok-pilot-")
        assert [(f.kind, f.rows) for f in manifest.files][:2] == [("hotels", 1), ("room_types", 2)]


def test_pilot_refuses_production_outputs_and_empty_import(snapshot, tmp_path):
    root, add = snapshot
    add()
    for out in (ROOT / "packs", ROOT / "packs/moscow", root, tmp_path):
        with pytest.raises(ValueError, match="пересекается"):
            build_pilot(root, out)
    out = tmp_path / "empty-result"
    with pytest.raises(ValueError, match="sochi: нет пригодных"):
        build_pilot(root, out)
    assert not out.exists()
    with pytest.raises(ValueError, match="положительный"):
        build_pilot(root, out, limit_per_city=0)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://localhost/oznayu",
        "sqlite:///catalog_test",
    ],
)
def test_database_guard_refuses_non_test_database(tmp_path, url):
    with pytest.raises(ValueError, match="отдельную PostgreSQL"):
        load_test_database(tmp_path, url)


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="нужна отдельная PostGIS *_test"
)
def test_real_postgis_loader_roundtrip(snapshot, tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.engine import make_url

    from core.db import FactRow, Hotel, RoomType
    from pipeline.load_packs import load_pack

    url = os.environ["TEST_DATABASE_URL"]
    assert (make_url(url).database or "").endswith("_test")
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    root, add = snapshot
    add("moscow", "101")
    add("sochi", "202")
    out = tmp_path / "db-pilot"
    build_pilot(root, out)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                for pack in discover(out):
                    manifest = verify(pack)
                    load_pack(conn, pack)
                    assert "уже загружен" in load_pack(conn, pack)
                    hotels = list(conn.execute(select(Hotel.id).where(Hotel.city_id == pack.name)))
                    assert len(hotels) == 1
                    hid = hotels[0][0]
                    assert conn.scalar(select(func.ST_Y(Hotel.geom)).where(Hotel.id == hid)) == 55.7
                    assert (
                        conn.scalar(
                            select(func.count())
                            .select_from(RoomType)
                            .where(RoomType.hotel_id == hid)
                        )
                        == 2
                    )
                    expected = next(f.rows for f in manifest.files if f.kind == "facts")
                    assert (
                        conn.scalar(
                            select(func.count())
                            .select_from(FactRow)
                            .where(FactRow.hotel_id == hid, FactRow.extra.has_key("pack"))
                        )
                        == expected
                    )
            finally:
                transaction.rollback()  # Не оставлять пилотные отели в общей тестовой БД.
    finally:
        engine.dispose()
