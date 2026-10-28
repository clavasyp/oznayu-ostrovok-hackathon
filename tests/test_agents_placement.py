"""Размещения категорий со слов отеля (D17) на настоящих корпусах и категориях отелей показа.

Корпуса, фасады и категории — из пакетов Сочи и Москвы; ответы — имитатор по профилям и
свободный текст. Модель не вызывается (conftest), разбор — правилами; модель с заготовленным
ответом — `FakeLLM`.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents.hotelier.placement import answer_placement, guarantee_of, parse_placement
from agents.hotelier.simulator import load_profile, profiled_hotels
from agents.llm import Gateway
from agents.llm.fake import FakeLLM
from app.main import create_app
from core.packs import iter_jsonl
from core.placement_catalog import (
    hotel_aliases,
    name_key,
    placement_maps,
    side_segments,
    with_alias,
)

PACKS = Path(__file__).parents[1] / "packs"
RADISSON, KORALL, COSMOS, DOROGOMILOVSKAYA = "13736470", "11313836", "9986868", "6458292"
ARCHITECT, PROVENCE = "10012084", "8736052"
CITIES = {"sochi": [RADISSON, ARCHITECT, COSMOS, KORALL], "moscow": [PROVENCE, DOROGOMILOVSKAYA]}
SEA_VIEW = f"{RADISSON}:3502302001O"  # профиль: «корпус 2» = way/260633950, юг, 3–8 этаж
PREMIUM = f"{RADISSON}:3A02302001O"  # «главный корпус», юго-запад — у корпуса там 5 стен
MAIN, BUILDING_2, BUILDING_3, BUILDING_4 = (
    "way/207598815",
    "way/260633950",
    "way/260633951",
    "way/374404842",
)
KORALL_RT, KORALL_BUILDING = f"{KORALL}:3202302", "way/53742331"  # один корпус, 15 эт.
OFFLINE = Gateway([])


@pytest.fixture(scope="module")
def rows():
    out: dict = {"room_types": {}, "hotel_buildings": [], "buildings": [], "facades": []}
    for city, hotels in CITIES.items():
        data = PACKS / city / "data"
        for path in sorted(data.glob("room_types*.jsonl.gz")):
            for r in iter_jsonl(path):
                if r["hotel_id"] in hotels:
                    out["room_types"].setdefault(r["hotel_id"], []).append(
                        {"room_type_id": r["id"], "name": r["name"], "placement_set": None,
                         "placements": []}
                    )  # fmt: skip
        links = [
            r for r in iter_jsonl(data / "hotel_buildings.jsonl.gz") if r["hotel_id"] in hotels
        ]
        bids = {link["building_id"] for link in links}
        out["hotel_buildings"] += links
        out["buildings"] += [r for r in iter_jsonl(data / "buildings.jsonl.gz") if r["id"] in bids]
        out["facades"] += [
            r for r in iter_jsonl(data / "facades.jsonl.gz") if r["building_id"] in bids
        ]
    return out


@pytest.fixture
def client(rows):
    app = create_app()
    with TestClient(app) as c:  # своё приложение: ответы пишутся в хранилище заглушек
        s = app.state.store
        s.room_types_by_hotel = {
            h: [dict(rt, placements=[]) for rt in rts] for h, rts in rows["room_types"].items()
        }
        s.hotel_buildings = [dict(link) for link in rows["hotel_buildings"]]
        s.buildings, s.facades = rows["buildings"], rows["facades"]
        yield c


def simulate_and_answer(client, hotel_id, room_type_id):
    url = f"/api/hotelier/{hotel_id}/placement-"
    sim = client.post(url + "simulate", json={"room_type_id": room_type_id}).json()
    body = {k: sim[k] for k in ("room_type_id", "answer_text", "building_aliases", "is_simulated")}
    return sim, client.post(url + "answers", json=body).json()


def test_question_lists_buildings_with_address_and_levels(client):
    body = client.get(f"/api/hotelier/{RADISSON}/placement-questions").json()
    assert len(body["questions"]) == 15 and body["notes"] == []
    q = next(x for x in body["questions"] if x["room_type_id"] == SEA_VIEW)
    assert len(q["building_choices"]) == 4
    assert "улица 65 лет Победы, 50 — 9 эт." in q["question"]
    # Имя из OSM — подпись «на карте», а не название корпуса со слов отеля.
    assert "(на карте: «Radisson Collection»)" in q["question"]
    assert q["question_id"].startswith("qp_") and q["feature_id"] == "category_placement"


def test_simulated_answer_saves_placements_end_to_end(client):
    sim, body = simulate_and_answer(client, RADISSON, SEA_VIEW)
    assert sim["building_aliases"] == {"корпус 2": "way/260633950"}
    assert body["state"] == "answered" and body["placements_saved"] and body["followup"] is None
    rt = body["room_type"]
    ps = rt["placement_set"]
    assert (ps["completeness"], ps["guarantee"]) == ("complete", "all_guaranteed")
    assert ps["status"] == "simulated_hotelier" and ps["source"]["provider"] == "hotelier_simulator"
    assert ps["source"]["question_id"] == body["question_id"]
    assert [p["floor"] for p in rt["placements"]] == [3, 4, 5, 6, 7, 8]
    assert {p["building_id"] for p in rt["placements"]} == {"way/260633950"}
    (window,) = {w for p in rt["placements"] for w in p["windows"]}  # одна южная стена
    geometry = client.app.state.store.placement_geometry(RADISSON)
    south = {(f["building_id"], f["side"]): f["facade_id"] for f in geometry["placement_facades"]}
    assert window == south[("way/260633950", "S")]
    # Название корпуса — со ссылкой на вопрос, датой и цитатой, со статусом имитатора.
    (alias,) = body["aliases_saved"]
    assert (alias["name"], alias["status"]) == ("корпус 2", "simulated_hotelier")
    assert alias["source"]["question_id"] == body["question_id"]
    assert alias["source"]["answered_at"] and alias["source"]["quote"] in sim["answer_text"]
    # Размещение видно в карточке отеля — тем же путём, что ответы на остальные вопросы.
    hotel = client.get(f"/api/hotels/{RADISSON}").json()
    saved = next(r for r in hotel["room_types"] if r["room_type_id"] == SEA_VIEW)
    assert len(saved["placements"]) == 6
    assert saved["placement_set"]["status"] == "simulated_hotelier"


def test_hotelier_answer_for_single_building_needs_no_name(client):
    rt_id = f"{KORALL}:3202302"
    text = "Окна на восток, с 2 по 8 этаж. Других вариантов нет. Гарантируем."
    answer = {"room_type_id": rt_id, "answer_text": text}
    body = client.post(f"/api/hotelier/{KORALL}/placement-answers", json=answer).json()
    assert body["state"] == "answered" and body["aliases_saved"] == []
    ps = body["room_type"]["placement_set"]
    assert ps["status"] == "hotelier" and ps["completeness"] == "complete"
    assert {p["building_id"] for p in body["room_type"]["placements"]} == {"way/53742331"}
    assert len(body["room_type"]["placements"]) == 7


def test_unknown_building_asks_to_choose_and_saves_nothing(client):
    text = "Окна на юг, с 3 по 8 этаж (корпус 7)."
    body = client.post(
        f"/api/hotelier/{RADISSON}/placement-answers",
        json={"room_type_id": SEA_VIEW, "answer_text": text},
    ).json()
    assert body["state"] == "needs_clarification" and not body["placements_saved"]
    assert "Какой это корпус — «корпус 7»?" in body["followup"]
    for bid in ("way/207598815", "way/260633950", "way/260633951", "way/374404842"):
        assert bid in body["followup"]
    assert body["room_type"]["placement_set"] is None and body["room_type"]["placements"] == []
    # Корпус, которого у отеля нет, в названия не попадает.
    bad = client.post(
        f"/api/hotelier/{RADISSON}/placement-answers",
        json={"room_type_id": SEA_VIEW, "answer_text": text,
              "building_aliases": {"корпус 7": "way/53742331"}},
    ).json()  # fmt: skip
    assert bad["aliases_saved"] == [] and bad["state"] == "needs_clarification"
    assert any("нет среди корпусов" in r["reason"] for r in bad["agent"]["rejected"])


def test_side_is_every_wall_of_three_metres_one_placement_per_wall(client):
    """D41-3: «окна на юго-запад» у главного корпуса — все 5 стен юго-запада от 3 м, без вопроса
    «какая из стен»; номер на одной из них — на каждую стену и этаж своё размещение."""
    _, body = simulate_and_answer(client, RADISSON, PREMIUM)
    assert body["state"] == "answered" and body["followup"] is None
    geometry = client.app.state.store.placement_geometry(RADISSON)
    walls = side_segments(geometry["facades"], MAIN)["SW"]
    assert len(walls) == 5
    rt = body["room_type"]
    assert all(len(p["windows"]) == 1 for p in rt["placements"])
    assert {(p["floor"], p["windows"][0]) for p in rt["placements"]} == {
        (floor, wall) for floor in range(3, 9) for wall in walls
    }
    assert len({p["placement_id"] for p in rt["placements"]}) == 30
    ps = rt["placement_set"]
    assert (ps["completeness"], ps["guarantee"]) == ("complete", "all_guaranteed")
    assert ps["source"]["rule"] == "D41-3"
    assert ps["source"]["stated"][0]["sides"] == ["SW"]
    assert [a["building_id"] for a in body["aliases_saved"]] == [MAIN]


def test_walls_shorter_than_three_metres_are_not_windows(client):
    """У корпуса 2 Radisson на восток один отрезок 2,96 м — это не стена: уточнение."""
    geometry = client.app.state.store.placement_geometry(RADISSON)
    short = [f for f in geometry["facades"] if f["building_id"] == BUILDING_2 and f["side"] == "E"]
    assert [round(f["length_m"], 2) for f in short] == [2.96]
    assert "E" not in side_segments(geometry["facades"], BUILDING_2)
    body = client.post(
        f"/api/hotelier/{RADISSON}/placement-answers",
        json={"room_type_id": SEA_VIEW, "answer_text": "Окна на восток, с 3 по 5 этаж (корпус 2).",
              "building_aliases": {"корпус 2": BUILDING_2}},
    ).json()  # fmt: skip
    assert body["state"] == "needs_clarification" and not body["placements_saved"]
    assert "по карте нет стены на восток (от 3 м)" in body["followup"]
    assert body["room_type"]["placement_set"] is None
    # Cosmos: на север 6 отрезков, от 3 м — 3; в размещения идут только они.
    cosmos = client.app.state.store.placement_geometry(COSMOS)
    north = side_segments(cosmos["facades"], "way/190714781")["N"]
    _, body = simulate_and_answer(client, COSMOS, f"{COSMOS}:32023020005")
    windows = {p["windows"][0] for p in body["room_type"]["placements"]}
    assert len(north) == 3 and windows == set(north)


def test_courtyard_without_marked_walls_asks_for_side():
    """Двор: стены с `is_courtyard is True`; не отмечены (`null`) — «не знаем», а не «нет»."""
    facades = [
        {"building_id": "b", "facade_id": "s", "side": "S", "is_courtyard": None, "length_m": 20},
        {"building_id": "b", "facade_id": "in1", "side": "N", "is_courtyard": True, "length_m": 9},
        {"building_id": "b", "facade_id": "in2", "side": "E", "is_courtyard": True, "length_m": 2},
    ]
    geometry = {"placement_building_choices": [{"building_id": "b"}], "facades": facades[:1]}
    got = answer_placement("Окна во двор, с 2 по 4 этаж.", geometry, gateway=OFFLINE)
    assert got.placements is None
    assert "не знаем, какие стены выходят во двор — назовите сторону света" in got.followup
    assert "нет стены" not in got.followup
    geometry["facades"] = facades
    got = answer_placement("Окна во внутренний двор, с 2 по 4 этаж.", geometry, gateway=OFFLINE)
    assert {(i["floor"], tuple(i["windows"])) for i in got.placements["items"]} == {
        (2, ("in1",)), (3, ("in1",)), (4, ("in1",))
    }  # fmt: skip


def test_corner_room_only_when_hotel_says_so():
    """Окна на две стороны: без слов «в каждом номере» — номер на одной из стен; со словами —
    один номер с окнами на обе стороны (все сочетания стен)."""
    facades = [
        {"building_id": "b", "facade_id": fid, "side": side, "is_courtyard": None, "length_m": 10}
        for fid, side in (("s1", "S"), ("s2", "S"), ("w1", "W"))
    ]
    geometry = {"placement_building_choices": [{"building_id": "b"}], "facades": facades}
    got = answer_placement("Окна на юг и на запад, 3 этаж.", geometry, gateway=OFFLINE)
    assert sorted(tuple(i["windows"]) for i in got.placements["items"]) == [
        ("s1",), ("s2",), ("w1",)
    ]  # fmt: skip
    text = "Угловые номера: в каждом номере окна на юг и на запад, 3 этаж."
    got = answer_placement(text, geometry, gateway=OFFLINE)
    assert sorted(tuple(i["windows"]) for i in got.placements["items"]) == [
        ("s1", "w1"), ("s2", "w1")
    ]  # fmt: skip
    some = answer_placement("Есть угловые номера, окна на юг и запад, 3 этаж.", geometry,
                            gateway=OFFLINE)  # fmt: skip
    assert all(len(i["windows"]) == 1 for i in some.placements["items"])


@pytest.mark.parametrize(
    ("hotel_id", "expected"),
    [
        (RADISSON, {"3502302001O": "answered", "3202302001F": "answered",
                    "5002302001O": "answered", "3A02302001O": "answered"}),
        (KORALL, {"3202302": "answered", "7002302": "answered", "3B02": "answered"}),
        # Несколько стен на сторону — номер на одной из них (D41-3), без уточнения.
        (COSMOS, {"32023020005": "answered"}),
        (ARCHITECT, {"3402": "answered"}),
        (PROVENCE, {"3202": "answered"}),
    ],
)  # fmt: skip
def test_profiles_on_real_categories(client, hotel_id, expected):
    categories = {c["room_type_id"] for c in load_profile(hotel_id)["categories"]}
    assert categories == {f"{hotel_id}:{rg}" for rg in expected}
    for rg, state in expected.items():
        sim, body = simulate_and_answer(client, hotel_id, f"{hotel_id}:{rg}")
        assert body["state"] == state, (hotel_id, rg, body["followup"])
        # Каждый вариант профиля — на своём корпусе, этажах и всех стенах стороны от 3 м.
        cat = next(
            c for c in load_profile(hotel_id)["categories"] if c["room_type_id"].endswith(rg)
        )
        geometry = client.app.state.store.placement_geometry(hotel_id)
        want = {
            (p["building_id"], floor, wall)
            for p in cat["placements"]
            for floor in range(p["floors"][0], p["floors"][1] + 1)
            for wall in side_segments(geometry["facades"], p["building_id"])[p["side"]]
        }
        got = {
            (p["building_id"], p["floor"], p["windows"][0]) for p in body["room_type"]["placements"]
        }
        assert got == want, (hotel_id, rg, sim["answer_text"])


def test_profile_categories_and_buildings_exist_in_packs(rows):
    links = {(r["hotel_id"], r["building_id"]) for r in rows["hotel_buildings"]}
    for hotel_id in profiled_hotels():
        ids = {rt["room_type_id"] for rt in rows["room_types"].get(hotel_id, [])}
        for cat in load_profile(hotel_id)["categories"]:
            assert cat["room_type_id"] in ids, cat["room_type_id"]
            for p in cat["placements"]:
                assert (hotel_id, p["building_id"]) in links, (hotel_id, p["building_id"])


def test_hotel_without_categories_explains_why(client, rows):
    assert DOROGOMILOVSKAYA not in rows["room_types"]
    body = client.get(f"/api/hotelier/{DOROGOMILOVSKAYA}/placement-questions").json()
    assert body["questions"] == [] and "нет категорий номеров" in body["notes"][0]
    answer = {"room_type_id": f"{DOROGOMILOVSKAYA}:x", "answer_text": "Окна на юг, 2–3 этаж."}
    resp = client.post(f"/api/hotelier/{DOROGOMILOVSKAYA}/placement-answers", json=answer)
    assert resp.status_code == 404
    assert load_profile(DOROGOMILOVSKAYA)["categories"] == []


def test_hotelier_aliases_survive_new_pack_version():
    """Новая версия пакета приносит свою `evidence`; названия со слов отеля переносятся,
    кандидаты из пакета — нет; повтор не дублируется."""
    said = {"name": "Корпус 2", "status": "hotelier",
            "source": {"question_id": "qp_1", "answered_at": "2026-10-27T10:00:00+00:00",
                       "quote": "корпус 2"}}  # fmt: skip
    proposed = {"name": "Radisson", "status": "proposed", "source": {"url": "osm"}}
    old = {"rule": "first", "hotel_aliases": [said, proposed]}
    kept = hotel_aliases(old)
    assert kept == [said]
    new = with_alias({"rule": "second"}, kept[0])
    assert new == {"rule": "second", "hotel_aliases": [said]}
    assert with_alias(new, said | {"name": "КОРПУС 2"}) == new
    links = [{"hotel_id": "h", "building_id": "way/1", "evidence": new}]
    assert placement_maps(links, [], "h")[0]["корпус 2"] == "way/1"


def test_answer_shows_search_before_after_and_asks_if_list_is_complete(client):
    """С запросом гостя ответ о размещении показывает статус отеля до и после; понятое без
    «других вариантов нет» сохраняется, а отель получает вопрос, все ли это номера."""
    from agents.search import parse_query

    rt_id = f"{KORALL}:3202302"
    ast = parse_query("обязательно на этаже не ниже 5", "sochi", mode="rules").ast
    answer = {"room_type_id": rt_id, "answer_text": "Окна на восток, с 2 по 8 этаж.",
              "ast": ast.model_dump(mode="json")}  # fmt: skip
    body = client.post(f"/api/hotelier/{KORALL}/placement-answers", json=answer).json()
    assert body["state"] == "answered" and body["before"] and body["after"]
    assert body["room_type"]["placement_set"]["completeness"] != "complete"
    assert "Других вариантов нет" in body["followup"]
    questions = client.get(f"/api/hotelier/{KORALL}/placement-questions").json()["questions"]
    (q,) = [q for q in questions if q["room_type_id"] == rt_id]
    assert q["answer_text"] == "Окна на восток, с 2 по 8 этаж."


def post_answer(client, hotel_id, room_type_id, text, aliases=None, simulated=False):
    body = {"room_type_id": room_type_id, "answer_text": text, "is_simulated": simulated}
    if aliases is not None:
        body["building_aliases"] = aliases
    return client.post(f"/api/hotelier/{hotel_id}/placement-answers", json=body)


def floors_and_sides(body):
    rt = body["room_type"]
    return {(p["floor"], w) for p in rt["placements"] for w in p["windows"]}


def test_several_variants_in_one_sentence_pair_by_position(client):
    """Этажи, сторона и корпус связываются по месту в тексте, а не первым найденным."""
    geometry = client.app.state.store.placement_geometry(KORALL)
    walls = side_segments(geometry["facades"], KORALL_BUILDING)
    text = "Окна с 2 по 8 этаж на юг, с 9 по 15 этаж на восток. Других вариантов нет. Гарантируем."
    body = post_answer(client, KORALL, KORALL_RT, text).json()
    assert body["state"] == "answered" and body["followup"] is None
    assert floors_and_sides(body) == {(f, w) for f in range(2, 9) for w in walls["S"]} | {
        (f, w) for f in range(9, 16) for w in walls["E"]
    }
    ps = body["room_type"]["placement_set"]
    assert (ps["completeness"], ps["guarantee"]) == ("complete", "all_guaranteed")
    # Две стороны на один диапазон: обе, номер — у одной из стен.
    body = post_answer(client, KORALL, KORALL_RT, "Окна на юг и на запад, с 3 по 8 этаж. "
                       "Все номера такие.").json()  # fmt: skip
    assert floors_and_sides(body) == {(f, w) for f in range(3, 9) for w in walls["S"] + walls["W"]}
    assert body["room_type"]["placement_set"]["completeness"] == "complete"
    # Корпус с двоеточием и запятой; у каждого варианта свой корпус.
    text = "Корпус 2: с 3 по 8 этаж на юг, корпус 3: со 2 по 4 этаж на север"
    aliases = {"корпус 2": BUILDING_2, "корпус 3": BUILDING_3}
    body = post_answer(client, RADISSON, SEA_VIEW, text, aliases).json()
    got = {(p["building_id"], p["floor"]) for p in body["room_type"]["placements"]}
    assert got == {(BUILDING_2, f) for f in range(3, 9)} | {(BUILDING_3, f) for f in range(2, 5)}
    text = "корпус 2 с 3 по 5 этаж на юг, корпус 4 с 5 по 7 этаж на юг"
    aliases = {"корпус 2": BUILDING_2, "корпус 4": BUILDING_4}
    body = post_answer(client, RADISSON, SEA_VIEW, text, aliases).json()
    got = {(p["building_id"], p["floor"]) for p in body["room_type"]["placements"]}
    assert got == {(BUILDING_2, f) for f in range(3, 6)} | {(BUILDING_4, f) for f in range(5, 8)}


def test_unpaired_piece_is_asked_and_list_is_never_complete(client):
    text = (
        "Корпус 2 и корпус 3: с 2 по 4 этаж на юг, с 5 по 6 этаж на восток. "
        "Окна на юг, с 5 по 7 этаж (корпус 4). Других вариантов нет. Гарантируем."
    )
    aliases = {"корпус 2": BUILDING_2, "корпус 3": BUILDING_3, "корпус 4": BUILDING_4}
    body = post_answer(client, RADISSON, SEA_VIEW, text, aliases).json()
    # Понятое сохранено, но список не полный: один кусок не связать.
    assert body["state"] == "answered"
    assert {p["building_id"] for p in body["room_type"]["placements"]} == {BUILDING_4}
    assert body["room_type"]["placement_set"]["completeness"] == "partial"
    assert "не поняли, какие этажи к какой стороне" in body["followup"]
    assert "Ответьте целиком" in body["followup"]


@pytest.mark.parametrize(
    ("text", "guarantee"),
    [
        ("Гарантировать не можем.", "some_possible"),
        ("Гарантировать, к сожалению, не можем.", "some_possible"),
        ("Не можем гарантировать.", "some_possible"),
        ("Мы не гарантируем конкретный вариант.", "some_possible"),
        ("Без гарантии.", "some_possible"),
        ("Гарантий нет.", "some_possible"),
        ("Гарантировать нельзя.", "some_possible"),
        ("Конкретный вариант — по запросу.", "on_request"),
        ("Других вариантов нет. Гарантируем.", "all_guaranteed"),
        ("Это гарантируем при бронировании.", "all_guaranteed"),
        ("Постараемся, но гарантировать не будем.", "some_possible"),
        ("", "unknown"),
    ],
)
def test_guarantee_negation_in_both_word_orders(text, guarantee):
    assert guarantee_of(text) == guarantee


def test_negated_guarantee_does_not_close_hard_conditions(client):
    text = "Окна на восток, с 2 по 8 этаж. Других вариантов нет. Гарантировать не можем."
    ps = post_answer(client, KORALL, KORALL_RT, text).json()["room_type"]["placement_set"]
    assert (ps["completeness"], ps["guarantee"]) == ("complete", "some_possible")


def test_lying_model_is_checked_by_rules():
    """D8: сторону, корпус, полноту и гарантию модели проверяют правила по тексту отельера."""
    text = "Окна на юг, с 3 по 8 этаж."
    lie = {
        "placements": [{"building": "корпус 2", "floor_from": "3", "floor_to": "8",
                        "all_floors": False, "sides": ["E"], "corner": True,
                        "quote": "с 3 по 8 этаж"}],
        "completeness": "complete", "guarantee": "all_guaranteed", "unclear": [],
    }  # fmt: skip
    compact, rejected, parsed_by = parse_placement(text, gateway=Gateway([FakeLLM([lie])]))
    assert parsed_by == "rules" and rejected and "по правилам" in rejected[0]["reason"]
    (p,) = compact.placements
    assert (p.sides, p.building, p.corner) == (["S"], "", False)
    assert (compact.completeness, compact.guarantee) == ("unknown", "unknown")
    # Сторона из цитаты, но корпуса в цитате нет — отклонено; правила всё равно дают вариант.
    lie["placements"][0] |= {"sides": ["S"], "quote": "Окна на юг, с 3 по 8 этаж"}
    compact, rejected, _ = parse_placement(text, gateway=Gateway([FakeLLM([lie])]))
    assert rejected[0]["reason"] == "корпуса нет в цитате"
    assert [(p.building, p.sides) for p in compact.placements] == [("", ["S"])]
    # Цитаты нет в ответе — выдумка.
    lie["placements"][0] |= {"quote": "Окна на запад, 9 этаж"}
    _, rejected, _ = parse_placement(text, gateway=Gateway([FakeLLM([lie])]))
    assert rejected[0]["reason"] == "цитаты нет в ответе"
    # Сохраняется тоже только проверенное: восточной стены и корпуса 2 нет.
    geometry = {
        "placement_building_choices": [{"building_id": "b1"}, {"building_id": "b2"}],
        "placement_building_aliases": {"корпус 2": "b2"},
        "facades": [
            {"building_id": b, "facade_id": f"{b}-{side}", "side": side, "length_m": 10}
            for b in ("b1", "b2")
            for side in ("E", "S")
        ],
    }
    lie["placements"][0] |= {"sides": ["E"], "quote": "с 3 по 8 этаж"}
    got = answer_placement(text, geometry, gateway=Gateway([FakeLLM([lie])]),
                           building_aliases={"корпус 1": "b1"})  # fmt: skip
    assert {w for i in got.placements["items"] for w in i["windows"]} == {"b1-S"}
    assert (got.placements["completeness"], got.placements["guarantee"]) == (
        "unknown",
        "some_possible",
    )


def test_honest_model_variant_is_accepted():
    text = "В категории «Стандарт с видом на восток»: окна на юг, с 3 по 8 этаж (корпус 2)."
    honest = {
        "placements": [{"building": "корпус 2", "floor_from": "3", "floor_to": "8",
                        "all_floors": False, "sides": ["S"], "corner": False,
                        "quote": "окна на юг, с 3 по 8 этаж (корпус 2)"}],
        "completeness": "complete", "guarantee": "all_guaranteed", "unclear": [],
    }  # fmt: skip
    compact, rejected, parsed_by = parse_placement(text, gateway=Gateway([FakeLLM([honest])]))
    assert (parsed_by, rejected) == ("llm", [])
    # Название категории в «ёлочках» — не сторона; полноту и гарантию модель не ставит.
    assert [(p.building, p.sides) for p in compact.placements] == [("корпус 2", ["S"])]
    assert (compact.completeness, compact.guarantee) == ("unknown", "unknown")


def test_empty_building_name_is_rejected_and_ignored(client):
    resp = post_answer(client, RADISSON, SEA_VIEW, "Окна на юг, с 3 по 8 этаж.", {"": BUILDING_2})
    assert resp.status_code == 422 and "пустое название" in resp.text
    resp = post_answer(client, RADISSON, SEA_VIEW, "Окна на юг, с 3 по 8 этаж.", {" ,. ": MAIN})
    assert resp.status_code == 422
    links = [{"hotel_id": "h", "building_id": "way/1", "evidence": {"hotel_aliases": [
        {"name": "", "status": "hotelier", "source": {"question_id": "qp"}},
        {"name": " - ", "status": "hotelier", "source": {"question_id": "qp"}},
    ]}}]  # fmt: skip
    assert placement_maps(links, [], "h")[0] == {"way/1": "way/1"}
    # Отмечен один корпус, в тексте не назван — берём отмеченный.
    body = post_answer(client, RADISSON, SEA_VIEW, "Окна на юг, с 3 по 8 этаж.",
                       {"корпус 2": BUILDING_2}).json()  # fmt: skip
    assert {p["building_id"] for p in body["room_type"]["placements"]} == {BUILDING_2}
    # Корпусов несколько, ни назван, ни отмечен — вопрос.
    body = post_answer(client, RADISSON, SEA_VIEW, "Окна на юг, с 3 по 8 этаж.").json()
    assert (
        body["state"] == "needs_clarification" and "корпус в ответе не назван" in body["followup"]
    )


def test_punctuation_after_building_number(client):
    for text in (
        "Номера стандарт — корпус 2, 3–5 этаж, окна на запад",
        "Корпус №2: окна на запад, 3-5 этаж.",
        "в корпусе 2; окна на запад, этажи 3–5",
    ):
        body = post_answer(client, RADISSON, SEA_VIEW, text, {"Корпус 2": BUILDING_2}).json()
        assert body["state"] == "answered", (text, body["followup"])
        assert {(p["building_id"], p["floor"]) for p in body["room_type"]["placements"]} == {
            (BUILDING_2, f) for f in (3, 4, 5)
        }
    assert name_key("корпус 2,") == name_key("Корпус №2") == name_key("2-й корпус") == "корпус 2"


def test_alias_passport_quote_only_when_name_is_in_text(client):
    text = "Окна на юг, с 3 по 8 этаж (корпус 2)."
    body = post_answer(client, RADISSON, SEA_VIEW, text,
                       {"Главный": MAIN, "корпус 2": BUILDING_2}).json()  # fmt: skip
    saved = {a["name"]: a["source"] for a in body["aliases_saved"]}
    assert saved["Главный"]["method"] == saved["корпус 2"]["method"] == "selected_in_list"
    assert "quote" not in saved["Главный"]
    assert saved["корпус 2"]["quote"] == "Окна на юг, с 3 по 8 этаж (корпус 2)"


@pytest.mark.parametrize(
    ("text", "floors"),
    [
        ("Окна на юг со 2 по 5 этаж.", range(2, 6)),
        ("Окна на юг, этажи 3–8.", range(3, 9)),
        ("На 3 этаже окна на юг.", range(3, 4)),
        ("Окна на юг, с 3-го по 5-й этаж.", range(3, 6)),
        ("Окна на юг, на 3 и 5 этажах.", (3, 5)),
    ],
)
def test_floor_forms(client, text, floors):
    body = post_answer(client, KORALL, KORALL_RT, text).json()
    assert body["state"] == "answered", body["followup"]
    assert {p["floor"] for p in body["room_type"]["placements"]} == set(floors)


def test_floors_checked_against_map_and_what_is_missing_is_named(client):
    # «Все этажи» — по карте (15 эт.), с просьбой подтвердить; полным список не считается.
    body = post_answer(client, KORALL, KORALL_RT, "Окна на восток, все этажи. Других нет.").json()
    assert body["state"] == "answered"
    assert {p["floor"] for p in body["room_type"]["placements"]} == set(range(1, 16))
    assert body["room_type"]["placement_set"]["completeness"] == "partial"
    assert "по карте в нём 15 эт. — подтвердите" in body["followup"]
    # Этаж выше этажности по карте — сохранено со слов отеля, с пометкой и просьбой подтвердить.
    text = "Окна на восток, с 20 по 25 этаж. Других вариантов нет."
    body = post_answer(client, KORALL, KORALL_RT, text).json()
    assert body["state"] == "answered" and "подтвердите этажи" in body["followup"]
    assert any("выше этажности" in n for n in body["notes"])
    # Море — не сторона света; этажи найдены — спрашиваем только сторону.
    body = post_answer(client, KORALL, KORALL_RT, "Окна на море, с 3 по 8.").json()
    assert body["state"] == "needs_clarification"
    assert "«на море» — это вид, а не сторона света" in body["followup"]
    body = post_answer(client, KORALL, KORALL_RT, "Окна на юг.").json()
    assert "нет этажей" in body["followup"]
    body = post_answer(client, KORALL, KORALL_RT, "Окна на юг, с 0 по 2 этаж.").json()
    assert body["state"] == "needs_clarification" and "вне 1–100" in body["followup"]


def test_simulator_does_not_replace_real_answer_in_stub_mode(client):
    """D41-8: ответ имитатора (и песочницы) не заменяет размещения настоящего отеля."""
    real = "Окна на восток, с 2 по 4 этаж. Других вариантов нет. Гарантируем."
    assert post_answer(client, KORALL, KORALL_RT, real).json()["state"] == "answered"
    sim = client.post(f"/api/hotelier/{KORALL}/placement-simulate",
                      json={"room_type_id": KORALL_RT}).json()  # fmt: skip
    assert "не заменит" in sim["notes"][0]
    body = post_answer(
        client, KORALL, KORALL_RT, sim["answer_text"], sim["building_aliases"], simulated=True
    ).json()
    assert body["state"] == "real_answer_kept" and body["real_answer_kept"] is True
    assert not body["placements_saved"] and body["aliases_saved"] == []
    assert "настоящий ответ остаётся" in body["notes"][-1]
    rt = body["room_type"]
    assert rt["placement_set"]["status"] == "hotelier"
    assert {p["floor"] for p in rt["placements"]} == {2, 3, 4}
    # Общая ручка ответов — то же правило в ядре.
    payload = {"feature_id": "category_placement", "room_type_id": KORALL_RT, "is_simulated": True,
               "placements": {"completeness": "complete", "guarantee": "all_guaranteed",
                              "items": [{"building_id": KORALL_BUILDING, "floor": 9,
                                         "windows": ["x"]}]}}  # fmt: skip
    store = client.app.state.store
    store.questions_by_hotel.setdefault(KORALL, []).append(
        {"question_id": 1, "hotel_id": KORALL, "feature_id": "category_placement",
         "city_id": "sochi", "parsed_fields": []}
    )  # fmt: skip
    resp = client.post(f"/api/hotelier/{KORALL}/answers", json=payload).json()
    assert resp["placements_saved"] is False
    after = client.get(f"/api/hotelier/{KORALL}/placement-questions").json()
    q = next(x for x in after["questions"] if x["room_type_id"] == KORALL_RT)
    assert (q["answer_status"], q["placement_status"]) == ("answered", "hotelier")
    # Имитатор заменяет прежний ответ имитатора; настоящий — любой.
    other = f"{KORALL}:7002302"
    _, first = simulate_and_answer(client, KORALL, other)
    assert first["state"] == "answered"
    again = post_answer(client, KORALL, other, "Окна на восток, 9 этаж.", simulated=True).json()
    assert again["state"] == "answered"
    assert {p["floor"] for p in again["room_type"]["placements"]} == {9}
    real = post_answer(client, KORALL, other, "Окна на восток, 10 этаж.").json()
    assert real["room_type"]["placement_set"]["status"] == "hotelier"
