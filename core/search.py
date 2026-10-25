"""Поиск по готовому AST → три статуса для гостя (D31). Работает с любым `core.store.Store`.

Уровень условия определяется по фактам характеристики у отеля:
- **отель, корпус, маршрут** — факты группируются по «месту» (субъект + параметры). «Да» —
  только если «да» на каждом месте, «нет» — если «нет» везде; иначе «нужно уточнить»: у отеля
  несколько корпусов, и неизвестно, где окажется гость. При `quantifier: any` хватает одного «да»;
- **категория номера** — факты с `subject_level = room_type` проверяются по каждой категории;
- **окно** (`facade_physical_floor`, «здание:фасад:floorN») — по размещениям категории (D17).
  Все такие условия проверяются **на одном и том же размещении**: нельзя взять «вид на закат»
  у одного размещения, а «тихо ночью» у другого. Категория подходит, только если список
  размещений полный и на каждом размещении все условия «да». Виду и солнцу хватает одного
  окна углового номера, остальному нужны все окна.

Этаж номера (`floor`) — не факт, а поле размещения из ответа отеля: проверяется на том же
размещении, что и условия окна (D39). Факт про весь фасад («окна во двор») — субъект
«здание:фасад», он относится к окнам этого фасада на любом этаже.

Обязательные условия соединяются графом `logic` из AST (И / ИЛИ / НЕ любой вложенности), без
графа — через И. Граф считается там, где условия меняются: на каждом размещении, если в запросе
есть условия окна или этажа, иначе — по категории или по отелю.

Отель подходит, если подходит хотя бы одна категория. Оценка не закрывает обязательное
условие (D18), неизвестное не становится «нет» (D5). Если на одном месте пригодные факты
разных источников расходятся, это противоречие (core §6): «нужно уточнить», ID фактов в ответе.
«Есть, тип не назван» (`present`) против конкретного типа — тоже «нужно уточнить» (D39).
Причины — кодом (`reason_code`) и текстом для экрана (`reason`, `core/reasons.py`).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from typing import Any

from agents.query_ast import Condition, LogicNode, QueryAST
from core.facts import Fact, Status, SubjectLevel
from core.reasons import reason_text
from core.store import Store
from core.verdict import GUEST_LABELS, GuestStatus, Verdict, and3, guest_status, not3, or3

EXAMPLE_AST = (
    Path(__file__).resolve().parents[1] / "agents" / "schemas" / "examples" / "track_query.json"
)
DEFAULT_FIELDS = (
    "distance_m",
    "count",
    "mean",
    "mean_C",
    "aboveground_levels",
    "known_step_sections",
    "known_lit_percent",
)
ORDER = {GuestStatus.fits: 0, GuestStatus.needs_check: 1, GuestStatus.not_fit: 2}
WINDOW_LEVELS = frozenset(
    {
        SubjectLevel.facade_physical_floor,
        SubjectLevel.facade,
        SubjectLevel.physical_floor,
        SubjectLevel.placement,
    }
)
CATEGORY_LEVELS = frozenset({SubjectLevel.room_type})
WINDOW_ANY = ("V", "S")  # вид и солнце: номеру хватает одного из окон
GUARANTEED = frozenset({"all_guaranteed", "exact_room"})
# Условия про само размещение (D39): характеристика → поле размещения из ответа отеля.
# Этаж — как его называет отель (первый — на уровне земли).
PLACEMENT_FIELDS = {"floor": "floor"}
# Наличие без поля да / нет (D39): «нет» — `none`, «есть, тип не назван» — `present`.
PRESENT, ABSENT = "present", "none"


def example_ast() -> QueryAST:
    return QueryAST.model_validate(json.loads(EXAMPLE_AST.read_text("utf-8")))


def window_subject(building_id: str, facade_id: str, floor: int) -> str:
    """ID окна как в расчётах v9: «здание:фасад:floorN»."""
    return f"{building_id}:{facade_id}:floor{floor}"


def facade_subject(building_id: str, facade_id: str) -> str:
    """ID фасада «здание:фасад» — факт про все его окна, например «окна во двор» (D39)."""
    return f"{building_id}:{facade_id}"


def _number(fact: Fact, field: str | None) -> Any:
    v = fact.value
    if isinstance(v, dict):
        if field:
            return v.get(field)
        return next((v[k] for k in DEFAULT_FIELDS if k in v), None)
    return v


def _compare(op: str, left: Any, right: Any) -> bool | None:
    if left is None:
        return None
    try:
        return {
            "eq": lambda: left == right,
            "ne": lambda: left != right,
            "lt": lambda: left < right,
            "lte": lambda: left <= right,
            "gt": lambda: left > right,
            "gte": lambda: left >= right,
            "in": lambda: left in right,
            "exists": lambda: left != ABSENT and left is not False,
        }[op]()
    except TypeError:
        return None


def _unspecified(op: str, left: Any, right: Any) -> bool:
    """«Есть, тип не назван» против конкретного типа: не «нет», а «неизвестно» (D39)."""
    if left != PRESENT or op not in ("eq", "ne", "in"):
        return False
    wanted = right if isinstance(right, list) else [right]
    return any(w not in (PRESENT, ABSENT) for w in wanted)


def _params_match(fact: Fact, params: dict[str, Any]) -> bool:
    return all(fact.params.get(k) == v for k, v in params.items())


def _place(fact: Fact) -> tuple[str, str]:
    """Место факта: субъект и параметры. Разные рецепты одного места — кандидаты на противоречие."""
    return fact.subject_id, json.dumps(fact.params, sort_keys=True, ensure_ascii=False, default=str)


def _result(c: Condition, verdict: Verdict, code: str | None = None, **extra: Any) -> dict:
    out: dict[str, Any] = {
        "feature_id": c.feature_id,
        "atom_id": c.atom_id,
        "text_span": c.text_span,
        "verdict": verdict,
        "fact_ids": [],
    }
    if code:
        out |= {"reason_code": code, "reason": reason_text(code)}
    return out | extra


def evaluate(facts: list[Fact], c: Condition, hard: bool) -> dict[str, Any]:
    """Одно условие по фактам одного отеля (или одного окна, одной категории)."""
    facts = [f for f in facts if _params_match(f, c.params)]
    if not facts:
        return _result(c, Verdict.unknown, "no_fact")
    ids = [f.fact_id for f in facts]
    if all(f.status == Status.not_applicable for f in facts):
        return _result(c, Verdict.no, "not_applicable", fact_ids=ids)
    known = [f for f in facts if f.status not in (Status.unknown, Status.not_applicable)]
    usable = [f for f in known if f.usable_in_hard] if hard else known
    if not usable:
        code = "estimate_only" if known else (facts[0].unknown_reason or "unknown")
        return _result(c, Verdict.unknown, code, fact_ids=ids)

    extra = {
        "fact_ids": [f.fact_id for f in usable],
        "statuses": sorted({f.status for f in usable}),
    }
    places: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for f in usable:
        places[_place(f)].append(f)
    verdicts: list[Verdict] = []
    conflict_ids: list[str] = []
    unspecified = False
    for fs in places.values():
        results: set[bool | None] = set()
        for f in fs:
            left = _number(f, c.field)
            if _unspecified(c.op, left, c.value):
                unspecified = True
                results.add(None)
            else:
                results.add(_compare(c.op, left, c.value))
        if results == {True}:
            verdicts.append(Verdict.yes)
        elif results == {False}:
            verdicts.append(Verdict.no)
        else:
            verdicts.append(Verdict.unknown)
            if {True, False} <= results:
                conflict_ids += [f.fact_id for f in fs]

    if all(v == Verdict.yes for v in verdicts) or (
        c.quantifier == "any" and Verdict.yes in verdicts
    ):
        return _result(c, Verdict.yes, **extra)
    if all(v == Verdict.no for v in verdicts):
        return _result(c, Verdict.no, **extra)
    if conflict_ids:
        return _result(c, Verdict.unknown, "conflict", conflict_fact_ids=conflict_ids, **extra)
    if Verdict.yes in verdicts and Verdict.no in verdicts:
        return _result(c, Verdict.unknown, "depends_on_building", **extra)
    if unspecified:
        return _result(c, Verdict.unknown, "value_unspecified", **extra)
    return _result(c, Verdict.unknown, "not_comparable", **extra)


def _level(facts: Iterable[Fact]) -> str:
    levels = {f.subject_level for f in facts}
    if levels & WINDOW_LEVELS:
        return "window"
    if levels & CATEGORY_LEVELS:
        return "category"
    return "hotel"


def _across(verdicts: list[Verdict]) -> Verdict:
    """По размещениям категории: «да» везде → да, «нет» везде → нет, иначе — неизвестно."""
    if verdicts and all(v == Verdict.yes for v in verdicts):
        return Verdict.yes
    if verdicts and all(v == Verdict.no for v in verdicts):
        return Verdict.no
    return Verdict.unknown


def _window_condition(c: Condition, windows: list[list[Fact]], hard: bool) -> dict[str, Any]:
    """Условие уровня окна на одном размещении: по всем его окнам."""
    if not windows:
        return _result(c, Verdict.unknown, "no_window_fact")
    results = [evaluate(w, c, hard) for w in windows]
    combine: Callable[[Iterable[Verdict]], Verdict] = (
        or3 if c.feature_id.startswith(WINDOW_ANY) else and3
    )
    verdict = combine(r["verdict"] for r in results)
    ids = [i for r in results for i in r["fact_ids"]]
    if verdict == Verdict.unknown:
        code = next((r["reason_code"] for r in results if r.get("reason_code")), "unknown")
        return _result(c, verdict, code, fact_ids=ids)
    return _result(c, verdict, fact_ids=ids)


def _placement_condition(c: Condition, p: dict[str, Any]) -> dict[str, Any]:
    """Условие про само размещение — этаж (D39): по ответу отеля, без фактов."""
    ok = _compare(c.op, p.get(PLACEMENT_FIELDS[c.feature_id]), c.value)
    if ok is None:
        return _result(c, Verdict.unknown, "not_comparable")
    return _result(c, Verdict.yes if ok else Verdict.no)


def _env(results: Iterable[dict[str, Any]]) -> dict[str, Verdict]:
    return {r["atom_id"]: r["verdict"] for r in results if r.get("atom_id")}


class Logic:
    """Граф обязательных условий AST (`logic` + `hard_root`): И / ИЛИ / НЕ любой вложенности.

    Считается трёхзначно (D5). Условия вне графа и обязательные части вне каталога соединяются
    с ним через И. Атом без условия — «неизвестно»; битый граф (нет корня) — «неизвестно» для
    всего запроса: без логики нельзя честно сказать ни «да», ни «нет».
    """

    def __init__(self, ast: QueryAST) -> None:
        self.nodes: dict[str, LogicNode] = {n.node_id: n for n in ast.logic}
        self.root = ast.hard_root or ""
        self.broken = self.root not in self.nodes
        self.atoms: set[str] = set()  # атомы, достижимые из корня
        seen: set[str] = set()
        stack = [self.root]
        while stack:
            node = self.nodes.get(stack.pop())
            if node is None or node.node_id in seen:
                continue
            seen.add(node.node_id)
            if node.op == "ATOM" and node.atom_id:
                self.atoms.add(node.atom_id)
            stack += node.children

    @classmethod
    def of(cls, ast: QueryAST) -> Logic | None:
        return cls(ast) if ast.logic else None

    def covers(self, result: dict[str, Any]) -> bool:
        """Условие учтено в графе; иначе оно соединяется с графом через И."""
        return result.get("atom_id") in self.atoms

    def verdicts(self, env: dict[str, Verdict]) -> dict[str, Verdict]:
        """Вердикт каждого узла, достижимого из корня. Цикл в графе — «неизвестно»."""
        out: dict[str, Verdict] = {}
        path: set[str] = set()

        def ev(node_id: str) -> Verdict:
            if node_id in out:
                return out[node_id]
            node = self.nodes.get(node_id)
            if node is None or node_id in path:
                return Verdict.unknown
            path.add(node_id)
            vs = [ev(child) for child in node.children]
            if node.op == "ATOM":
                v = env.get(node.atom_id or "", Verdict.unknown)
            elif node.op == "NOT":
                v = not3(vs[0]) if len(vs) == 1 else Verdict.unknown
            elif not vs:
                v = Verdict.unknown  # пустые И / ИЛИ — ошибка разбора, а не «да»
            else:
                v = and3(vs) if node.op == "AND" else or3(vs)
            path.discard(node_id)
            out[node_id] = v
            return v

        ev(self.root)
        return out

    def root_verdict(self, nodes: dict[str, Verdict], loose: Iterable[Verdict]) -> Verdict:
        if self.broken:
            return Verdict.unknown
        return and3([nodes.get(self.root, Verdict.unknown), *loose])

    def items(self, nodes: dict[str, Verdict], results: list[dict[str, Any]]) -> list[dict]:
        """Обязательные условия для экрана по графу. Верхнее «И» раскрывается; «ИЛИ» и вложенные
        группы — один пункт с вариантами (`op`, `items`); «НЕ» — то же условие с `negated`."""
        by_atom = {r["atom_id"]: r for r in results if r.get("atom_id")}
        root = self.nodes.get(self.root)
        top = root.children if root is not None and root.op == "AND" else [self.root]
        return [self._item(n, nodes, by_atom, 0) for n in top]

    def _item(
        self, node_id: str, nodes: dict[str, Verdict], by_atom: dict[str, dict], depth: int
    ) -> dict[str, Any]:
        node = self.nodes.get(node_id)
        verdict = nodes.get(node_id, Verdict.unknown)
        invalid = {
            "text_span": None,
            "verdict": verdict,
            "fact_ids": [],
            "reason_code": "logic_invalid",
            "reason": reason_text("logic_invalid"),
        }
        if node is None or depth > len(self.nodes):
            return {"op": "invalid", "atom_id": None, **invalid}
        if node.op == "ATOM":
            found = by_atom.get(node.atom_id or "")
            if found is None:
                return {"atom_id": node.atom_id, **invalid}
            return found | {"verdict": verdict}
        children = [self._item(c, nodes, by_atom, depth + 1) for c in node.children]
        if node.op == "NOT" and len(children) == 1 and "op" not in children[0]:
            item = children[0] | {"verdict": verdict, "negated": True}
            if verdict != Verdict.unknown:
                item.pop("reason_code", None)
                item.pop("reason", None)
            return item
        # Варианты из одного фрагмента («заселиться ночью»: стойка или самозаселение) — без повтора.
        spans = list(dict.fromkeys(c["text_span"] for c in children if c.get("text_span")))
        text = {"OR": " или ", "AND": " и "}.get(node.op, ", ").join(spans) or None
        item = {
            "op": node.op,
            "atom_id": None,
            "text_span": f"не {text}" if node.op == "NOT" and text else text,
            "verdict": verdict,
            "fact_ids": [x for c in children for x in c.get("fact_ids", [])],
            "items": children,
        }
        if verdict == Verdict.unknown:
            code = next((c["reason_code"] for c in children if c.get("reason_code")), "unknown")
            item |= {"reason_code": code, "reason": reason_text(code)}
        return item


def evaluate_category(
    rt: dict[str, Any],
    category_conds: list[Condition],
    window_conds: list[Condition],
    facts: dict[str, list[Fact]],
    hard: bool,
    logic: Logic | None = None,
    base: dict[str, Verdict] | None = None,
) -> dict[str, Any]:
    """Категория номера: её собственные факты и размещения «здание × этаж × окна» (D17).

    Без графа `logic` условия соединяются через И. С графом весь запрос считается на каждом
    размещении: условия отеля (`base`), категории и этого размещения — вместе.
    """
    conditions = [
        evaluate([f for f in facts[c.feature_id] if f.subject_id == rt["room_type_id"]], c, hard)
        for c in category_conds
    ]
    own = list(conditions)
    units: list[dict[str, Verdict]] = []  # узлы графа на каждом размещении

    def combine(here: list[dict[str, Any]]) -> Verdict:
        if logic is None:
            return and3(r["verdict"] for r in here)
        rs = [*own, *here]
        nodes = logic.verdicts((base or {}) | _env(rs))
        units.append(nodes)
        return logic.root_verdict(nodes, [r["verdict"] for r in rs if not logic.covers(r)])

    verdicts = [r["verdict"] for r in conditions]
    whole: Verdict | None = None  # вердикт по графу
    code: str | None = None
    placements_out: list[dict[str, Any]] = []
    if window_conds:
        ps = rt.get("placement_set")
        placements = rt.get("placements") or []
        if not ps or not placements or ps.get("completeness") != "complete":
            code = "no_placements" if not ps or not placements else "placements_incomplete"
            missing = [_result(c, Verdict.unknown, code) for c in window_conds]
            conditions += missing
            verdicts.append(Verdict.unknown)
            whole = combine(missing) if logic else None
        else:
            by_subject: dict[str, dict[str, list[Fact]]] = {
                c.feature_id: defaultdict(list) for c in window_conds
            }
            for c in window_conds:
                for f in facts[c.feature_id]:
                    by_subject[c.feature_id][f.subject_id].append(f)
            per_cond: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for p in placements:
                b, floor = p["building_id"], p["floor"]
                here = []
                for i, c in enumerate(window_conds):
                    if c.feature_id in PLACEMENT_FIELDS:
                        r = _placement_condition(c, p)
                    else:
                        # Окно — факт этажа фасада или факт всего фасада («окна во двор»).
                        subj = by_subject[c.feature_id]
                        windows = [
                            [
                                *subj.get(window_subject(b, w, floor), []),
                                *subj.get(facade_subject(b, w), []),
                            ]
                            for w in p["windows"]
                        ]
                        r = _window_condition(c, windows, hard)
                    per_cond[i].append(r)
                    here.append(r)
                placements_out.append(
                    {
                        "placement_id": p["placement_id"],
                        "building_id": b,
                        "floor": floor,
                        "floor_label": p.get("floor_label"),
                        "verdict": combine(here),
                    }
                )
            # Все условия — на одном размещении: сначала внутри размещения, потом по ним.
            category_window = _across([p["verdict"] for p in placements_out])
            for i, c in enumerate(window_conds):
                rs = per_cond[i]
                v = _across([r["verdict"] for r in rs])
                ids = [x for r in rs for x in r["fact_ids"]]
                if v == Verdict.unknown and any(r["verdict"] == Verdict.yes for r in rs):
                    conditions.append(_result(c, v, "depends_on_room", fact_ids=ids))
                elif v == Verdict.unknown:
                    rc = next((r["reason_code"] for r in rs if r.get("reason_code")), "unknown")
                    conditions.append(_result(c, v, rc, fact_ids=ids))
                else:
                    conditions.append(_result(c, v, fact_ids=ids))
            if (
                logic is None
                and category_window == Verdict.no
                and all(r["verdict"] != Verdict.no for r in conditions[len(category_conds) :])
            ):
                # По отдельности условия где-то выполняются, но вместе — ни на одном размещении.
                code = "no_single_placement"
            elif category_window == Verdict.unknown:
                yes_somewhere = any(p["verdict"] == Verdict.yes for p in placements_out)
                if yes_somewhere and ps.get("guarantee") not in GUARANTEED:
                    code = "not_guaranteed"
                elif yes_somewhere:
                    code = "depends_on_room"
            verdicts.append(category_window)
            whole = category_window
    elif logic:
        whole = combine([])
    verdict = and3(verdicts) if logic is None else whole or Verdict.unknown
    out: dict[str, Any] = {
        "room_type_id": rt["room_type_id"],
        "name": rt.get("name"),
        "verdict": verdict,
        "conditions": conditions,
        "placements": placements_out,
        "placement_status": (rt.get("placement_set") or {}).get("status"),
    }
    if code and verdict != Verdict.yes:
        out |= {"reason_code": code, "reason": reason_text(code)}
    if logic is not None:
        # Узел графа по категории: «да» на всех размещениях → да, «нет» на всех → нет.
        out["_logic"] = {n: _across([u.get(n, Verdict.unknown) for u in units]) for n in units[0]}
    return out


def _score(soft: list[dict[str, Any]]) -> dict[str, float]:
    score_min = sum(s["weight"] for s in soft if s["verdict"] == Verdict.yes)
    score_max = score_min + sum(s["weight"] for s in soft if s["verdict"] == Verdict.unknown)
    return {"min": round(score_min, 3), "max": round(score_max, 3)}


def search(
    store: Store, ast: QueryAST, only: Collection[str] | None = None
) -> list[dict[str, Any]]:
    hotels = store.hotels(ast.city_id or None)
    if only is not None:
        hotels = [h for h in hotels if h["hotel_id"] in only]
    ids = [h["hotel_id"] for h in hotels]
    # Один запрос к хранилищу на характеристику, а не на отель.
    features = {c.feature_id for c in [*ast.hard, *ast.soft]}
    facts = {fid: store.facts_for(fid, ids) for fid in features}
    room_types = store.room_types(ids) if features else {}
    logic = Logic.of(ast)

    results = []
    for hotel in hotels:
        hid = hotel["hotel_id"]
        own = {fid: facts[fid].get(hid, []) for fid in features}
        levels = {
            fid: "window" if fid in PLACEMENT_FIELDS else _level(fs) for fid, fs in own.items()
        }

        def split(conds: list[Condition], *, levels: dict[str, str] = levels) -> tuple:
            hotel_c = [c for c in conds if levels[c.feature_id] == "hotel"]
            cat_c = [c for c in conds if levels[c.feature_id] == "category"]
            win_c = [c for c in conds if levels[c.feature_id] == "window"]
            return hotel_c, cat_c, win_c

        hard_hotel, hard_cat, hard_win = split(list(ast.hard))
        soft_hotel, soft_cat, soft_win = split(list(ast.soft))
        hard = [evaluate(own[c.feature_id], c, hard=True) for c in hard_hotel]
        # Обязательная часть запроса вне каталога (D34) не засчитывается как выполненная;
        # часть вне каталога внутри графа — «неизвестно» на своём месте в графе.
        hard += [
            {
                "atom_id": o.atom_id,
                "text_span": o.text_span,
                "verdict": Verdict.unknown,
                "reason_code": "out_of_catalog",
                "reason": reason_text("out_of_catalog"),
                "fact_ids": [],
            }
            for o in ast.out_of_catalog
            if o.requiredness == "required" or (logic is not None and o.atom_id in logic.atoms)
        ]
        soft = [
            evaluate(own[c.feature_id], c, hard=False) | {"weight": c.weight} for c in soft_hotel
        ]

        categories: list[dict[str, Any]] = []
        best: dict[str, Any] | None = None
        nodes: dict[str, Verdict] = {}
        if hard_cat or hard_win or soft_cat or soft_win:
            rts = room_types.get(hid, [])
            if not rts:
                code = "no_placements" if (hard_win or soft_win) else "no_room_types"
                hard += [_result(c, Verdict.unknown, code) for c in [*hard_cat, *hard_win]]
                soft += [
                    _result(c, Verdict.unknown, code) | {"weight": c.weight}
                    for c in [*soft_cat, *soft_win]
                ]
            else:
                base = _env(hard)
                # Условия отеля вне графа — через И; условия в графе уже внутри категории.
                outer = [h["verdict"] for h in hard if logic is None or not logic.covers(h)]
                for rt in rts:
                    cat = evaluate_category(
                        rt, hard_cat, hard_win, own, hard=True, logic=logic, base=base
                    )
                    cat["verdict"] = and3([*outer, cat["verdict"]])
                    cat["status"] = guest_status([cat["verdict"]])
                    cat["status_label"] = GUEST_LABELS[cat["status"]]
                    categories.append(cat)
                categories.sort(key=lambda x: ORDER[x["status"]])
                best = categories[0]
                nodes = best.get("_logic", {})
                for cat in categories:
                    cat.pop("_logic", None)
                hard += best["conditions"]
                if soft_cat or soft_win:
                    s = evaluate_category(
                        next(rt for rt in rts if rt["room_type_id"] == best["room_type_id"]),
                        soft_cat,
                        soft_win,
                        own,
                        hard=False,
                    )
                    weights = {c.feature_id: c.weight for c in [*soft_cat, *soft_win]}
                    soft += [r | {"weight": weights[r["feature_id"]]} for r in s["conditions"]]

        if logic is None:
            status = best["status"] if best else guest_status(h["verdict"] for h in hard)
        else:
            if best is None:
                nodes = logic.verdicts(_env(hard))
                loose = [h["verdict"] for h in hard if not logic.covers(h)]
                status = guest_status([logic.root_verdict(nodes, loose)])
            else:
                status = best["status"]
            hard = logic.items(nodes, hard) + [h for h in hard if not logic.covers(h)]
        results.append(
            {
                "hotel": hotel,
                "status": status,
                "status_label": GUEST_LABELS[status],
                "hard": hard,
                "soft": soft,
                "score": _score(soft),
                "categories": categories,
                "best_category": best["room_type_id"] if best else None,
            }
        )
    results.sort(key=lambda r: (ORDER[r["status"]], -r["score"]["max"], -r["score"]["min"]))
    return results
