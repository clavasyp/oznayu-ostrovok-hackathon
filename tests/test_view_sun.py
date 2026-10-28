"""Вид и солнце из окна (D40): расчёт лучей, сводка по окнам отеля, слой пакета Сочи.

Расчёт (calculators/view_sun.py) запускается при сборке пакета с группой builder; здесь —
логика итогов по окну без тяжёлых библиотек и то, как факты окна доходят до поиска.
"""

import gzip
import json
from datetime import UTC, datetime

import pytest

from agents.explain.explain import build_claims
from agents.query_ast import Condition, QueryAST, SoftCondition
from app.stub_store import StubStore
from calculators import view_sun as vs
from core.facts import Fact
from core.hotelier import AnswerIn, build_answer
from core.packs import PackError, PackSource, add_layer, keep_layers, read_manifest, verify
from core.search import facade_subject, search, window_subject
from core.verdict import GuestStatus
from pipeline.build_v9_packs import PACKS

HOTEL = {"hotel_id": "h1", "city_id": "sochi", "name": "Тест"}
DATE = {"date": "2026-11-14"}
SUNSET = Condition(feature_id="V05", op="eq", value=True, params=DATE)
n = 0


def fact(subject, value, status="computed", azimuth=225.0, level="facade_physical_floor",
         params=None):  # fmt: skip
    global n
    n += 1
    return Fact(
        fact_id=f"v{n}",
        hotel_id="h1",
        city_id="sochi",
        subject_level=level,
        subject_id=subject,
        feature_id="V05",
        params=({"last_minutes": 30} | DATE) if params is None else params,
        value=None if value is None else {"visible": value, "why": "clear" if value else "blocked"},
        status="unknown" if value is None and status == "computed" else status,
        unknown_reason="view_obstacle_height" if value is None else None,
        hard_condition_eligible=False if status == "estimate" else None,
        depends_on=[{"facade_azimuth_deg": azimuth}],
    )


def run(facts, room_types=None, cond=SUNSET):
    s = StubStore(
        hotel_by_id={"h1": HOTEL},
        by_hotel={"h1": {"V05": facts}},
        room_types_by_hotel={"h1": room_types} if room_types else {},
    )
    ast = QueryAST(raw_text="закат из окна", city_id="sochi", hard=[cond])
    return search(s, ast)[0]


def text_of(result, facts):
    by_id = {f.fact_id: f for f in facts}
    return " ".join(c.text for c in build_claims(result, by_id.get))


def test_visible_field_is_compared_on_placements():
    facts = [fact(window_subject("b", "e1", 7), True), fact(window_subject("b", "e1", 8), True)]
    rt = {
        "room_type_id": "sea",
        "name": "С видом",
        "placement_set": {"completeness": "complete", "guarantee": "all_guaranteed"},
        "placements": [
            {"placement_id": f"p{f}", "building_id": "b", "floor": f, "windows": ["e1"]}
            for f in (7, 8)
        ],
    }
    r = run(facts, [rt])
    assert r["status"] == GuestStatus.fits  # {"visible": true} — не «значение не сравнить»


def test_no_window_known_is_not_a_hotel_no():
    # Все посчитанные окна — «нет», но корпуса отеля известны не все: только «нужно уточнить».
    facts = [fact(facade_subject("b", "e0"), False, level="facade"),
             fact(window_subject("b", "e1", 1), False)]  # fmt: skip
    r = run(facts)
    item = r["hard"][0]
    assert r["status"] == GuestStatus.needs_check
    assert item["reason_code"] == "no_window_known" and item["fact_ids"]
    assert "не подходит ни одно окно известных нам корпусов" in text_of(r, facts)


def test_some_windows_yes_tells_where():
    facts = [
        fact(window_subject("b", "e1", 7), True),
        fact(window_subject("b", "e1", 8), True),
        fact(window_subject("b", "e1", 5), None),
        fact(window_subject("b", "e1", 5), True, status="estimate"),
        fact(window_subject("b", "e2", 5), False, azimuth=45.0),
    ]
    r = run(facts)
    item = r["hard"][0]
    assert r["status"] == GuestStatus.needs_check and item["reason_code"] == "window_some"
    assert item["windows"]["yes"] == {"ЮЗ": [7, 8]} and item["windows"]["no"] == {"СВ": [5]}
    assert item["windows"]["estimate"] == {"ЮЗ": [5]}
    assert set(item["fact_ids"]) == {facts[0].fact_id, facts[1].fact_id}  # только расчёт «да»
    assert "окна на юго-запад, этажи 7–8" in text_of(r, facts)


def test_estimate_does_not_close_hard_condition():
    facts = [fact(window_subject("b", "e1", 5), None),
             fact(window_subject("b", "e1", 5), True, status="estimate")]  # fmt: skip
    r = run(facts)
    item = r["hard"][0]
    assert r["status"] == GuestStatus.needs_check and item["reason_code"] == "estimate_only"
    assert item["windows"]["estimate"] == {"ЮЗ": [5]} and not item["windows"]["yes"]


def test_estimate_is_not_shown_as_computed_in_soft_condition():
    facts = [fact(window_subject("b", "e1", 5), None),
             fact(window_subject("b", "e1", 5), True, status="estimate")]  # fmt: skip
    s = StubStore(hotel_by_id={"h1": HOTEL}, by_hotel={"h1": {"V05": facts}})
    soft = SoftCondition(**SUNSET.model_dump(), weight=1.0)
    ast = QueryAST(raw_text="хорошо бы закат", city_id="sochi", soft=[soft])
    item = search(s, ast)[0]["soft"][0]
    assert not item["windows"]["yes"] and item["windows"]["estimate"] == {"ЮЗ": [5]}


def test_sunset_without_date_uses_only_all_year_facts():
    # На даты: «нет» (за стеной в ноябре); без даты — только факт «в любую дату».
    dated = [fact(window_subject("b", "e1", 3), False)]
    any_date = [fact(facade_subject("b", "e1"), None, level="facade", params={"last_minutes": 30})]
    undated = Condition(feature_id="V05", op="eq", value=True)
    r = run(dated + any_date, cond=undated)
    item = r["hard"][0]
    assert r["status"] == GuestStatus.needs_check and not item["windows"]["no"]
    assert run(dated + any_date)["hard"][0]["windows"]["no"] == {"ЮЗ": [3]}  # с датой — да
    other_day = SUNSET.model_copy(update={"params": {"date": "2026-11-20"}})
    assert run(dated, cond=other_day)["hard"][0]["reason_code"] == "no_fact_params"


def test_negated_condition_lists_windows_without_sunset():
    facts = [fact(window_subject("b", "e1", 7), True),
             fact(window_subject("b", "e2", 2), False, azimuth=45.0)]  # fmt: skip
    no_sunset = SUNSET.model_copy(update={"value": False, "text_span": "без заката"})
    r = run(facts, cond=no_sunset)
    text = text_of(r, facts)
    assert "нет — окна на северо-восток, этаж 2" in text and "юго-запад" not in text


def test_simulator_cannot_close_view_but_can_other_features():
    def answer(feature_id):
        q = {"question_id": 1, "hotel_id": "h1", "city_id": "sochi", "feature_id": feature_id}
        a = AnswerIn(question_id=1, answer_text="да", fields={"visible": True}, is_simulated=True)
        return build_answer(q, a, [], datetime(2026, 10, 26, tzinfo=UTC)).facts[0]

    assert answer("V05").usable_in_hard is False
    assert answer("S04").usable_in_hard is True  # кондиционер — вопрос отелю, не расчёт


# --- Итог по окну в расчёте (без тяжёлых библиотек) ---

OBS = vs.Obstacles(d=None, lo=None, hi=None, mid=None, kind=["osm_levels", "trees"],
                   ids=["way/1", "worldcover"])  # fmt: skip
GROUND = (-1.0, 1.0, 3.0)


def minute(e_clear, e_block, state="front"):
    return state, (e_clear, e_block, 0, 0, e_clear, 0, OBS)


def test_sunset_yes_all_minutes_no_none_partial_unknown():
    # 7-й этаж: глаз 18,6–25,8 м над землёй; с землёй −1…3 м — 17,6–28,8 м.
    clear, blocked = minute(10.0, 0.0), minute(40.0, 35.0)
    assert vs._v05_point([clear] * 3, 7, GROUND).visible is True
    no = vs._v05_point([blocked] * 3, 7, GROUND)
    assert no.visible is False and no.blocker == "way/1"
    # Видно часть последних 30 минут: «да» по одному прочтению, «нет» по другому — неизвестно.
    assert vs._v05_point([clear, blocked], 7, GROUND).reason == "view_sun_partial"
    assert vs._v05_point([("behind", None), clear], 7, GROUND).reason == "view_sun_partial"
    behind = vs._v05_point([("behind", None)] * 2, None, GROUND)
    assert behind.visible is False and behind.why == "sun_behind_facade"
    unsure = vs._v05_point([minute(20.0, 0.0)], 7, GROUND)
    assert unsure.visible is None and unsure.reason == "view_obstacle_height"
    assert vs._v05_point([minute(10.0, 0.0, "oblique")], 7, GROUND).reason == "view_sun_oblique"
    assert vs._v05_point([minute(-10.0, -20.0)], 2, GROUND).reason == "view_low_floor"
    assert vs._v05_point([clear], None, GROUND).reason == "floor_count_unknown"


def test_all_year_no_only_when_sun_is_behind_the_wall_all_year():
    winter = [(232.5, 4.0), (237.6, 0.1)]
    summer = [(299.2, 4.0), (304.1, 0.1)]
    assert vs.v05_all_year(winter, summer, 90.0).why == "sun_behind_facade_all_year"  # восток
    assert vs.v05_all_year(winter, summer, 270.0).reason == "view_need_dates"  # запад
    assert vs.v05_all_year(winter, summer, 160.0).reason == "view_need_dates"  # зимой видно


def sea(e_clear=1e9, e_block=-1e9, on=True):
    return vs.SeaRay(e_clear, e_block, 0, 0, OBS if on else None, on, e_clear)


def test_sea_no_only_without_sea_and_never_from_missing_data():
    assert vs._v01_point([sea(on=False)] * 3, 5, GROUND).why == "no_sea"
    missing = vs._v01_point([sea(on=False), sea(on=None)], 5, GROUND)
    assert missing.visible is None and missing.reason == "view_landcover_missing"
    assert vs._v01_point([sea(e_clear=10.0)], 7, GROUND).visible is True
    # Закрыто на всех лучах — всё равно «неизвестно»: щель между лучами не доказать.
    assert vs._v01_point([sea(e_block=40.0)] * 3, 7, GROUND).reason == "view_sea_blocked"


def test_window_answer_must_hold_along_the_facade():
    yes, no = vs.Cell(True, why="clear"), vs.Cell(False, why="blocked")
    assert vs._combine([yes, yes, yes]).visible is True
    assert vs._combine([yes, no, yes]).reason == "view_window_position"
    assert vs._combine([no, no, no]).visible is False


def test_heights_from_maps():
    assert vs.top_interval({"building:levels": "5"}, None) == (14.5, 24.0, "osm_levels")
    lo, hi, kind = vs.top_interval({}, 20.0)
    assert kind == "globfp" and (lo, hi) == pytest.approx((3.8, 36.2))
    assert vs.top_interval({}, None)[2] == "unmeasured"
    building = vs.top_interval({"building": "construction", "building:levels": "10"}, None)
    assert building[0] == float("-inf") and building[2] == "construction"  # «нет» по стройке нет
    curated = {"levels": "9", "checked_by": "lead", "checked_at": "2026-10-26", "source_url": "u"}
    assert vs.top_interval({"building:levels": "5"}, None, curated)[2] == "curated"


def test_curated_heights_need_a_checker(tmp_path):
    path = tmp_path / "h.csv"
    path.write_text("building_id,levels,source_url,checked_by,checked_at\nway/1,9,,,\n", "utf-8")
    with pytest.raises(ValueError, match="way/1"):
        vs.load_curated(path)
    path.write_text("building_id,levels,source_url,checked_by,checked_at\nway/1,,,,\n", "utf-8")
    assert vs.load_curated(path) == {}  # строка очереди без этажности — не данные


# --- Слой пакета ---


def test_sochi_pack_has_view_layer():
    pack = PACKS / "sochi"
    manifest = verify(pack)
    layer = next(f for f in manifest.files if f.path == "data/view_sun.jsonl.gz")
    assert layer.kind == "facts" and layer.layer and "+" in manifest.pack_version
    assert {s.id for s in manifest.sources if getattr(s, "layer", None) == layer.path} >= {
        "view_sun:osm-buildings",
        "view_sun:copdem-glo30",
        "view_sun:worldcover-2021",
    }
    seen, stamps = set(), set()
    with gzip.open(pack / layer.path, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            seen.add((r["feature_id"], r["status"], "date" in r["params"]))
            stamps.add(r["calculated_at"])
            assert "value" in r and r["subject_level"] in ("facade", "facade_physical_floor")
            if r["status"] == "computed":
                assert r["hard_condition_eligible"] is True
            if r["status"] == "estimate":
                assert r["hard_condition_eligible"] is False
            if r["subject_level"] == "facade" and r["depends_on"][0]["floors"]:
                # Факт на весь фасад — только если ответ не зависит от этажа.
                assert (r["value"] or {}).get("why") in vs.FLOOR_FREE or (
                    r["unknown_reason"] in vs.NO_ESTIMATE
                )
    assert {("V05", "computed", True), ("V05", "estimate", True), ("V01", "computed", False),
            ("V05", "computed", False)} <= seen  # fmt: skip
    assert len(stamps) == 1  # время расчёта — от входов: пересборка не меняет sha256


def manifest_with_layer(tmp_path):
    pack = tmp_path / "x"
    (pack / "data").mkdir(parents=True)
    (pack / "data" / "facts.jsonl.gz").write_bytes(b"")
    (pack / "data" / "layer.jsonl.gz").write_bytes(b"1")
    base = {
        "city_id": "x",
        "city_name": "X",
        "timezone": "UTC",
        "depth": "automatic",
        "pack_version": "v9-abc",
        "built_at": "2026-10-26T00:00:00Z",
        "sources": [{"id": "osm", "provider": "OpenStreetMap"}],
        "files": [{"path": "data/facts.jsonl.gz", "kind": "facts", "rows": 0, "bytes": 0,
                   "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}],
    }  # fmt: skip
    (pack / "manifest.json").write_text(json.dumps(base), "utf-8")
    add_layer(pack, "data/layer.jsonl.gz", "facts", 1, [PackSource(id="l:s", provider="p")])
    return pack


def test_layer_survives_pack_rebuild(tmp_path):
    pack = manifest_with_layer(tmp_path)
    with_layer = read_manifest(pack)
    assert with_layer.pack_version.startswith("v9-abc+") and with_layer.files[-1].layer
    v9 = with_layer.model_copy(update={"files": with_layer.files[:1], "pack_version": "v9-abc",
                                       "sources": with_layer.sources[:1]})  # fmt: skip
    rebuilt = keep_layers(pack, v9)
    assert [f.path for f in rebuilt.files] == ["data/facts.jsonl.gz", "data/layer.jsonl.gz"]
    assert rebuilt.pack_version == with_layer.pack_version
    assert [s.id for s in rebuilt.sources] == ["osm", "l:s"]
    (pack / "data" / "layer.jsonl.gz").unlink()  # слой без файла выпадает вместе с источниками
    gone = keep_layers(pack, v9)
    assert [f.path for f in gone.files] == ["data/facts.jsonl.gz"] and gone.pack_version == "v9-abc"
    assert [s.id for s in gone.sources] == ["osm"]


def test_layer_source_ids_do_not_clash(tmp_path):
    pack = manifest_with_layer(tmp_path)
    (pack / "data" / "other.jsonl.gz").write_bytes(b"2")
    with pytest.raises(PackError, match="l:s"):
        add_layer(pack, "data/other.jsonl.gz", "facts", 1, [PackSource(id="l:s", provider="p")])


def test_publish_keeps_layer(tmp_path, monkeypatch):
    from pipeline import publish_packs

    packs = tmp_path / "packs"
    packs.mkdir()
    target = manifest_with_layer(packs)
    target.rename(packs / "sochi")
    target = packs / "sochi"
    old = read_manifest(target)
    (target / "manifest.json").write_text(
        old.model_copy(update={"city_id": "sochi"}).model_dump_json(exclude_none=True), "utf-8"
    )
    built = tmp_path / "built"
    (built / "data").mkdir(parents=True)
    (built / "data" / "facts.jsonl.gz").write_bytes(b"")
    new = old.model_copy(update={"city_id": "sochi", "files": old.files[:1],
                                 "sources": old.sources[:1], "pack_version": "full-2"})  # fmt: skip
    (built / "manifest.json").write_text(new.model_dump_json(exclude_none=True), "utf-8")
    monkeypatch.setattr(publish_packs, "PACKS", packs)
    monkeypatch.setattr(publish_packs, "ROOT", packs)
    publish_packs.publish([built], tmp_path / "backup")
    published = verify(packs / "sochi")  # пересборка каталога не выкидывает слой вида и солнца
    assert [f.path for f in published.files] == ["data/facts.jsonl.gz", "data/layer.jsonl.gz"]
    assert published.pack_version.startswith("full-2+") and [s.id for s in published.sources] == [
        "osm",
        "l:s",
    ]


def test_window_summary_counts_each_wall():
    # Две стены одной стороны на одном этаже (фасады seg-…): окна считаются поштучно —
    # «нет» у обеих — «нет ни одного подходящего окна», а не падение сводки.
    facts = [
        fact(window_subject("b", "seg-1", 5), False),
        fact(window_subject("b", "seg-2", 5), False),
    ]
    assert run(facts)["hard"][0]["reason_code"] == "no_window_known"
    facts = [
        fact(window_subject("b", "seg-1", 5), True),
        fact(window_subject("b", "seg-2", 5), True),
    ]
    assert run(facts)["hard"][0]["reason_code"] == "window_all"


def test_category_without_placements_gets_window_summary():
    # У одной категории размещения есть, у другой нет: вторая — сводка по окнам корпусов,
    # а не «не знаем, где номера» (D17, D40).
    facts = [fact(window_subject("b", "e1", 7), True), fact(window_subject("b", "e1", 2), False)]
    placed = {
        "room_type_id": "h1:sea",
        "name": "С видом",
        "placement_set": {"completeness": "complete", "guarantee": "all_guaranteed",
                          "status": "hotelier", "source": {}},
        "placements": [{"placement_id": "h1:sea|b|2", "building_id": "b", "floor": 2,
                        "windows": ["e1"]}],
    }  # fmt: skip
    bare = {"room_type_id": "h1:std", "name": "Стандарт", "placement_set": None, "placements": []}
    cats = {c["room_type_id"]: c for c in run(facts, [placed, bare])["categories"]}
    assert cats["h1:sea"]["status"] == GuestStatus.not_fit
    (item,) = [c for c in cats["h1:std"]["conditions"] if c["feature_id"] == "V05"]
    assert item["reason_code"] == "window_some" and item["windows"]["yes"] == {"ЮЗ": [7]}


def test_needs_check_hotels_with_computed_yes_come_first():
    def hotel(hid, value):
        f = fact(window_subject(f"b{hid}", "e1", 5), value).model_copy(update={"hotel_id": hid})
        return {"hotel_id": hid, "city_id": "sochi", "name": hid}, f

    hotels = [hotel("no", False), hotel("guess", None), hotel("yes", True)]
    s = StubStore(
        hotel_by_id={h["hotel_id"]: h for h, _ in hotels},
        by_hotel={h["hotel_id"]: {"V05": [f]} for h, f in hotels},
    )
    ast = QueryAST(raw_text="закат", city_id="sochi", hard=[SUNSET])
    order = [r["hotel"]["hotel_id"] for r in search(s, ast)]
    assert order == ["yes", "guess", "no"]  # все «Нужно уточнить»: по расчёту «да» — первым


def test_window_summary_when_categories_have_no_placements():
    # После полного каталога у отелей есть категории Островка, но размещений нет (D17):
    # условие окна — по окнам корпусов, а не «нет данных о размещении».
    facts = [fact(window_subject("b", "e1", 7), True), fact(window_subject("b", "e1", 2), False)]
    rt = {"room_type_id": "h1:rg", "name": "Стандарт", "placement_set": None, "placements": []}
    r = run(facts, [rt])
    item = r["hard"][0]
    assert item["reason_code"] == "window_some" and item["windows"]["yes"] == {"ЮЗ": [7]}


def test_sun_hours_upper_bound_decides_only_one_side():
    """Часы солнца по фасаду — верхняя граница (соседей не считаем): «не больше часа» при
    границе 0,5 ч — «да», при 3 ч — «неизвестно», скорее «нет»; «не меньше часа» — наоборот."""
    from agents.query_ast import Condition
    from core.search import evaluate

    def hours(h):
        return Fact(fact_id=f"s{h}", hotel_id="1", city_id="sochi", subject_level="facade",
                    subject_id="way/1:seg-1", feature_id="S02",
                    value={"hours": h, "bound": "upper"},
                    status="computed", algorithm_version="sun-hours-upper@1",
                    params={"month": 7, "time_from": "07:00", "time_to": "11:00"})  # fmt: skip

    params = {"month": 7, "time_from": "07:00", "time_to": "11:00"}
    few = Condition(feature_id="S02", field="hours", op="lte", value=1, params=params)
    some = Condition(feature_id="S02", field="hours", op="gte", value=1, params=params)
    assert evaluate([hours(0.5)], few, hard=True)["verdict"] == "yes"
    open_few = evaluate([hours(3.0)], few, hard=True)
    assert open_few["verdict"] == "unknown" and open_few["leaning"] == "no"
    assert open_few["reason_code"] == "sun_hours_upper_bound"
    assert evaluate([hours(0.5)], some, hard=True)["verdict"] == "no"
    assert evaluate([hours(3.0)], some, hard=True)["leaning"] == "yes"
