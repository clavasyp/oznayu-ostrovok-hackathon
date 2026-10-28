"""Связь «отель → здание» по точке (D42-1), H03, N02 / N03 от контура и оценка от точки (D18)."""

import gzip
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agents.address.match import contradiction
from agents.address.normalize import parse_address
from core.facts import Fact
from core.packs import (
    Manifest,
    PackError,
    PackFile,
    iter_jsonl,
    read_manifest,
    sha256_file,
    write_jsonl_gz,
    write_manifest,
)
from pipeline import build_building_links as bl

LAT, LON = 0.7, 0.6  # Гвинейский залив: вдали от настоящих данных
D = 0.0002  # полуширина квадратного корпуса, ≈22 м


def way(id_, lon, lat, d=D, **tags):
    """Здание из ответа Overpass (`out meta geom`): квадрат вокруг точки."""
    ring = [(lon - d, lat - d), (lon + d, lat - d), (lon + d, lat + d), (lon - d, lat + d)]
    ring.append(ring[0])
    return {
        "type": "way",
        "id": id_,
        "version": 3,
        "timestamp": "2025-01-02T03:04:05Z",
        "tags": {"building": "yes", **tags},
        "bounds": {"minlat": lat - d, "minlon": lon - d, "maxlat": lat + d, "maxlon": lon + d},
        "geometry": [{"lat": y, "lon": x} for x, y in ring],
    }


def line(id_, lat, **tags):
    """Ось дороги или пути вдоль широты."""
    return {
        "type": "way",
        "id": id_,
        "version": 1,
        "timestamp": "2024-05-06T07:08:09Z",
        "tags": tags,
        "geometry": [{"lat": lat, "lon": LON - 0.05}, {"lat": lat, "lon": LON + 0.05}],
    }


def hotel(hid, address, lon=LON, lat=LAT, city="bltest"):
    return {"id": hid, "city_id": city, "name": "Отель " + hid, "address": address,
            "latitude": lat, "longitude": lon}  # fmt: skip


def footprints(*els):
    return {bl.osm_id(e): (e, bl.element_geometry(e)) for e in els}


LENINA_5 = {"addr:street": "улица Ленина", "addr:housenumber": "5"}


# ------------------------------------------------------------------------------ связь по точке


def test_point_inside_one_footprint_is_auto_point_link():
    fp = footprints(way(1, LON, LAT, **LENINA_5), way(2, LON + 0.01, LAT))
    [o] = bl.link_by_point([hotel("1", "Москва, ул. Ленина, д. 5")], fp, skip=set())
    assert o.code == "linked" and o.candidates == ["way/1"]
    assert o.evidence["rule"] == "auto_point" and o.evidence["decision"] == "D42-1"
    assert o.evidence["address_check"] == "match"
    assert o.evidence["source_url"] == "https://www.openstreetmap.org/way/1"
    # «5 к2» у отеля и «5» у здания — не противоречие: корпус назван только у отеля.
    [o] = bl.link_by_point([hotel("1", "ул. Ленина, 5 к2")], fp, skip=set())
    assert o.code == "linked" and o.evidence["address_check"] == "compatible"
    # У здания нет адреса — сравнивать нечего, связь по точке.
    [o] = bl.link_by_point([hotel("1", "ул. Ленина, 5")], footprints(way(3, LON, LAT)), set())
    assert o.code == "linked" and o.evidence["address_check"] == "not_compared"


def test_overlapping_footprints_no_footprint_and_existing_links_give_no_link():
    fp = footprints(way(1, LON, LAT), way(2, LON + D, LAT))  # точка на общей части
    hotels = [hotel("1", "", lon=LON + D / 2), hotel("2", "", lon=LON + 0.05), hotel("3", "")]
    out = {o.hotel_id: o for o in bl.link_by_point(hotels, fp, skip={"3"})}
    assert out["1"].code == "several_footprints" and out["1"].candidates == ["way/1", "way/2"]
    assert out["2"].code == "no_footprint" and out["2"].candidates == []
    assert "3" not in out  # прежняя связь не пересматривается
    # Незамкнутый контур не проверить: точка в его рамке — связи нет.
    broken = way(4, LON, LAT)
    broken["geometry"] = broken["geometry"][:-1]
    [o] = bl.link_by_point([hotel("1", "")], footprints(broken), set())
    assert o.code == "invalid_footprint"


def test_contradicting_address_gives_no_link():
    fp = footprints(way(1, LON, LAT, **{"addr:street": "улица Ленина", "addr:housenumber": "7"}))
    [o] = bl.link_by_point([hotel("1", "ул. Ленина, 5")], fp, skip=set())
    assert o.code == "address_conflict" and o.evidence["conflict"] == "house"
    [o] = bl.link_by_point([hotel("1", "ул. Пушкина, 7")], fp, skip=set())
    assert o.code == "address_conflict" and o.evidence["conflict"] == "street"
    # Угловой дом: второй адрес здания совпадает — не противоречие.
    corner = way(
        2,
        LON,
        LAT,
        **{
            "addr:street": "улица Ленина",
            "addr:housenumber": "7",
            "addr2:street": "улица Пушкина",
            "addr2:housenumber": "2",
        },
    )
    [o] = bl.link_by_point([hotel("1", "ул. Пушкина, 2")], footprints(corner), skip=set())
    assert o.code == "linked"


@pytest.mark.parametrize(
    ("hotel_address", "osm_address", "expected"),
    [
        ("ул. Ленина, 5", "улица Ленина, 5", None),
        ("ул. Ленина, 5", "улица Ленина, 7", "house"),
        ("ул. Ленина, 5к1", "улица Ленина, 5к2", "house"),
        ("ул. Ленина, 5", "улица Ленина, 5 с3", None),
        ("ул. Ленина, 5а", "улица Ленина, 5б", "house"),
        ("ул. Ленина, 22", "улица Ленина, 21-23", None),
        ("ул. Ленина, 20/1", "улица Ленина, 20", None),
        ("Lenina Street 5", "улица Ленина, 5", None),
        ("ул. Ленина, 5", "Ленинский проспект, 5", "street"),
        ("Смоленская улица, 3", "Смоленская площадь, 3", "street"),
        ("Сочи", "улица Ленина, 5", None),
        # Тип улицы не распознан у отеля — решает дом; слово типа внутри названия — не имя.
        ("Sochi, 6 Yeniseyskaya Street", "улица Енисейская, 6", None),
        ("bul'var Nadezhd, 8/2", "бульвар Надежд, 8/2", None),
        ("bul'var Nadezhd, 8/2", "бульвар Надежд, 8/3", "house"),
        ("ул. Демократическая улица, д. 62А", "улица Демократическая, 62", None),
    ],
)
def test_address_contradiction(hotel_address, osm_address, expected):
    assert contradiction(parse_address(hotel_address), parse_address(osm_address)) == expected


# --------------------------------------------------------------------------- N02 / N03 и H03

EXTENT = (LAT - 0.06, LON - 0.06, LAT + 0.06, LON + 0.06)
RAW = {"osm_base": "2026-10-28T09:00:00Z", "sha256": "ab" * 32}
STAMP = "2026-10-28T10:00:00+00:00"


def roads():
    return bl.Targets("bltest", [line(10, LAT + 0.001, highway="primary", name="Тестовая")],
                      EXTENT, RAW)  # fmt: skip


def test_distance_from_footprint_is_computed_and_from_point_is_estimate():
    from shapely.geometry import shape

    building = bl.building_row(way(1, LON, LAT), bl.element_geometry(way(1, LON, LAT)), "bltest")
    footprint = shape(building["geometry"])
    targets = roads()
    computed = bl.building_distance_fact("1", "bltest", "N02", building, footprint, targets,
                                         STAMP, bl.BUILDINGS)  # fmt: skip
    Fact.model_validate(computed)
    assert computed["status"] == "computed" and computed["subject_id"] == "way/1"
    assert computed["subject_level"] == "building" and computed["recipe_id"] == "N02-R2"
    assert computed["params"]["distance_origin"] == "accepted building footprint"
    assert computed["params"]["road_classes"] == sorted(bl.BUSY)
    assert computed["value"]["target_id"] == "way/10"
    assert computed["value"]["target_class"] == "primary"
    assert 86 < computed["value"]["distance_m"] < 90  # (0,001 − 0,0002)° широты ≈ 88 м
    estimate = bl.point_estimate_fact(hotel("2", ""), "bltest", "N02",
                                      bl.Outcome("2", "several_footprints", ["way/3", "way/4"]),
                                      targets, STAMP)  # fmt: skip
    fact = Fact.model_validate(estimate)
    assert fact.status == "estimate" and not fact.usable_in_hard  # D18
    assert estimate["hard_condition_eligible"] is False
    assert estimate["subject_id"] == "hotel:2" and estimate["recipe_id"] == "N02-R2-point"
    assert estimate["params"]["distance_origin"] == "OTA hotel point"
    assert "Оценка от точки отеля" in estimate["scope_note"]
    assert "нескольких контуров" in estimate["scope_note"] and "D18" in estimate["scope_note"]
    assert estimate["depends_on"][0]["candidate_building_ids"] == ["way/3", "way/4"]
    assert 109 < estimate["value"]["distance_m"] < 112  # 0,001° ≈ 110,6 м от точки
    assert estimate["fact_id"] != computed["fact_id"]


def test_rail_excludes_disused_and_target_beyond_extent_is_not_claimed():
    from shapely.geometry import Point

    rail = [line(20, LAT - 0.002, railway="rail"), line(21, LAT - 0.0005, railway="rail",
                                                        disused="yes")]  # fmt: skip
    targets = bl.Targets("bltest", [r for r in rail if bl.active_rail(r["tags"])], EXTENT, RAW)
    r, dist = targets.nearest(Point(LON, LAT))
    assert r["id"] == 20 and 220 < dist < 223
    far = bl.Targets("bltest", [line(22, LAT + 0.059, railway="rail")], EXTENT, RAW)
    assert far.nearest(Point(LON, LAT - 0.05)) is None  # край выборки ближе найденного пути


def test_levels_fact_from_tag_and_missing_tag_is_unknown():
    link = {"method": "auto_point", "evidence": {"rule": "auto_point"}}
    source = {"provider": "OpenStreetMap", "url": "https://www.openstreetmap.org/way/1"}
    b = {"id": "way/1", "tags": {"building": "yes", "building:levels": "9"}}
    row = bl.levels_fact("1", "bltest", b, link, source, STAMP)
    Fact.model_validate(row)
    assert row["status"] == "observed" and row["value"]["aboveground_levels"] == 9
    assert row["unit"] == "aboveground_storeys" and row["recipe_id"] == "H03-R2"
    assert row["params"] == {"building_match_evidence": {"method": "auto_point",
                                                         "rule": "auto_point"}}  # fmt: skip
    for tags in ({"building": "yes"}, {"building": "yes", "building:levels": "5;6"}):
        row = bl.levels_fact("1", "bltest", {"id": "way/1", "tags": tags}, link, source, STAMP)
        Fact.model_validate(row)
        assert row["status"] == "unknown" and row["value"] is None
        assert row["unknown_reason"] == "missing_aboveground_levels"
        assert row["subject_level"] == "building" and row["subject_id"] == "way/1"


# ------------------------------------------------------------------------- сборка слоёв пакета


def write_raw(raw, city, buildings, roads_, rail):
    folder = raw / city
    folder.mkdir(parents=True)
    files = []
    for kind, elements in (("buildings", buildings), ("roads", roads_), ("rail", rail)):
        path = folder / f"{kind}_000.json.gz"
        body = {"osm3s": {"timestamp_osm_base": "2026-10-28T09:00:00Z"}, "elements": elements}
        with gzip.GzipFile(path, "wb", mtime=0) as gz:
            gz.write(json.dumps(body).encode())
        files.append({"file": path.name, "kind": kind, "url": "https://overpass.test/api",
                      "query": kind, "query_sha256": kind, "fetched_at": STAMP,
                      "osm_base": "2026-10-28T09:00:00Z", "sha256": sha256_file(path)})  # fmt: skip
    manifest = {"city_id": city, "bbox": list(EXTENT), "files": files}
    (folder / "manifest.json").write_text(json.dumps(manifest))


def write_pack(packs, city, base):
    """Пакет: отели 1–4, прежняя связь отеля 3 с посчитанными N02 / N03, покрытие."""
    hid = {k: str(base + k) for k in range(1, 5)}
    b = {k: f"way/{base + k}" for k in range(1, 7)}
    hotels = [
        hotel(hid[1], "Москва, ул. Ленина, д. 5", city=city),  # внутри одного контура
        hotel(hid[2], "", lon=LON + 0.01 + D / 2, city=city),  # внутри двух
        hotel(hid[3], "ул. Ленина, 1", lon=LON - 0.02, city=city),  # прежняя связь
        hotel(hid[4], "ул. Ленина, 9", lon=LON + 0.02, city=city),  # адрес расходится
    ]
    old_building = bl.building_row(way(base + 5, LON - 0.02, LAT), bl.element_geometry(
        way(base + 5, LON - 0.02, LAT)), city) | {"tags": {"building": "yes",
                                                           "building:levels": "4"}}  # fmt: skip
    old_link = {"hotel_id": hid[3], "building_id": b[5], "method": "auto",
                "evidence": {"rule": "unique_full_address_and_point_in_polygon"}}  # fmt: skip

    def fact(h, fid, subject, level, status, **kw):
        return bl.fact_row(Fact(fact_id=f"{h}{fid}{status}", hotel_id=h, city_id=city,
                                subject_level=level, subject_id=subject, feature_id=fid,
                                status=status, **kw))  # fmt: skip

    facts = [fact(hid[3], fid, b[5], "building", "computed", value={"distance_m": 5.0})
             for fid in ("N02", "N03")]  # fmt: skip
    facts += [fact(hid[k], fid, f"hotel:{hid[k]}", "hotel", "unknown",
                   unknown_reason="verified_hotel_building_footprint_missing_or_ambiguous")
              for k in (1, 2, 4) for fid in ("N02", "N03")]  # fmt: skip
    coverage = [{"city_id": city, "feature_id": fid, "n_hotels": 4, "computed": 0, "estimate": 0,
                 "unknown": 4, "not_applicable": 0} for fid in ("H03", "N02", "N03")]  # fmt: skip
    pack = packs / city
    (pack / "data").mkdir(parents=True)
    files = []
    for kind, rows in (("hotels", hotels), ("buildings", [old_building]),
                       ("hotel_buildings", [old_link]), ("facts", facts),
                       ("coverage", coverage)):  # fmt: skip
        path = pack / "data" / f"{kind}.jsonl.gz"
        write_jsonl_gz(path, rows)
        files.append(PackFile(path=f"data/{kind}.jsonl.gz", kind=kind, rows=len(rows),
                              bytes=path.stat().st_size, sha256=sha256_file(path)))  # fmt: skip
    write_manifest(
        pack,
        Manifest(
            city_id=city,
            city_name="Тест",
            timezone="Europe/Moscow",
            depth="full",
            pack_version="test.1",
            built_at=datetime(2026, 10, 28, tzinfo=UTC),
            files=files,
        ),
    )
    buildings = [
        way(base + 1, LON, LAT, **LENINA_5, **{"building:levels": "9"}),
        way(base + 2, LON + 0.01, LAT),
        way(base + 3, LON + 0.01 + D, LAT),
        way(base + 5, LON - 0.02, LAT),
        way(base + 6, LON + 0.02, LAT, **{"addr:street": "улица Ленина",
                                          "addr:housenumber": "11"}),
    ]  # fmt: skip
    road = [
        line(base + 10, LAT + 0.001, highway="primary"),
        line(base + 11, LAT + 0.0003, highway="residential"),
    ]
    rail = [line(base + 20, LAT - 0.002, railway="rail")]
    return hid, b, buildings, road, rail


def build(tmp_path, city="bltest", base=0):
    packs, raw = tmp_path / "packs", tmp_path / "raw"
    hid, b, buildings, road, rail = write_pack(packs, city, base)
    write_raw(raw, city, buildings, road, rail)
    return hid, b, packs, raw, bl.build_city(raw, city, packs)


def test_build_city_writes_layers_without_touching_existing(tmp_path):
    hid, b, packs, raw, stats = build(tmp_path)
    pack = packs / "bltest"
    m = read_manifest(pack)
    layers = {f.path: f for f in m.files if f.layer}
    assert set(layers) == set(bl.OWN_LAYERS)
    assert stats["outcomes"] == {"linked": 1, "several_footprints": 1, "address_conflict": 1}
    [link] = iter_jsonl(pack / bl.LINKS)
    assert link["hotel_id"] == hid[1] and link["building_id"] == b[1]
    assert link["method"] == "auto_point" and link["evidence"]["point_covered_by_building"]
    # Прежняя связь и прежний корпус — в своих файлах, в слой не повторяются.
    assert [x["hotel_id"] for x in iter_jsonl(pack / "data/hotel_buildings.jsonl.gz")] == [hid[3]]
    [building] = iter_jsonl(pack / bl.BUILDINGS)
    assert building["id"] == b[1] and building["geometry"]["type"] == "MultiPolygon"
    assert building["tags"]["placement_catalog"]["building_census_complete"] is False
    facades = list(iter_jsonl(pack / bl.FACADES))
    assert len(facades) == 4 and {f["building_id"] for f in facades} == {b[1]}
    facts = list(iter_jsonl(pack / bl.FACTS))
    for row in facts:
        Fact.model_validate(row)
    got = {(r["hotel_id"], r["feature_id"]): r for r in facts}
    assert got[(hid[1], "H03")]["value"]["aboveground_levels"] == 9
    assert got[(hid[3], "H03")]["value"]["aboveground_levels"] == 4  # прежней связи — тоже
    assert got[(hid[1], "N02")]["status"] == "computed"
    assert 86 < got[(hid[1], "N02")]["value"]["distance_m"] < 90  # residential — не класс N02
    assert 197 < got[(hid[1], "N03")]["value"]["distance_m"] < 201
    for k in (2, 4):
        for fid in ("N02", "N03"):
            assert got[(hid[k], fid)]["status"] == "estimate"
    # У отеля 3 N02 / N03 уже посчитаны — ни повтора, ни оценки.
    assert (hid[3], "N02") not in got and (hid[3], "N03") not in got
    assert len({r["fact_id"] for r in facts}) == len(facts)
    cov = stats["coverage_after"]
    assert cov["N02"] == {"computed": 2, "estimate": 2, "unknown": 0, "not_applicable": 0}
    assert cov["H03"]["computed"] == 2 and cov["H03"]["unknown"] == 2
    # Повторная сборка по тому же сырью — тот же пакет (слои пересобираются, не копятся).
    before = sha256_file(pack / "manifest.json")
    bl.build_city(raw, "bltest", packs)
    assert sha256_file(pack / "manifest.json") == before


def test_build_refuses_tampered_raw_and_pack_audit_passes(tmp_path):
    from pipeline.audit_packs import audit_pack

    hid, b, packs, raw, _ = build(tmp_path)
    result, _ = audit_pack(packs / "bltest")
    assert result["status"] == "passed", result
    assert result["building_linked_hotels"] == 2
    target = raw / "bltest" / "roads_000.json.gz"
    target.write_bytes(target.read_bytes() + b"\0")
    with pytest.raises(PackError, match="sha256"):
        bl.build_city(raw, "bltest", packs)


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="нужна PostGIS")
def test_layers_of_buildings_and_links_load_into_db(tmp_path, monkeypatch):
    """Загрузчик берёт все файлы одного вида: корпуса и связи пакета и слоя вместе."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.engine import make_url

    from core.db import Building, Facade, FactRow, HotelBuilding
    from pipeline.load_packs import load_pack

    url = os.environ["TEST_DATABASE_URL"]
    assert make_url(url).database.endswith("_test")
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    city = "bltest_" + uuid4().hex[:12]
    base = int(uuid4().int % 10**12) * 100  # свои id корпусов и отелей у каждого прогона
    hid, b, packs, raw, _ = build(tmp_path, city, base)
    engine = create_engine(url)
    with engine.begin() as conn:
        assert "загружено" in load_pack(conn, packs / city)
        links = conn.execute(
            select(HotelBuilding.hotel_id, HotelBuilding.method).where(
                HotelBuilding.hotel_id.in_(hid.values())
            )
        ).all()
        assert sorted(links) == sorted([(hid[1], "auto_point"), (hid[3], "auto")])
        assert conn.scalar(
            select(func.count()).select_from(Building).where(Building.city_id == city)
        ) == 2  # fmt: skip
        assert conn.scalar(
            select(func.count()).select_from(Facade).where(Facade.building_id == b[1])
        ) == 4  # fmt: skip
        statuses = conn.execute(
            select(FactRow.status).where(FactRow.city_id == city, FactRow.feature_id == "N02")
        ).scalars()
        assert sorted(statuses) == ["computed", "computed", "estimate", "estimate", "unknown",
                                    "unknown", "unknown"]  # fmt: skip
    engine.dispose()


def test_extract_from_pbf_keeps_only_buildings_under_hotels_and_target_lines(tmp_path):
    """Срез PBF → то же сырьё, что из Overpass; паспорт фактов — sha256 самого PBF."""
    import osmium
    from osmium.osm.mutable import Node, Way

    packs, raw = tmp_path / "packs", tmp_path / "raw"
    hid, b, *_ = write_pack(packs, "bltest", 0)
    pbf = tmp_path / "city.osm.pbf"
    stamp = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    nodes, ways, nid = [], [], 1

    def ring(wid, lon, lat, tags, d=D):
        nonlocal nid
        ids = []
        for x, y in (
            (lon - d, lat - d),
            (lon + d, lat - d),
            (lon + d, lat + d),
            (lon - d, lat + d),
        ):
            nodes.append(Node(id=nid, location=(x, y), version=1, timestamp=stamp))
            ids.append(nid)
            nid += 1
        ways.append(Way(id=wid, nodes=[*ids, ids[0]], tags=tags, version=2, timestamp=stamp))

    def axis(wid, lat, tags):
        nonlocal nid
        nodes.append(Node(id=nid, location=(LON - 0.05, lat), version=1, timestamp=stamp))
        nodes.append(Node(id=nid + 1, location=(LON + 0.05, lat), version=1, timestamp=stamp))
        ways.append(Way(id=wid, nodes=[nid, nid + 1], tags=tags, version=1, timestamp=stamp))
        nid += 2

    ring(1, LON, LAT, {"building": "yes", **LENINA_5, "building:levels": "9"})
    ring(7, LON + 0.03, LAT, {"building": "yes"})  # ни одного отеля внутри — не нужен
    axis(10, LAT + 0.001, {"highway": "primary"})
    axis(11, LAT + 0.0003, {"highway": "residential"})  # не класс N02
    axis(20, LAT - 0.002, {"railway": "rail"})
    with osmium.SimpleWriter(str(pbf)) as writer:
        for n in nodes:
            writer.add_node(n)
        for w in ways:
            writer.add_way(w)
    m = bl.extract_pbf(raw, "bltest", pbf, packs)
    got = {f["kind"]: f for f in m["files"]}
    assert {k: f["elements"] for k, f in got.items()} == {"buildings": 1, "roads": 1, "rail": 1}
    assert got["buildings"]["source_sha256"] == sha256_file(pbf)
    assert not (raw / "bltest" / "nodes.idx").exists()  # файловый индекс узлов удалён
    with pytest.raises(PackError, match="новая папка"):
        bl.extract_pbf(raw, "bltest", pbf, packs)
    stats = bl.build_city(raw, "bltest", packs)
    assert stats["new_links"] == 1 and stats["raw"]["provider"] == "OpenStreetMap/Geofabrik"
    facts = {(r["hotel_id"], r["feature_id"]): r for r in iter_jsonl(packs / "bltest" / bl.FACTS)}
    n02 = facts[(hid[1], "N02")]
    assert n02["status"] == "computed" and 86 < n02["value"]["distance_m"] < 90
    assert n02["sources"][0]["dataset_sha256"] == sha256_file(pbf)
    assert facts[(hid[1], "H03")]["value"]["aboveground_levels"] == 9
