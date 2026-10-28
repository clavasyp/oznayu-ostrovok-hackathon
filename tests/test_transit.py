"""Время на транспорте по 2ГИС на запросе (core/transit.py): только первая страница, только
с ключом; ответ 2ГИС — лучший маршрут; сбой сети — факта нет, пункт «неизвестно»."""

from agents.search import parse_query
from app.stub_store import StubStore
from connectors.twogis import TwoGISError
from core import transit
from core.search import search
from core.verdict import GuestStatus

HOTELS = {
    h: {"hotel_id": h, "city_id": "sochi", "name": h, "latitude": lat, "longitude": 39.9}
    for h, lat in (("direct", 43.43), ("change", 43.58), ("down", 43.60))
}


class FakeClient:
    """Маршрут по широте отеля: без пересадок, с пересадкой или «2ГИС не ответил»."""

    def request(self, action, body):
        lat = body["target"]["point"]["lat"]
        if lat == 43.60:
            raise TwoGISError("нет сети", kind="tls_timeout")
        transfers = 0 if lat == 43.43 else 1
        duration = 1800 + 900 * transfers
        route = {"pedestrian": False, "transport": ["bus"], "total_duration": duration,
                 "transfer_count": transfers}  # fmt: skip
        return {"response": [route], "source": {"provider": "2gis", "url": "x"}}


def test_page_hotels_get_transit_facts_and_failures_stay_unknown(monkeypatch):
    monkeypatch.setenv("TWOGIS_API_KEY", "test")
    monkeypatch.setattr(transit, "TwoGISClient", FakeClient, raising=False)
    real = transit.transit_facts
    monkeypatch.setattr(
        "core.search.transit_facts", lambda *a, **k: real(*a, **k, client=FakeClient())
    )
    ast = parse_query("из аэропорта обязательно без пересадок", "sochi", mode="rules").ast
    by = {
        r["hotel"]["hotel_id"]: r for r in search(StubStore(hotel_by_id=HOTELS), ast, transit=True)
    }
    assert by["direct"]["status"] == GuestStatus.fits
    from agents.explain import build_claims
    from core.live import recent

    said = " ".join(c.text for c in build_claims(by["direct"], recent, ast=ast))
    assert "30 мин, без пересадок от «аэропорт Сочи», 2ГИС" in said
    assert by["change"]["status"] == GuestStatus.not_fit
    assert by["down"]["hard"][0]["reason_code"] == "transit_on_request"


def test_without_key_transit_is_honest_unknown(monkeypatch):
    monkeypatch.delenv("TWOGIS_API_KEY", raising=False)
    ast = parse_query("из аэропорта обязательно без пересадок", "sochi", mode="rules").ast
    (r, *_) = search(StubStore(hotel_by_id=HOTELS), ast, transit=True)
    assert r["hard"][0]["reason_code"] == "transit_no_key"


def test_origin_is_the_named_airport_or_the_main_one():
    assert transit.origin("moscow", "T01", "из Домодедово")[0] == "аэропорт Домодедово"
    assert transit.origin("moscow", "T01", "из аэропорта")[0] == "аэропорт Шереметьево"
    assert transit.origin("sochi", "T02", "с вокзала Адлер")[0] == "вокзал Адлер"


class FakeTripClient:
    """«Доехать до X»: от первого отеля — пешком 4 мин, от второго — 25 мин с пересадкой."""

    def request(self, action, body):
        lat = body["source"]["point"]["lat"]
        if lat == 43.60:
            raise TwoGISError("нет сети", kind="tls_timeout")
        if lat == 43.43:
            route = {"pedestrian": True, "total_duration": 240}
        else:
            route = {"pedestrian": False, "transport": ["metro"], "total_duration": 1500,
                     "transfer_count": 1}  # fmt: skip
        return {"response": [route], "source": {"provider": "2gis", "url": "x"}}


def test_trip_to_target_gets_2gis_route_time():
    """Маршрут 2ГИС от отеля до цели: время в пути — ответ «да», а не оценка по прямой; нет
    маршрута или сети — отеля нет в ответе, пункт остаётся оценкой."""
    from agents.catalog import load_catalog
    from agents.explain.explain import _condition_claim
    from agents.query_ast import OutOfCatalog
    from core.search import trip_item

    target = {"place_id": "p1", "name": "Большой театр", "latitude": 43.5, "longitude": 39.8}
    routes = transit.trip_routes(target, list(HOTELS.values()), client=FakeTripClient())
    assert routes["direct"]["walk_only"] and routes["direct"]["duration_min"] == 4
    assert routes["change"] == routes["change"] | {"duration_min": 25, "transfers": 1}
    assert "down" not in routes
    o = OutOfCatalog(text_span="доехать до Большого театра", normalized="Большого театра",
                     kind="missing_feature", atom_id="a1", requiredness="required",
                     nearest_contracts=["trip_to_target"])  # fmt: skip
    item = trip_item(o, target, 900, routes["change"])
    assert item["verdict"] == "yes" and item["reason_code"] == "trip_2gis"
    claim = _condition_claim("c1", item, load_catalog(), lambda _: None)
    assert "«Большой театр» — 25 мин на транспорте, пересадок: 1, 2ГИС" in claim.text
    assert trip_item(o, target, 900)["reason_code"] == "trip_estimate"
