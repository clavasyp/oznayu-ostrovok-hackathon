from fastapi.testclient import TestClient

from agents.explain import build_claims, explain
from agents.explain.explain import assemble, problems
from agents.llm import Gateway, LLMUnavailable
from agents.llm.fake import FakeLLM
from app.main import create_app
from core.facts import Fact

RADISSON = "13736470"
W01 = Fact(
    fact_id="f_w01",
    hotel_id="h",
    city_id="sochi",
    subject_level="hotel",
    subject_id="hotel:h",
    feature_id="W01",
    value={"distance_m": 155.3},
    status="estimate",
)
FACTS = {W01.fact_id: W01}


def sunset_result():
    """Закат виден только с 5-го этажа: размещения 2–4 «нет», 5–8 «да» (D17)."""
    placements = [
        {
            "placement_id": f"p{n}",
            "building_id": "b",
            "floor": n,
            "verdict": "yes" if n >= 5 else "no",
        }
        for n in range(2, 9)
    ]
    v05 = {
        "feature_id": "V05",
        "atom_id": "a1",
        "text_span": "с видом на закат",
        "verdict": "unknown",
        "fact_ids": ["v1", "v2"],
        "reason_code": "depends_on_room",
        "reason": "Зависит от номера.",
    }
    w01 = {
        "feature_id": "W01",
        "atom_id": "a2",
        "text_span": "недалеко от моря",
        "verdict": "yes",
        "fact_ids": ["f_w01"],
        "weight": 1.0,
    }
    return {
        "status": "needs_check",
        "status_label": "Нужно уточнить",
        "hard": [v05],
        "soft": [w01],
        "categories": [
            {
                "room_type_id": "h:sea",
                "name": "С видом на море",
                "conditions": [v05],
                "placements": placements,
            },
        ],  # fmt: skip
        "best_category": "h:sea",
    }


def test_sunset_only_from_fifth_floor():
    claims = build_claims(sunset_result(), FACTS.get)
    texts = [c.text for c in claims]
    assert "закат из окна: только с 5-го этажа в категории «С видом на море»" in texts
    assert "пешком до пляжа: да (155 м, оценка)" in texts  # цифра — из факта, с паспортом


def test_template_says_blockers_first_then_but():
    e = explain(sunset_result(), FACTS.get)
    assert e.by == "template"
    assert e.text.startswith("Закат из окна: нужно уточнить — зависит от номера")
    assert ", но пешком до пляжа: да (155 м, оценка)." in e.text


def test_llm_orders_claims_and_links_only():
    reply = {"order": ["c2", "c1", "c3"], "links": ["", "но", "при этом"]}
    fake = FakeLLM([reply])
    e = explain(sunset_result(), FACTS.get, use_llm=True, gateway=Gateway([fake]))
    assert e.by == "llm"
    assert e.text == (
        "Закат из окна: только с 5-го этажа в категории «С видом на море», но закат из окна: "
        "нужно уточнить — зависит от номера, при этом пешком до пляжа: да (155 м, оценка)."
    )
    assert "Утверждения (данные)" in fake.calls[0]["user"]


def test_llm_order_that_drops_a_blocker_falls_back_to_template():
    fake = FakeLLM([{"order": ["c3"], "links": [""]}])  # про закат промолчали
    e = explain(sunset_result(), FACTS.get, use_llm=True, gateway=Gateway([fake]))
    assert e.by == "template" and "пропущены обязательные" in e.notes[0]
    fake = FakeLLM([{"order": ["c9"], "links": [""]}])
    e = explain(sunset_result(), FACTS.get, use_llm=True, gateway=Gateway([fake]))
    assert e.by == "template" and "чужие ID" in e.notes[0]
    e = explain(
        sunset_result(), FACTS.get, use_llm=True, gateway=Gateway([FakeLLM([LLMUnavailable("x")])])
    )
    assert e.by == "template" and "модель недоступна" in e.notes[0]


def test_numbers_must_come_from_claims():
    claims = build_claims(sunset_result(), FACTS.get)
    order = [c.claim_id for c in claims]
    text = assemble(claims, order, [""] * len(order)) + " До моря 50 м."
    assert any("числа не из фактов: 50" in p for p in problems(claims, order, [], text))


def test_api_search_has_template_explanation_and_explain_endpoint():
    with TestClient(create_app()) as c:
        body = c.post("/api/search", json={"text": "недалеко от моря", "city_id": "sochi"}).json()
        first = body["results"][0]
        assert first["explanation"]["by"] == "template" and first["explanation"]["text"]
        one = c.post(
            "/api/explain",
            json={"text": "тихий номер с видом на закат", "city_id": "sochi", "hotel_id": RADISSON},
        ).json()
        assert one["hotel_id"] == RADISSON and one["explanation"]["by"] == "template"
        assert any("ключ LLM не задан" in n for n in one["notes"])  # в тестах ключей нет
        missing = c.post("/api/explain", json={"text": "у моря", "hotel_id": "nope"})
        assert missing.status_code == 404


def test_same_reason_is_said_once():
    """Ревью №12: на «с собакой 10 кг» было две почти одинаковые фразы «нужно уточнить —
    об этом у нас пока нет данных». Одна причина — одна фраза; разные условия — через запятую."""
    with TestClient(create_app()) as c:
        dog = c.post("/api/search", json={"text": "С собакой 10 кг", "city_id": "sochi"}).json()
        both = c.post(
            "/api/search", json={"text": "обязательно лифт и кондиционер", "city_id": "sochi"}
        ).json()
    text = dog["results"][0]["explanation"]["text"]
    assert text.count("нужно уточнить") == 1 and text.startswith("Можно с животным:")
    text = both["results"][0]["explanation"]["text"]
    assert text.count("нужно уточнить") == 1 and text.startswith("Лифт, кондиционер")


def test_negated_condition_uses_guest_words():
    """Ревью №12: подпись отрицания — «без собак», а не «не можно с животным»."""
    ast = {
        "raw_text": "обязательно без собак",
        "city_id": "sochi",
        "hard": [
            {"feature_id": "P01", "field": "species", "op": "in",
             "value": ["dog", "dog_cat", "any"], "binding": "hotel", "atom_id": "a1",
             "span": {"start": 12, "end": 21, "text": "без собак"}, "text_span": "без собак"},
        ],
        "logic": [
            {"node_id": "n_a1", "op": "ATOM", "atom_id": "a1"},
            {"node_id": "not_a1", "op": "NOT", "children": ["n_a1"]},
            {"node_id": "root", "op": "AND", "children": ["not_a1"]},
        ],
        "hard_root": "root",
    }  # fmt: skip
    with TestClient(create_app()) as c:
        results = c.post("/api/search", json={"ast": ast}).json()["results"]
    texts = [r["explanation"]["text"] for r in results]
    assert any("без собак" in t.lower() for t in texts)
    assert not any("не можно" in t for t in texts)


def test_indirect_condition_says_guest_words_and_enum_words():
    """Ревью №20: «без собак» → правила дают species ∈ {cat, none} без negated, и объяснение
    говорило «Можно с животным: нет (dog_cat…)» — будто с животными нельзя."""
    with TestClient(create_app()) as c:
        sim = c.post("/api/hotelier/13736470/simulate", json={"feature_id": "P01"}).json()
        payload = {"question_id": sim["question_id"], "answer_text": sim["answer_text"],
                   "is_simulated": True}  # fmt: skip
        c.post("/api/hotelier/13736470/answers", json=payload)
        found = c.post("/api/search", json={"text": "обязательно без собак", "city_id": "sochi"})
    radisson = next(r for r in found.json()["results"] if r["hotel"]["hotel_id"] == "13736470")
    assert radisson["status"] == "not_fit"
    assert radisson["explanation"]["text"].startswith(
        "Без собак: нет (пускают собак и кошек, ответ имитатора отельера)"
    )


def test_every_enum_value_has_words():
    from agents.catalog import load_catalog
    from agents.explain.explain import value_words

    assert value_words("species", "dog_cat") == "пускают собак и кошек"
    assert value_words("lift_id", "present") == "есть" and value_words("x", 5) is None
    missing = [
        (f.id, name, v)
        for f in load_catalog().features.values()
        for name, spec in f.fields.items()
        for v in spec.values
        if value_words(name, v) is None
    ]
    assert missing == []


def test_has_x_is_direct_condition():
    """Ревью №21: «≠ нет» — это «есть X»; подпись — название, а не «Лифтом: да»."""
    with TestClient(create_app()) as c:
        for hotel_id, feature_id in (("9986868", "A03"), ("13736470", "P01")):
            sim = c.post(f"/api/hotelier/{hotel_id}/simulate", json={"feature_id": feature_id})
            sim = sim.json()
            payload = {"question_id": sim["question_id"], "answer_text": sim["answer_text"],
                       "is_simulated": True}  # fmt: skip
            c.post(f"/api/hotelier/{hotel_id}/answers", json=payload)
        lift = c.post("/api/search", json={"text": "обязательно с лифтом", "city_id": "sochi"})
        pet = c.post("/api/search", json={"text": "с питомцем 15 кг", "city_id": "sochi"})
    cosmos = next(r for r in lift.json()["results"] if r["hotel"]["hotel_id"] == "9986868")
    assert cosmos["explanation"]["text"].startswith("Лифт: да (есть")
    radisson = next(r for r in pet.json()["results"] if r["hotel"]["hotel_id"] == "13736470")
    assert "можно с животным: да" in radisson["explanation"]["text"]
    assert "с питомцем 15 кг: да" not in radisson["explanation"]["text"]


def test_label_follows_condition_direction():
    """Ревью №21: подпись N04 одна — «мало баров и клубов рядом», и гость, просивший
    «обязательно 2 бара рядом», видел «Мало баров и клубов рядом: нет (1)» — будто отель отклонён
    за то, что баров мало. Так же W05: «Лестницы по пути: да (0)» на «без лестниц»."""
    with TestClient(create_app()) as c:

        def text(query: str, hotel_id: str = RADISSON) -> str:
            body = c.post("/api/search", json={"text": query, "city_id": "sochi"}).json()
            found = next(r for r in body["results"] if r["hotel"]["hotel_id"] == hotel_id)
            return found["explanation"]["text"]

        assert text("обязательно 2 бара рядом").startswith("Бары и клубы рядом: нет (1)")
        assert text("мало баров рядом").startswith("Мало баров и клубов рядом: да (1)")
        stairs = text("до пляжа без лестниц")
        assert "без лестниц по пути: да (0)" in stairs and "лестницы по пути: да" not in stairs


def test_floors_claim_counts_floor_only_when_all_walls_agree():
    """D41-3: на этаже несколько размещений — по одному на стену стороны. Этаж «да», только
    если закат виден со всех стен; иначе этаж в утверждение не попадает."""
    from agents.catalog import load_catalog
    from agents.explain.explain import _floors_claim

    def placement(floor, wall, verdict):
        return {"placement_id": f"p{floor}{wall}", "building_id": "b", "floor": floor,
                "verdict": verdict}  # fmt: skip

    category = {
        "name": "Стандарт",
        "conditions": [{"feature_id": "V05", "text_span": "закат"}],
        "placements": [
            placement(2, "a", "no"),
            placement(2, "b", "no"),
            placement(3, "a", "yes"),
            placement(3, "b", "no"),  # одна стена из двух — не «да»
            placement(4, "a", "yes"),
            placement(4, "b", "yes"),
        ],  # fmt: skip
    }
    claim = _floors_claim("c1", category, load_catalog(), label=lambda c: "закат из окна")
    assert (
        claim is not None
        and claim.text == "закат из окна: только с 4-го этажа в категории «Стандарт»"
    )
    category["placements"] = category["placements"][:4]  # «да» нигде целиком — утверждения нет
    assert _floors_claim("c1", category, load_catalog(), label=lambda c: "закат") is None


def test_floors_claim_uses_item_label():
    """Ревью №21: утверждение об этажах подписывалось названием характеристики — на «без вида
    на море» гость читал «вид на море из окна: только до 2-го этажа», хотя на этих этажах вида
    как раз нет. Теперь подпись та же, что у пункта выше: слова гостя."""
    from agents.search import parse_query

    ast = parse_query("обязательно без вида на море", "sochi", mode="rules").ast
    (v01,) = ast.hard
    assert (v01.feature_id, v01.value) == ("V01", False)
    item = {
        "feature_id": "V01",
        "atom_id": v01.atom_id,
        "text_span": v01.text_span,
        "verdict": "unknown",
        "fact_ids": [],
        "reason_code": "depends_on_room",
        "reason": "Зависит от номера.",
    }
    placements = [
        {
            "placement_id": f"p{n}",
            "building_id": "b",
            "floor": n,
            "verdict": "yes" if n <= 2 else "no",
        }
        for n in range(1, 5)
    ]
    result = {
        "status": "needs_check",
        "hard": [item],
        "soft": [],
        "categories": [
            {"room_type_id": "h:std", "name": "Стандарт", "conditions": [item],
             "placements": placements},
        ],
        "best_category": "h:std",
    }  # fmt: skip
    texts = [c.text for c in build_claims(result, FACTS.get, ast=ast)]
    assert "без вида на море: только до 2-го этажа в категории «Стандарт»" in texts
    assert not any(t.startswith("вид на море из окна") for t in texts)


def test_evidence_words_for_lower_bound_and_beach_surface():
    """Стройки X03: «не ближе 1 км» — «ближе 1 км на карте нет», а не «1 км»; покрытие пляжа
    B01 — словами («галька»)."""
    from agents.catalog import load_catalog
    from agents.explain.explain import _evidence

    def fact(fid, feature, value):
        return Fact(fact_id=fid, hotel_id="h", city_id="sochi", subject_level="hotel",
                    subject_id="hotel:h", feature_id=feature, status="estimate",
                    value=value)  # fmt: skip

    facts = {
        "x": fact("x", "X03", {"distance_m": 1000, "bound": "lower"}),
        "b": fact(
            "b", "B01", {"surface": "pebble", "beach_id": "way/1", "beach_name": "Ставрополье"}
        ),
    }
    catalog = load_catalog()
    said = {k: _evidence({"feature_id": f.feature_id, "fact_ids": [k]}, catalog, facts.get)
            for k, f in facts.items()}  # fmt: skip
    assert said == {"x": "ближе 1 км на карте нет, оценка", "b": "галька, оценка"}


def test_soft_with_data_but_no_yes_is_explained_not_called_missing():
    """Желательное «нужно уточнить», но данные есть (часть окон подходит) — объяснение говорит
    об окнах, а не «данных нет»; «без стройки» — «нет стройки рядом» с расстоянием."""
    from agents.catalog import load_catalog
    from agents.explain.explain import _evidence

    result = {
        "status": "fits",
        "hard": [],
        "soft": [
            {"feature_id": "S02", "atom_id": "a1", "verdict": "unknown", "weight": 0.5,
             "reason_code": "window_some",
             "reason": "Подходят не все окна — уточним у отеля, где будет номер",
             "windows": {"yes": {"СЗ": [0]}, "no": {}, "estimate": {}, "count": 6, "want": 1}},
            {"feature_id": "V02", "atom_id": "a2", "verdict": "unknown", "weight": 0.5,
             "reason_code": "no_fact", "reason": "Нет данных"},
        ],
    }  # fmt: skip
    text = explain(result, lambda _: None).text
    assert "окна на северо-запад" in text and "нет данных" not in text.lower()
    near = Fact(fact_id="n", hotel_id="h", city_id="moscow", subject_level="hotel",
                subject_id="hotel:h", feature_id="X03", status="estimate",
                value={"distance_m": 328, "site_id": "way/1"})  # fmt: skip
    item = {"feature_id": "X03", "fact_ids": ["n"]}
    assert _evidence(item, load_catalog(), {"n": near}.get) == (
        "ближайшая стройка на карте — 328 м, оценка"
    )
    assert load_catalog().get("X03").short_for("gte", 300) == "нет стройки рядом"
    heritage = Fact(fact_id="h", hotel_id="h", city_id="moscow", subject_level="building",
                    subject_id="way/1", feature_id="H02", status="observed",
                    value={"heritage": True, "category": "регионального значения"})  # fmt: skip
    item = {"feature_id": "H02", "fact_ids": ["h"]}
    assert _evidence(item, load_catalog(), {"h": heritage}.get) == "регионального значения"
