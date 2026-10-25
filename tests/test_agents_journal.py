from datetime import UTC, date, datetime, timedelta

from agents.search import parse_query
from agents.search.journal import query_id, redact, summarize, unmatched_events

TODAY = date(2026, 10, 25)
ITALIAN = "Хорошо бы рядом итальянский ресторан."


def events(text, city_id="moscow", **kw):
    return unmatched_events(parse_query(text, city_id, today=TODAY, mode="rules").ast, **kw)


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
