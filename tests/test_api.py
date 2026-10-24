import pytest
from fastapi.testclient import TestClient

from app.main import create_app

RADISSON = "13736470"


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as c:
        yield c


def ast(*hard, soft=(), city="sochi"):
    return {"raw_text": "тест", "city_id": city, "hard": list(hard), "soft": list(soft)}


def result_for(resp, hotel_id):
    return next(r for r in resp.json()["results"] if r["hotel"]["hotel_id"] == hotel_id)


def test_health_ok_after_start(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/live").status_code == 200


def test_health_503_before_ready():
    app = create_app()  # без запуска lifespan
    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    assert resp.json()["phase"] == "starting"


def test_search_track_example_is_honest(client):
    resp = client.post(
        "/api/search", json={"text": "тихий номер с видом на закат недалеко от моря"}
    )
    body = resp.json()
    assert body["stub"] and body["notes"]
    # Вид на закат (V05) пока нигде не посчитан →
    # «Нужно уточнить», а не «Подходит» и не «Не подходит».
    assert {r["status"] for r in body["results"]} == {"needs_check"}


def test_computed_fact_can_close_hard_condition(client):
    cond = {"feature_id": "N04", "op": "lte", "value": 100, "field": "count", "binding": "hotel"}
    r = result_for(client.post("/api/search", json={"ast": ast(cond)}), RADISSON)
    assert r["status"] == "fits" and r["status_label"] == "Подходит"
    assert r["hard"][0]["fact_ids"]


def test_estimate_does_not_close_hard_condition(client):
    cond = {
        "feature_id": "W01",
        "op": "lte",
        "value": 100000,
        "field": "distance_m",
        "binding": "hotel",
    }
    r = result_for(client.post("/api/search", json={"ast": ast(cond)}), RADISSON)
    assert r["status"] == "needs_check"
    assert "D18" in r["hard"][0]["reason"]


def test_valid_no_means_not_fit(client):
    cond = {"feature_id": "N04", "op": "lte", "value": -1, "field": "count", "binding": "hotel"}
    r = result_for(client.post("/api/search", json={"ast": ast(cond)}), RADISSON)
    assert r["status"] == "not_fit"


def test_estimate_counts_in_soft_ranking(client):
    soft = {"feature_id": "W01", "op": "lte", "value": 100000, "field": "distance_m", "weight": 0.5}
    r = result_for(client.post("/api/search", json={"ast": ast(soft=[soft])}), RADISSON)
    assert r["score"]["min"] == 0.5


def test_hotel_card_has_passports(client):
    body = client.get(f"/api/hotels/{RADISSON}").json()
    n02 = body["facts"]["N02"]
    assert n02 and all(f["sources"] for f in n02)
    assert client.get(f"/api/facts/{n02[0]['fact_id']}").status_code == 200


def test_cities_and_coverage(client):
    cities = {c["city_id"]: c for c in client.get("/api/cities").json()["cities"]}
    assert cities["sochi"]["depth"] == "full" and cities["moscow"]["depth"] == "full"
    assert len(client.get("/api/cities/sochi/coverage").json()["features"]) == 54


def test_hotelier_questions_and_labels(client):
    qs = client.get(f"/api/hotelier/{RADISSON}/questions").json()["questions"]
    assert len(qs) == 26
    labels = client.get("/api/meta/labels").json()
    assert labels["booking_imitation_note"].startswith("Имитация оформления Островка")
