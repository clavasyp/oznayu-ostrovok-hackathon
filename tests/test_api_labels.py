"""Подписи для интерфейса: названия характеристик, `label` и `feature_name` у пунктов поиска,
вопрос с предзаполнением в ответах кабинета отельера."""

from fastapi.testclient import TestClient

from app.main import create_app

RADISSON = "13736470"
NIGHT = "заселиться ночью"


def cond(atom, feature, op, value, field, span):
    return {
        "atom_id": atom,
        "feature_id": feature,
        "op": op,
        "value": value,
        "field": field,
        "binding": "hotel",
        "text_span": span,
    }


def atom(a):
    return {"node_id": f"n_{a}", "op": "ATOM", "atom_id": a}


def search(c, hard, logic=(), root=None):
    ast = {"raw_text": "тест", "city_id": "sochi", "hard": hard, "logic": list(logic)}
    if root:
        ast["hard_root"] = root
    results = c.post("/api/search", json={"ast": ast}).json()["results"]
    return next(r for r in results if r["hotel"]["hotel_id"] == RADISSON)


def test_feature_names_for_screens():
    with TestClient(create_app()) as c:
        features = c.get("/api/meta/labels").json()["features"]
    assert features["P01"] == "можно с животным" and features["floor"] == "этаж номера"


def test_items_show_guest_words_and_feature_name():
    # «Без собак» правила переворачивают в «кошка или без животных»: рядом с «нет» — слова
    # гостя, а не «можно с животным»; у поля не по умолчанию — уточнение в названии.
    hard = [
        cond("dog", "P01", "in", ["cat", "none"], "species", "без собак"),
        cond("kg", "P01", "gte", 10, "weight_kg", "10 кг"),
    ]
    with TestClient(create_app()) as c:
        r = search(c, hard)
    by = {h["atom_id"]: h for h in r["hard"]}
    assert by["dog"]["label"] == "без собак" and by["dog"]["feature_name"] == "можно с животным"
    assert by["kg"]["feature_name"] == "можно с животным (максимальная масса животного)"


def test_or_group_and_negation_get_labels():
    hard = [
        cond("desk", "L04", "eq", "24/7", "front_desk_hours", NIGHT),
        cond("self", "L04", "eq", True, "self_checkin", NIGHT),
        cond("dog", "P01", "in", ["dog", "dog_cat", "any"], "species", "с собакой"),
    ]
    logic = [
        atom("desk"),
        atom("self"),
        atom("dog"),
        {"node_id": "or", "op": "OR", "children": ["n_desk", "n_self"]},
        {"node_id": "not_dog", "op": "NOT", "children": ["n_dog"]},
        {"node_id": "root", "op": "AND", "children": ["or", "not_dog"]},
    ]
    with TestClient(create_app()) as c:
        r = search(c, hard, logic, "root")
    group = next(h for h in r["hard"] if h.get("op") == "OR")
    assert group["label"] == NIGHT and group["feature_name"] is None
    assert [i["label"] for i in group["items"]] == [NIGHT, NIGHT]
    negated = next(h for h in r["hard"] if h.get("negated"))
    assert negated["label"] == "не с собакой"


def test_hotelier_responses_keep_prefill_and_fields():
    with TestClient(create_app()) as c:
        q = next(
            x
            for x in c.get(f"/api/hotelier/{RADISSON}/questions").json()["questions"]
            if x["feature_id"] == "P01"
        )
        asked = c.post(f"/api/hotelier/{RADISSON}/questions/{q['question_id']}/ask").json()
        answered = c.post(
            f"/api/hotelier/{RADISSON}/answers",
            json={"question_id": q["question_id"], "fields": {"species": "dog"}},
        ).json()
    for got in (asked["question"], answered["question"]):
        assert got["prefill"] == q["prefill"] and got["fields"] == q["fields"]
        assert got["title"] == q["title"]
