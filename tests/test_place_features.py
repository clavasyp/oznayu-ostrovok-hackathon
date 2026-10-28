"""Характеристики каталога «место рядом» по местам пакета на запросе (D41, core/place_features).

Еда, аптека, супермаркет, ветклиника — открыто ли в момент гостя по часам OSM; выгул собак,
коворкинг, детская неотложка — есть ли место такого типа. «Да» — с паспортом места; «нет» не
говорим: часы не указаны — склоняется к «да», всё рядом закрыто или не нашли — к «нет»."""

import os
from datetime import date
from uuid import uuid4

import pytest

from agents.explain import build_claims
from agents.search import parse_query
from app.stub_store import StubStore
from core import place_features as pf
from core.facts import Status
from core.search import search
from core.verdict import GuestStatus, Verdict

NIGHT_FOOD = (
    "в пятницу 14 мая 2027 в час ночи обязательно должна быть открыта еда в пешей доступности"
)
TZ = [{"city_id": "sochi", "name": "Большой Сочи", "timezone": "Europe/Moscow"}]


def place(name, distance, **tags):
    return {"place_id": f"osm:node/{name}", "name": name, "distance_m": distance, "tags": tags,
            "source": {"method": "nearby-postgis.1", "passport_ref": "place.source"}}  # fmt: skip


def store(category, by_hotel):
    hotels = {h: {"hotel_id": h, "city_id": "sochi", "name": h} for h in by_hotel}
    return StubStore(hotel_by_id=hotels, city_list=TZ, places_near={category: by_hotel})


def run(text, s):
    ast = parse_query(text, "sochi", today=date(2026, 10, 28), mode="rules").ast
    return {r["hotel"]["hotel_id"]: r for r in search(s, ast)}, ast


def item(result):
    return (result["hard"] or result["soft"])[0]


FOOD = {
    "open": [
        place("Кафе", 100, amenity="cafe", opening_hours="Mo-Su 09:00-22:00"),
        place("Шаурма у вокзала", 240, amenity="fast_food", opening_hours="24/7"),
    ],
    "hours_unknown": [
        place("Столовая", 200, amenity="cafe"),
        place("Кафе у моря", 90, amenity="cafe", opening_hours="Mo-Su 09:00-22:00"),
    ],
    "closed": [place("Ресторан", 150, amenity="restaurant", opening_hours="Mo-Su 09:00-22:00")],
    "bar": [place("Бар", 150, amenity="bar", opening_hours="24/7")],
    "none": [],
}


def test_open_food_at_the_moment_is_yes_with_passport():
    results, ast = run(NIGHT_FOOD, store("L02", FOOD))
    hit = results["open"]
    assert hit["status"] == GuestStatus.fits
    food = item(hit)
    assert food["feature_id"] == "L02" and food["verdict"] == Verdict.yes
    assert food["statuses"] == [Status.computed]  # расчёт по открытым данным закрывает обязательное
    # Ближайшее кафе в час ночи закрыто — в ответе открытая шаурма, хотя она дальше.
    p = food["place"]
    assert (p["name"], p["distance_m"], p["place_id"]) == (
        "Шаурма у вокзала",
        240,
        "osm:node/Шаурма у вокзала",
    )
    hours = p["hours"]
    assert hours["state"] == "open" and hours["opening_hours"] == "24/7"
    assert hours["moment"] == {"arrival_local": "2027-05-14T01:00:00"}
    assert hours["timezone"] == "Europe/Moscow"
    assert food["nearby"] == {"category": "L02", "radius_m": 500}
    claims = " ".join(c.text for c in build_claims(hit, lambda _: None, ast=ast))
    assert "да («Шаурма у вокзала», 240 м по прямой, открыто: 24/7)" in claims


def test_hours_unknown_leans_yes_and_closed_leans_no():
    results, ast = run(NIGHT_FOOD, store("L02", FOOD))
    unknown = item(results["hours_unknown"])
    # Закрытое кафе ближе, но часов у столовой нет: «неизвестно», склоняется к «да».
    assert unknown["reason_code"] == "place_hours_unknown" and unknown["leaning"] == Verdict.yes
    assert unknown["place"]["name"] == "Столовая"
    closed = item(results["closed"])
    assert closed["verdict"] == Verdict.unknown  # «нет» не говорим: карты — не перепись
    assert closed["reason_code"] == "place_closed_at_moment" and closed["leaning"] == Verdict.no
    claims = " ".join(c.text for c in build_claims(results["closed"], lambda _: None, ast=ast))
    assert "«Ресторан», 150 м по прямой, часы: Mo-Su 09:00-22:00" in claims
    bar = item(results["bar"])  # бар открыт, а кормят ли — часов кухни нет
    assert bar["reason_code"] == "place_kitchen_unknown" and bar["leaning"] == Verdict.yes
    none = item(results["none"])
    assert none["reason_code"] == "place_not_found" and none["leaning"] == Verdict.no
    assert {results[h]["status"] for h in FOOD if h != "open"} == {GuestStatus.needs_check}
    # Порядок: подходит → склоняется к «да» → закрыто или не нашли.
    order = list(results)
    assert order[0] == "open" and order.index("hours_unknown") < order.index("closed")
    assert order.index("bar") < order.index("none")


def test_no_places_of_the_kind_in_city_is_no_data():
    results, _ = run(NIGHT_FOOD, store("L02", {"open": []}))
    assert item(results["open"])["reason_code"] == "place_no_data"


def test_far_on_foot_and_kitchen_hours():
    far = {"h": [place("Пицца", 450, amenity="restaurant", opening_hours="24/7")]}
    results, _ = run(NIGHT_FOOD, store("L02", far))
    # 450 м по прямой × 1,3 — дальше 500 м пешком: открыто, но дойти — не факт.
    assert item(results["h"])["reason_code"] == "place_far_on_foot"
    kitchen = {
        "h": [
            place(
                "Бар",
                100,
                amenity="bar",
                opening_hours="24/7",
                **{"opening_hours:kitchen": "Mo-Su 12:00-23:00"},
            )
        ]  # fmt: skip
    }
    results, _ = run(NIGHT_FOOD, store("L02", kitchen))
    assert item(results["h"])["reason_code"] == "place_closed_at_moment"  # кухня закрыта


def test_night_without_date_checks_every_night():
    pharmacies = {
        "always": [place("Аптека 24", 300, amenity="pharmacy", opening_hours="24/7")],
        "weekdays": [place("Аптека", 300, amenity="pharmacy", opening_hours="Mo-Fr 00:00-24:00")],
        "day": [place("Аптека днём", 300, amenity="pharmacy", opening_hours="Mo-Su 08:00-21:00")],
    }
    results, ast = run("аптека, открытая ночью рядом", store("L01", pharmacies))
    (cond,) = ast.soft
    assert cond.feature_id == "L01" and cond.params == {"time_local": "02:00"}
    assert item(results["always"])["verdict"] == Verdict.yes
    weekdays = item(results["weekdays"])  # открыто не каждую ночь недели
    assert weekdays["reason_code"] == "place_hours_vary" and weekdays["leaning"] == Verdict.yes
    assert item(results["day"])["reason_code"] == "place_closed_at_moment"


def test_without_moment_any_place_of_the_kind_counts():
    results, _ = run(
        "аптека рядом", store("L01", {"h": [place("Аптека", 300, amenity="pharmacy")]})
    )
    found = item(results["h"])
    assert found["verdict"] == Verdict.yes and "hours" not in found["place"]


def test_dog_area_and_coworking_need_public_access():
    dogs = {
        "public": [place("Площадка для собак", 400, leisure="dog_park")],
        "private": [place("Площадка ЖК", 100, leisure="dog_park", access="private")],
    }
    results, ast = run("рядом площадка для выгула собак", store("P03", dogs))
    (cond,) = ast.soft
    assert (cond.feature_id, cond.value, cond.params) == ("P03", 1000, {"animal": "dog"})
    assert item(results["public"])["verdict"] == Verdict.yes
    assert item(results["private"])["reason_code"] == "place_not_found"  # во двор ЖК не пустят
    desks = {"h": [place("Рабочая станция", 500, amenity="coworking_space")]}
    results, ast = run("нужен коворкинг рядом", store("R04", desks))
    (cond,) = ast.hard
    assert cond.feature_id == "R04" and cond.params == {}
    assert results["h"]["status"] == GuestStatus.fits


def test_child_emergency_is_confirmed_by_tags_or_name_never_guessed():
    hospitals = {
        "tags": [
            place(
                "Больница",
                900,
                amenity="hospital",
                emergency="yes",
                **{"healthcare:speciality": "emergency;paediatrics"},
            )
        ],  # fmt: skip
        "name": [
            place("НИИ неотложной детской хирургии и травматологии", 1500, amenity="hospital")
        ],
        "children": [place("Детская больница", 800, amenity="hospital", healthcare="hospital")],
        "clinic": [place("Детская поликлиника", 300, amenity="clinic")],
        "no_er": [place("Детская больница", 300, amenity="hospital", emergency="no")],
        "adult": [place("Городская больница", 200, amenity="hospital", emergency="yes")],
    }
    results, ast = run("рядом детская больница с неотложкой", store("hospital", hospitals))
    (cond,) = ast.soft
    assert (cond.feature_id, cond.value) == ("F02", 3000)
    assert item(results["tags"])["verdict"] == Verdict.yes
    assert item(results["name"])["verdict"] == Verdict.yes
    children = item(results["children"])  # детская больница, а неотложка не отмечена
    assert children["reason_code"] == "place_emergency_unconfirmed"
    assert children["leaning"] == Verdict.yes
    for h in ("clinic", "no_er", "adult"):  # поликлиника, «emergency=no», взрослая — не она
        assert item(results[h])["reason_code"] == "place_not_found"


def test_example_phrases_parse_into_catalog_conditions():
    def conds(text):
        ast = parse_query(text, "sochi", today=date(2026, 10, 28), mode="rules").ast
        assert not ast.unparsed_spans and not ast.out_of_catalog
        return [(c.feature_id, c.params) for c in [*ast.hard, *ast.soft]], ast

    (food,), ast = conds(NIGHT_FOOD)
    assert food == ("L02", {"arrival_local": "2027-05-14T01:00:00"}) and ast.hard
    assert conds("рядом площадка для выгула собак")[0] == [("P03", {"animal": "dog"})]
    assert conds("нужен коворкинг рядом")[0] == [("R04", {})]
    assert conds("рядом детская больница с неотложкой")[0] == [("F02", {})]
    assert conds("аптека, открытая ночью рядом")[0] == [("L01", {"time_local": "02:00"})]
    shop = "Сочи, 14 мая 2027 в 23:30 открытый супермаркет рядом"
    assert conds(shop)[0] == [("L03", {"arrival_local": "2027-05-14T23:30:00"})]
    # Ветклиника нужна не в момент приезда: время поездки ей не параметр, «круглосуточная» — да.
    assert conds("ветклиника рядом, приезжаем 14 мая 2027 в 02:00")[0] == [("P04", {})]
    assert conds("круглосуточная ветклиника")[0] == [("P04", {"time_local": "02:00"})]
    # «Детская площадка» — по-прежнему F01, «с собакой» — P01.
    assert conds("детская площадка рядом")[0][0][0] == "F01"
    assert [f for f, _ in conds("мы с собакой, где гулять с собакой рядом")[0]] == ["P01", "P03"]


def test_classify_ranks_and_hours():
    spec = pf.FEATURES["L02"]
    moment = {"arrival_local": "2027-05-14T01:00:00"}
    tz = {"sochi": "Europe/Moscow"}

    def rank(**tags):
        return pf.ranker(spec, moment, tz)({"city_id": "sochi", "tags": tags})

    assert rank(amenity="cafe", opening_hours="24/7") == pf.YES
    assert rank(amenity="cafe") == pf.HOURS_UNKNOWN
    assert rank(amenity="cafe", opening_hours="Mo-Su 09:00-22:00") == pf.CLOSED
    assert rank(amenity="cafe", opening_hours="24/7", access="private") is None
    # Пояс города неизвестен — часы не проверить: «неизвестно», а не «закрыто».
    found = pf.classify(spec, {"city_id": "x", "tags": {"opening_hours": "24/7"}}, moment, tz)
    assert found is not None and found[0] == pf.HOURS_UNKNOWN
    assert found[1]["reason"] == "city_timezone_unknown"


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="нужна PostGIS")
def test_ranked_nearest_in_postgis(monkeypatch):
    """В базе: ранг — по меткам места, запрос берёт лучшее по рангу, среди равных — ближайшее."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, delete, insert
    from sqlalchemy.engine import make_url

    from core.db import Building, City, Hotel, HotelBuilding, Place
    from core.geo_store import GeoStore

    url = os.environ["TEST_DATABASE_URL"]
    assert make_url(url).database.endswith("_test")
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url)
    city = "pf_test_" + uuid4().hex
    hid = city + "hotel"
    # Южная Атлантика, своя точка на каждый прогон: ни пакетов, ни мест других тестов рядом.
    lon, lat = -25 + uuid4().int % 1000 / 1000, -35.0
    point = f"SRID=4326;POINT({lon} {lat})"

    def row(suffix, north_m, **tags):
        return {"id": city + suffix, "city_id": city, "name": suffix, "longitude": lon,
                "latitude": lat + north_m / 111_320, "categories": ["L02"], "tags": tags,
                "source": {"provider": "OpenStreetMap"}}  # fmt: skip

    try:
        with engine.begin() as conn:
            conn.execute(insert(City), [{"id": city, "name": city, "timezone": "Europe/Moscow"}])
            hotel = {"id": hid, "city_id": city, "name": "h", "latitude": lat, "longitude": lon}
            conn.execute(insert(Hotel), [hotel | {"geom": point}])
            # Корпус самого отеля нанесён как кафе: в ответ отелю он не попадает.
            building = {"id": city + "b", "city_id": city, "geom": point, "tags": {}}
            conn.execute(insert(Building), [building])
            link = {"hotel_id": hid, "building_id": city + "b", "method": "auto", "evidence": {}}
            conn.execute(insert(HotelBuilding), [link])
            conn.execute(
                insert(Place),
                [
                    row("closed", 50, amenity="cafe", opening_hours="Mo-Su 09:00-22:00"),
                    row("unknown", 120, amenity="cafe"),
                    row("open", 300, amenity="fast_food", opening_hours="24/7"),
                    row("private", 20, amenity="cafe", opening_hours="24/7", access="private"),
                    row("own", 5, amenity="cafe", opening_hours="24/7")
                    | {"id": "osm:" + city + "b"},
                ],
            )
        geo = GeoStore(engine)
        moment = {"arrival_local": "2027-05-14T01:00:00"}
        rank = pf.ranker(pf.FEATURES["L02"], moment, {city: "Europe/Moscow"})
        found, known = geo.nearest([hid], "L02", 500, rank)
        assert known == 4  # частное кафе — не место для гостя
        best = found[hid]
        assert best["place_id"] == city + "open" and best["rank"] == pf.YES
        assert 295 < best["distance_m"] < 305 and best["tags"]["opening_hours"] == "24/7"
        found, _ = geo.nearest([hid], "L02", 200, rank)  # открытое дальше радиуса
        assert found[hid]["place_id"] == city + "unknown" and found[hid]["rank"] == pf.HOURS_UNKNOWN
        found, known = geo.nearest([hid], "L02", 500, lambda p: None)
        assert (found, known) == ({}, 0)
        plain, total = geo.nearest([hid], "L02", 500)  # без ранга — как раньше: ближайшее
        assert plain[hid]["place_id"] == city + "private" and total == 5
    finally:
        with engine.begin() as conn:
            conn.execute(delete(Place).where(Place.city_id == city))
            conn.execute(delete(HotelBuilding).where(HotelBuilding.hotel_id == hid))
            conn.execute(delete(Building).where(Building.city_id == city))
            conn.execute(delete(Hotel).where(Hotel.city_id == city))
            conn.execute(delete(City).where(City.id == city))
