"""Логика обязательных условий (граф `logic`: И / ИЛИ / НЕ), этаж по размещениям и факты
фасада (D39). Граф считается там, где условия меняются: на каждом размещении (D17)."""

from agents.query_ast import Condition, OutOfCatalog, QueryAST
from app.stub_store import StubStore
from core.facts import Fact
from core.search import facade_subject, search, window_subject
from core.verdict import GuestStatus, Verdict

HOTEL = {"hotel_id": "h1", "city_id": "sochi", "name": "Тест"}
DOG = Condition(
    feature_id="P01",
    op="in",
    value=["dog", "dog_cat", "any"],
    field="species",
    binding="hotel",
    atom_id="dog",
    text_span="с собакой",
)
DESK = Condition(
    feature_id="L04",
    op="eq",
    value="24/7",
    field="front_desk_hours",
    binding="hotel",
    atom_id="desk",
    text_span="круглосуточная стойка",
)
SELF_IN = Condition(
    feature_id="L04",
    op="eq",
    value=True,
    field="self_checkin",
    binding="hotel",
    atom_id="self",
    text_span="самозаселение",
)
POOL = Condition(feature_id="F03", op="eq", value=True, binding="hotel", atom_id="pool")
SUNSET = Condition(feature_id="V05", op="eq", value=True, atom_id="sunset", text_span="закат")
QUIET = Condition(feature_id="N01", op="lte", value=45, field="lnight_db", atom_id="quiet")
NOT_FIRST = Condition(feature_id="floor", op="ne", value=1, field="ordinal", atom_id="floor")
LOW = Condition(feature_id="floor", op="lte", value=3, field="ordinal", atom_id="low")
COURTYARD = Condition(feature_id="courtyard", op="eq", value=True, field="is_courtyard")
n = 0


def fact(feature, value, level="hotel", subject="hotel:h1", status="hotelier"):
    global n
    n += 1
    return Fact(
        fact_id=f"f{n}",
        hotel_id="h1",
        city_id="sochi",
        subject_level=level,
        subject_id=subject,
        feature_id=feature,
        value=value,
        status=status,
    )


def window(feature, building, facade, floor, value):
    subject = window_subject(building, facade, floor)
    return fact(feature, value, "facade_physical_floor", subject, "computed")


def room_type(rt_id, placements, guarantee="all_guaranteed"):
    return {
        "room_type_id": rt_id,
        "name": rt_id,
        "placement_set": {
            "completeness": "complete",
            "guarantee": guarantee,
            "status": "hotelier",
            "source": {},
        },
        "placements": [
            {"placement_id": f"{rt_id}|{b}|{f}", "building_id": b, "floor": f, "windows": w}
            for b, f, w in placements
        ],
    }


def store(facts, room_types=None):
    by_feature = {}
    for f in facts:
        by_feature.setdefault(f.feature_id, []).append(f)
    return StubStore(
        hotel_by_id={"h1": HOTEL},
        by_hotel={"h1": by_feature},
        room_types_by_hotel={"h1": room_types} if room_types is not None else {},
    )


def atom(a):
    return {"node_id": f"n_{a}", "op": "ATOM", "atom_id": a}


def group(node_id, op, *children):
    return {"node_id": node_id, "op": op, "children": list(children)}


def run(s, hard, logic=(), root=None, ooc=()):
    ast = QueryAST(
        raw_text="тест",
        city_id="sochi",
        hard=list(hard),
        logic=list(logic),
        hard_root=root,
        out_of_catalog=list(ooc),
    )
    return search(s, ast)[0]


WITHOUT_DOGS = [atom("dog"), group("not_dog", "NOT", "n_dog"), group("root", "AND", "not_dog")]


def test_not_inverts_condition():
    # «Только без собак»: отель, где можно с собакой, — «не подходит», а не «подходит».
    r = run(store([fact("P01", {"species": "dog"})]), [DOG], WITHOUT_DOGS, "root")
    assert r["status"] == GuestStatus.not_fit
    assert r["hard"][0]["negated"] and r["hard"][0]["verdict"] == Verdict.no
    r = run(store([fact("P01", {"species": "none"})]), [DOG], WITHOUT_DOGS, "root")
    assert r["status"] == GuestStatus.fits
    assert run(store([]), [DOG], WITHOUT_DOGS, "root")["status"] == GuestStatus.needs_check


NIGHT = [
    atom("desk"),
    atom("self"),
    group("or", "OR", "n_desk", "n_self"),
    group("root", "AND", "or"),
]


def test_or_needs_one_yes():
    # «Заселиться ночью»: круглосуточная стойка ИЛИ самозаселение.
    s = store([fact("L04", {"front_desk_hours": "08:00-23:00", "self_checkin": True})])
    r = run(s, [DESK, SELF_IN], NIGHT, "root")
    assert r["status"] == GuestStatus.fits
    (item,) = r["hard"]
    assert item["op"] == "OR" and [i["verdict"] for i in item["items"]] == [Verdict.no, Verdict.yes]
    assert item["text_span"] == "круглосуточная стойка или самозаселение"
    assert run(s, [DESK, SELF_IN])["status"] == GuestStatus.not_fit  # без графа — И
    s = store([fact("L04", {"front_desk_hours": "08:00-23:00", "self_checkin": False})])
    assert run(s, [DESK, SELF_IN], NIGHT, "root")["status"] == GuestStatus.not_fit


def test_or_with_not_inside_stays_unknown():
    # (НЕ собаки) ИЛИ стойка: собак пускают → «нет», про стойку не знаем → «нужно уточнить».
    logic = [
        atom("dog"),
        atom("desk"),
        group("not_dog", "NOT", "n_dog"),
        group("or", "OR", "not_dog", "n_desk"),
        group("root", "AND", "or"),
    ]
    r = run(store([fact("P01", {"species": "dog"})]), [DOG, DESK], logic, "root")
    assert r["status"] == GuestStatus.needs_check
    assert r["hard"][0]["reason_code"] == "no_fact"


def test_nested_groups():
    # (собака И бассейн) ИЛИ самозаселение.
    logic = [
        atom("dog"),
        atom("pool"),
        atom("self"),
        group("both", "AND", "n_dog", "n_pool"),
        group("root", "OR", "both", "n_self"),
    ]
    facts = [fact("P01", {"species": "dog"}), fact("F03", False)]
    yes = store([*facts, fact("L04", {"self_checkin": True})])
    no = store([*facts, fact("L04", {"self_checkin": False})])
    assert run(yes, [DOG, POOL, SELF_IN], logic, "root")["status"] == GuestStatus.fits
    assert run(no, [DOG, POOL, SELF_IN], logic, "root")["status"] == GuestStatus.not_fit


def test_or_of_window_conditions_is_checked_on_each_placement():
    # Закат ИЛИ тишина: на 5-м этаже закат, на 2-м тишина — каждый номер подходит.
    facts = [
        window("V05", "b", "west", 5, True),
        window("N01", "b", "west", 5, {"lnight_db": 60}),
        window("V05", "b", "east", 2, False),
        window("N01", "b", "east", 2, {"lnight_db": 40}),
    ]
    rts = [room_type("std", [("b", 5, ["west"]), ("b", 2, ["east"])])]
    logic = [atom("sunset"), atom("quiet"), group("root", "OR", "n_sunset", "n_quiet")]
    r = run(store(facts, rts), [SUNSET, QUIET], logic, "root")
    assert r["status"] == GuestStatus.fits
    assert r["hard"][0]["op"] == "OR" and r["hard"][0]["verdict"] == Verdict.yes
    assert run(store(facts, rts), [SUNSET, QUIET])["status"] == GuestStatus.not_fit  # И


def test_hotel_or_window_condition():
    # Бассейн ИЛИ закат: бассейна нет, закат — только из части номеров.
    logic = [atom("pool"), atom("sunset"), group("root", "OR", "n_pool", "n_sunset")]
    facts = [fact("F03", False), window("V05", "b", "west", 5, True)]
    all_west = [room_type("std", [("b", 5, ["west"])])]
    assert run(store(facts, all_west), [POOL, SUNSET], logic, "root")["status"] == GuestStatus.fits
    facts.append(window("V05", "b", "east", 5, False))
    mixed = [room_type("std", [("b", 5, ["west"]), ("b", 5, ["east"])])]
    r = run(store(facts, mixed), [POOL, SUNSET], logic, "root")
    assert r["status"] == GuestStatus.needs_check
    assert r["categories"][0]["reason_code"] == "depends_on_room"


def test_out_of_catalog_inside_or_does_not_block_yes():
    cozy = OutOfCatalog(text_span="уютно", normalized="cozy", atom_id="cozy")
    logic = [atom("dog"), atom("cozy"), group("root", "OR", "n_dog", "n_cozy")]
    r = run(store([fact("P01", {"species": "dog"})]), [DOG], logic, "root", [cozy])
    assert r["status"] == GuestStatus.fits
    required = cozy.model_copy(update={"atom_id": "other", "requiredness": "required"})
    r = run(store([fact("P01", {"species": "dog"})]), [DOG], logic, "root", [required])
    assert r["status"] == GuestStatus.needs_check  # обязательное вне графа — через И


def test_broken_graph_is_never_fits():
    s = store([fact("P01", {"species": "dog"})])
    assert run(s, [DOG], [atom("dog")], "missing")["status"] == GuestStatus.needs_check
    two = [atom("dog"), group("root", "NOT", "n_dog", "n_dog")]
    assert run(s, [DOG], two, "root")["status"] == GuestStatus.needs_check
    cycle = [group("root", "AND", "loop"), group("loop", "OR", "root")]
    assert run(s, [DOG], cycle, "root")["status"] == GuestStatus.needs_check


def test_conditions_outside_graph_join_with_and():
    s = store([fact("P01", {"species": "dog"}), fact("L04", {"front_desk_hours": "08:00-23:00"})])
    r = run(s, [DOG, DESK], [atom("dog")], "n_dog")
    assert r["status"] == GuestStatus.not_fit
    assert [h["atom_id"] for h in r["hard"]] == ["dog", "desk"]


def test_floor_is_checked_by_placements():
    # «Не на первом этаже»: этаж — из ответа отеля о размещениях, фактов этажа нет (D39).
    mixed = [room_type("std", [("b", 1, ["west"]), ("b", 2, ["west"])])]
    r = run(store([], mixed), [NOT_FIRST])
    assert r["status"] == GuestStatus.needs_check
    assert r["categories"][0]["reason_code"] == "depends_on_room"
    high = [room_type("std", [("b", 3, ["west"]), ("b", 4, ["west"])])]
    assert run(store([], high), [NOT_FIRST])["status"] == GuestStatus.fits
    r = run(store([], []), [NOT_FIRST])
    assert r["status"] == GuestStatus.needs_check
    assert r["hard"][0]["reason_code"] == "no_placements"


def test_floor_and_view_on_the_same_placement():
    # Закат есть только на 5-м, а гостю нужно не выше 3-го: вместе — ни в одном номере.
    facts = [window("V05", "b", "west", 5, True), window("V05", "b", "west", 2, False)]
    rts = [room_type("std", [("b", 5, ["west"]), ("b", 2, ["west"])])]
    r = run(store(facts, rts), [SUNSET, LOW])
    assert r["status"] == GuestStatus.not_fit
    assert r["categories"][0]["reason_code"] == "no_single_placement"


def test_facade_fact_applies_to_every_floor():
    # «Окна во двор» — факт про весь фасад, а не про этаж; угловому номеру нужны все окна.
    facts = [
        fact("courtyard", {"is_courtyard": True}, "facade", facade_subject("b", "yard")),
        fact("courtyard", {"is_courtyard": False}, "facade", facade_subject("b", "street")),
    ]
    yard = [room_type("std", [("b", 2, ["yard"]), ("b", 7, ["yard"])])]
    assert run(store(facts, yard), [COURTYARD])["status"] == GuestStatus.fits
    corner = [room_type("corner", [("b", 3, ["yard", "street"])])]
    assert run(store(facts, corner), [COURTYARD])["status"] == GuestStatus.not_fit
