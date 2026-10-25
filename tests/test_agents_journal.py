from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agents.search import parse_query
from agents.search.journal import query_id, redact, summarize, unmatched_events
from app.main import create_app

TODAY = date(2026, 10, 25)
ITALIAN = "Хорошо бы рядом итальянский ресторан."


def events(text, city_id="moscow", **kw):
    return unmatched_events(parse_query(text, city_id, today=TODAY, mode="rules").ast, **kw)


@pytest.fixture
def client():
    with TestClient(create_app()) as c:  # своё приложение: журнал в памяти у каждого теста
        yield c


def test_events_cover_out_of_catalog_and_unparsed_without_raw_text():
    text = "Приехал посмотреть Москву, звоните +7 999 123-45-67. " + ITALIAN
    evs = events(text)
    by_key = {e["normalized"]: e for e in evs}
    place = by_key["nearby_place:italian"]
    assert place["nearest_contracts"] == ["nearby_place"] and place["requiredness"] == "desired"
    assert all("999" not in e["span_text"] and "999" not in e["normalized"] for e in evs)
    assert any(e["atom_id"].startswith("unparsed") for e in evs)
    assert {e["query_id"] for e in evs} == {query_id(text, "moscow")}  # псевдоним, а не текст
    assert redact("пишите a.b@mail.ru") == "пишите …"


def test_frequency_is_distinct_queries():
    now = datetime.now(UTC)
    evs = events(ITALIAN, now=now) + events(ITALIAN, now=now)  # тот же запрос ещё раз
    evs += events("Чтобы рядом был итальянский ресторан, обязательно", now=now)
    evs += events("рядом музей", now=now + timedelta(minutes=1))
    rows = summarize(evs)
    top = rows[0]
    assert top["normalized"] == "nearby_place:italian" and top["queries"] == 2
    assert top["required_queries"] == 1 and top["label"].startswith("рядом: итальянский")
    assert top["cities"] == ["moscow"] and len(top["examples"]) == 2
    assert {r["normalized"] for r in rows} >= {"nearby_place:museum"}
    assert query_id("Рядом  Музей!", "sochi") == query_id("рядом музей", "sochi")


def test_search_writes_journal_and_admin_reads_it(client):
    for text in (ITALIAN, ITALIAN, "рядом итальянский ресторан и тихо"):
        assert (
            client.post("/api/search", json={"text": text, "city_id": "moscow"}).status_code == 200
        )
    ast = client.post("/api/search", json={"text": "рядом музей"}).json()["ast"]
    client.post("/api/search", json={"ast": ast})  # готовый AST — не новый запрос гостя
    body = client.get("/api/out-of-catalog", params={"city_id": "moscow"}).json()
    top = body["items"][0]
    assert body["stratum"] == "live" and top["normalized"] == "nearby_place:italian"
    assert top["queries"] == 2
    everywhere = client.get("/api/out-of-catalog").json()["items"]
    museum = next(r for r in everywhere if r["normalized"] == "nearby_place:museum")
    assert museum["queries"] == 1
    assert client.get("/api/out-of-catalog", params={"stratum": "synthetic"}).json()["items"] == []


def test_journal_failure_does_not_break_search(client, monkeypatch):
    def boom(events):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(client.app.state.store, "record_unmatched", boom)
    response = client.post("/api/search", json={"text": ITALIAN, "city_id": "moscow"})
    assert response.status_code == 200 and response.json()["results"]
