"""«Рядом место типа X» в поиске (D34): места пакета, PostGIS на запросе — здесь заглушка.

«Да» — место категории в радиусе с запасом на обход; «нет» не говорим (места — не перепись):
не нашли — «неизвестно», и такой отель ниже в выдаче."""

from agents.explain import build_claims
from agents.search import parse_query
from app.stub_store import StubStore
from core.search import search
from core.verdict import GuestStatus

HOTELS = {h: {"hotel_id": h, "city_id": "sochi", "name": h} for h in ("near", "far", "none")}


def place(name, distance):
    return {"place_id": f"osm:node/{name}", "name": name, "distance_m": distance,
            "source": {"method": "nearby-postgis.1"}}  # fmt: skip


def store(places=None):
    places = (
        {"near": place("Траттория", 300), "far": place("Пиццерия", 700)}
        if places is None
        else places
    )
    return StubStore(hotel_by_id=HOTELS, places_near={"italian": places} if places else {})


def run(text, s=None):
    ast = parse_query(text, "sochi", mode="rules").ast
    return {r["hotel"]["hotel_id"]: r for r in search(s or store(), ast)}, ast


def test_required_place_near_fits_with_place_and_distance():
    results, ast = run("обязательно рядом итальянский ресторан")
    near = results["near"]
    assert near["status"] == GuestStatus.fits
    (item,) = near["hard"]
    assert item["place"]["name"] == "Траттория" and item["place"]["distance_m"] == 300
    claims = " ".join(c.text for c in build_claims(near, lambda _: None, ast=ast))
    assert "«Траттория», 300 м по прямой" in claims


def test_place_not_found_is_unknown_not_no():
    results, _ = run("обязательно рядом итальянский ресторан")
    # 700 м по прямой — пешком может быть дальше 800 м; не нашли — не «нет»
    assert results["far"]["hard"][0]["reason_code"] == "place_far_on_foot"
    assert results["none"]["hard"][0]["reason_code"] == "place_not_found"
    assert {results[h]["status"] for h in ("far", "none")} == {GuestStatus.needs_check}
    order = [h for h in results]
    assert order == ["near", "far", "none"]  # «склоняется к да» выше, чем «не нашли»


def test_no_places_in_city_is_no_data():
    results, _ = run("обязательно рядом итальянский ресторан", store({}))
    assert results["near"]["hard"][0]["reason_code"] == "place_no_data"


def test_desired_place_ranks_and_quality_is_not_confirmed():
    ast = parse_query("отель рядом с хорошими итальянскими ресторанами", "sochi", mode="rules").ast
    (o,) = ast.out_of_catalog
    assert o.place.category == "italian" and o.place.quality == "хорошими"
    results = search(store(), ast)
    assert [r["hotel"]["hotel_id"] for r in results][0] == "near"
    item = results[0]["soft"][0]
    # Место есть, а «хорошее» ли — рейтингов нет: честное «неизвестно», а не «да»
    assert item["reason_code"] == "place_quality_unknown" and item["place"]["name"] == "Траттория"


def test_place_in_hotel_goes_to_hotelier():
    results, _ = run("обязательно ресторан в отеле")
    assert results["near"]["hard"][0]["reason_code"] == "place_on_site"


def test_trip_to_target_is_an_estimate_closer_first():
    """«Доехать до X» по своим данным: цель — место города по названию (с падежами), расстояние
    по прямой и пешком с запасом — оценка, время на транспорте не считаем; ближе — выше."""
    from agents.explain.explain import build_claims as claims_of
    from core.geo_store import target_match, target_stems

    assert target_match("Большой театр", target_stems("Большого театра"))
    assert not target_match("Красноказарменная площадь", target_stems("Красной площади"))
    theatre = {"place_id": "osm:node/1", "name": "Большой театр", "categories": ["theatre"],
               "latitude": 55.76, "longitude": 37.62,
               "distances": {"near": 150, "far": 9000}}  # fmt: skip
    shop = {"place_id": "osm:node/2", "name": "Сувенирный магазин Большого театра",
            "categories": ["souvenir"], "latitude": 55.7, "longitude": 37.6,
            "distances": {"near": 5, "far": 5, "none": 5}}  # fmt: skip
    s = StubStore(hotel_by_id={h: v | {"city_id": "moscow"} for h, v in HOTELS.items()},
                  targets={"moscow": [shop, theatre]})  # fmt: skip
    ast = parse_query("хочу легко доехать до Большого театра", "moscow", mode="rules").ast
    results = search(s, ast)
    assert [r["hotel"]["hotel_id"] for r in results] == ["near", "far", "none"]
    near = results[0]["hard"][0]
    assert near["trip"]["name"] == "Большой театр" and near["reason_code"] == "trip_estimate"
    assert {r["status"] for r in results} == {GuestStatus.needs_check}
    text = " ".join(c.text for c in claims_of(results[0], lambda _: None, ast=ast))
    assert "«Большой театр» — 0,1 км по прямой, пешком около 2 мин" in text
    missing = parse_query("доехать до Эрмитажа", "moscow", mode="rules").ast
    assert search(s, missing)[0]["hard"][0]["reason_code"] == "trip_target_not_found"


def test_amenity_in_the_hotel_is_checked_on_the_hotel_page():
    """«Обязательно бассейн» — про сам отель: строка удобств страницы — «да» с паспортом;
    сезонный — «уточним»; строки нет — «спросим отель», не «нет»."""
    from datetime import UTC, datetime

    from core.facts import Fact

    def onsite(hid, names, **extra):
        value = {"present": True, "names": names} | extra
        return Fact(fact_id=f"f{hid}", hotel_id=hid, city_id="sochi", subject_level="hotel",
                    subject_id=f"hotel:{hid}", feature_id="onsite", params={"category": "pool"},
                    value=value, status="ostrovok_content", hard_condition_eligible=True,
                    sources=[{"provider": "ostrovok.ru"}],
                    observed_at=datetime.now(UTC))  # fmt: skip

    s = StubStore(hotel_by_id=HOTELS, by_hotel={
        "near": {"onsite": [onsite("near", ["Открытый бассейн"])]},
        "far": {"onsite": [onsite("far", ["Сезонный открытый бассейн"], seasonal=True)]},
    })  # fmt: skip
    results, ast = run("обязательно бассейн", s)
    (o,) = ast.out_of_catalog
    assert o.place.on_site and o.requiredness == "required"
    assert results["near"]["status"] == GuestStatus.fits
    claims = " ".join(c.text for c in build_claims(results["near"], lambda _: None, ast=ast))
    assert "Островок: «Открытый бассейн»" in claims
    assert results["far"]["hard"][0]["reason_code"] == "place_on_site_seasonal"
    assert results["none"]["hard"][0]["reason_code"] == "place_on_site"
    assert [h for h in results] == ["near", "far", "none"]
