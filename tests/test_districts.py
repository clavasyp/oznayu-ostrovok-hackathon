"""Районы Москвы у отелей (`pipeline.build_districts`) и в разборе запроса."""

import json
from datetime import UTC, datetime

from agents.search import parse_query
from agents.search.rules import AREA_NAMES, MOSCOW_AREA_LIST, area_city, areas
from core.packs import (
    Manifest,
    PackFile,
    iter_jsonl,
    read_manifest,
    sha256_file,
    verify,
    write_jsonl_gz,
    write_manifest,
)
from pipeline import build_districts as bd


def square(lon, lat, d=0.01):
    return {"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat], [lon + d, lat + d],
                                                [lon, lat + d], [lon, lat]]]}  # fmt: skip


def district(rel, name, okrug, lon, lat):
    return {"type": "Feature", "geometry": square(lon, lat),
            "properties": {"osm_relation": rel, "name": name, "okrug": okrug,
                           "okrug_relation": 1}}  # fmt: skip


def write_inputs(tmp_path):
    pack = tmp_path / "moscow"
    (pack / "data").mkdir(parents=True)
    hotels = [
        {"id": "1", "city_id": "moscow", "name": "А", "latitude": 55.735, "longitude": 37.575,
         "source": {"dataset_sha256": "x"}, "subzone": None, "subzone_id": None},
        {"id": "2", "city_id": "moscow", "name": "Б", "latitude": 55.745, "longitude": 37.645,
         "subzone": None, "subzone_id": None},
        {"id": "3", "city_id": "moscow", "name": "В", "latitude": 55.98, "longitude": 37.41,
         "subzone": None, "subzone_id": None},
    ]  # fmt: skip
    path = pack / "data/hotels.jsonl.gz"
    write_jsonl_gz(path, hotels)
    files = [PackFile(path="data/hotels.jsonl.gz", kind="hotels", rows=3,
                      bytes=path.stat().st_size, sha256=sha256_file(path))]  # fmt: skip
    built = datetime(2026, 10, 28, tzinfo=UTC)
    write_manifest(pack, Manifest(city_id="moscow", city_name="Москва", timezone="Europe/Moscow",
                                  depth="full", pack_version="t-0", code_version="t",
                                  built_at=built, files=files))  # fmt: skip
    raw = tmp_path / "raw"
    raw.mkdir()
    doc = {"type": "FeatureCollection", "features": [
        district(10, "район Хамовники", "Центральный административный округ", 37.57, 55.73),
        district(11, "Таганский район", "Центральный административный округ", 37.64, 55.74),
    ]}  # fmt: skip
    (raw / "districts.geojson").write_text(json.dumps(doc, ensure_ascii=False), "utf-8")
    meta = {"pbf_sha256": "p" * 64, "source_date": "2026-10-23T20:21:00Z",
            "districts_sha256": sha256_file(raw / "districts.geojson")}  # fmt: skip
    (raw / "meta.json").write_text(json.dumps(meta), "utf-8")
    return pack, raw


def test_apply_assigns_districts_and_versions_the_pack(tmp_path):
    """Отель в полигоне района — район и код; за границей Москвы (у Шереметьево) — без района.
    Файл отелей, источник и версия пакета меняются на месте; повтор даёт те же байты."""
    pack, raw = write_inputs(tmp_path)
    areas_file = tmp_path / "moscow_areas.json"
    bd.apply(raw, pack, areas_file)
    rows = {h["id"]: h for h in iter_jsonl(pack / "data/hotels.jsonl.gz")}
    assert (rows["1"]["subzone"], rows["1"]["subzone_id"]) == ("Хамовники", "khamovniki")
    assert (rows["2"]["subzone"], rows["2"]["subzone_id"]) == ("Таганский", "taganskiy")
    assert rows["3"]["subzone_id"] is None and "district" not in rows["3"]["source"]
    assert rows["1"]["source"]["dataset_sha256"] == "x"  # прежний паспорт отеля сохранён
    assert rows["1"]["source"]["district"]["okrug"] == "ЦАО"
    m = verify(pack)
    assert m.pack_version.startswith("t-") and m.pack_version != "t-0"
    assert [s.id for s in m.sources] == ["districts_osm"]
    listed = json.loads(areas_file.read_text("utf-8"))
    assert [(a["id"], a["okrug"]) for a in listed] == [("taganskiy", "ЦАО"), ("khamovniki", "ЦАО")]

    def state():
        return (pack / "data/hotels.jsonl.gz").read_bytes(), read_manifest(pack).pack_version

    before = state()
    bd.apply(raw, pack, areas_file)
    assert state() == before


def test_slug_and_short_names():
    assert bd.short_name("район Хорошёво-Мнёвники") == "Хорошёво-Мнёвники"
    assert bd.slug("Хорошёво-Мнёвники") == "khoroshevo_mnevniki"
    assert bd.short_name("Басманный район") == "Басманный"
    assert bd.slug("Замоскворечье") == "zamoskvoreche"
    assert bd.okrug_short("Юго-Западный административный округ") == "ЮЗАО"


def test_moscow_areas_from_guest_words():
    """«В Хамовниках», «на Арбате», «в Басманном районе», «в ЦАО» — районы Москвы; «рядом с
    Арбатом» — расстояние, а не район; «в районе аэропорта» — не район «Аэропорт»."""
    assert MOSCOW_AREA_LIST and all(a["id"] in AREA_NAMES for a in MOSCOW_AREA_LIST)
    cao = {a["id"] for a in MOSCOW_AREA_LIST if a["okrug"] == "ЦАО"}
    assert len(cao) == 10

    def want(text, city="moscow"):
        return areas(text, city)[0]

    assert want("отель в Хамовниках с завтраком") == ["khamovniki"]
    assert want("на Арбате") == ["arbat"]
    assert want("в Басманном районе") == ["basmannyy"]
    assert want("в Замоскворечье, тихо") == ["zamoskvoreche"]
    assert want("в Марьиной Роще") == ["marina_roshcha"]
    assert set(want("в ЦАО")) == cao
    assert areas("не в ЦАО", "moscow")[1] and not want("не в ЦАО")
    assert want("рядом с Арбатом") == [] and want("на Новом Арбате") == []
    assert want("в районе аэропорта Внуково") == []
    assert want("в Адлере") == [] and areas("в Хамовниках", "sochi")[0] == []
    assert area_city(want("в Хамовниках")) == "moscow" and area_city(["adler"]) == "sochi"
    # Общее слово нескольких районов, первое слово двойного названия, как говорят в городе.
    assert want("в Бутово") == ["severnoe_butovo", "yuzhnoe_butovo"]
    assert want("в Северном Бутове") == ["severnoe_butovo"]
    assert want("в Марьино") == ["marino"] and want("у Марьиной рощи") == []
    assert want("в Тропарёво") == ["troparevo_nikulino"]
    assert want("на Таганке") == ["taganskiy"] and want("на Таганской площади") == []
    assert want("в Тверской области") == [] and want("на Тверской") == []
    assert want("у метро Аэропорт") == [] and want("в районе Сокол") == ["sokol"]


def test_moscow_district_query_sets_city_and_filter():
    ast = parse_query("тихий отель в Хамовниках", None, mode="rules").ast
    assert ast.city_id == "moscow" and ast.areas == ["khamovniki"]
    assert not [u.text for u in ast.unparsed_spans if "хамовник" in u.text.lower()]
