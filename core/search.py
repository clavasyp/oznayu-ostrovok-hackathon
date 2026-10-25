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

Отель подходит, если подходит хотя бы одна категория. Оценка не закрывает обязательное
условие (D18), неизвестное не становится «нет» (D5). Если на одном месте пригодные факты
разных источников расходятся, это противоречие (core §6): «нужно уточнить», ID фактов в ответе.
Причины — кодом (`reason_code`) и текстом для экрана (`reason`, `core/reasons.py`).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from typing import Any

from agents.query_ast import Condition, QueryAST
from core.facts import Fact, Status, SubjectLevel
from core.reasons import reason_text
from core.store import Store
from core.verdict import GUEST_LABELS, GuestStatus, Verdict, and3, guest_status, or3

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


def example_ast() -> QueryAST:
    return QueryAST.model_validate(json.loads(EXAMPLE_AST.read_text("utf-8")))


def window_subject(building_id: str, facade_id: str, floor: int) -> str:
    """ID окна как в расчётах v9: «здание:фасад:floorN»."""
    return f"{building_id}:{facade_id}:floor{floor}"


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
            "exists": lambda: True,
        }[op]()
    except TypeError:
        return None


def _params_match(fact: Fact, params: dict[str, Any]) -> bool:
    return all(fact.params.get(k) == v for k, v in params.items())


def _place(fact: Fact) -> tuple[str, str]:
    """Место факта: субъект и параметры. Разные рецепты одного места — кандидаты на противоречие."""
    return fact.subject_id, json.dumps(fact.params, sort_keys=True, ensure_ascii=False, default=str)


def _result(c: Condition, verdict: Verdict, code: str | None = None, **extra: Any) -> dict:
    out: dict[str, Any] = {
        "feature_id": c.feature_id,
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
    for fs in places.values():
        results = {_compare(c.op, _number(f, c.field), c.value) for f in fs}
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


def evaluate_category(
    rt: dict[str, Any],
    category_conds: list[Condition],
    window_conds: list[Condition],
    facts: dict[str, list[Fact]],
    hard: bool,
) -> dict[str, Any]:
    """Категория номера: её собственные факты и размещения «здание × этаж × окна» (D17)."""
    conditions = [
        evaluate([f for f in facts[c.feature_id] if f.subject_id == rt["room_type_id"]], c, hard)
        for c in category_conds
    ]
    verdicts = [r["verdict"] for r in conditions]
    code: str | None = None
    placements_out: list[dict[str, Any]] = []
    if window_conds:
        ps = rt.get("placement_set")
        placements = rt.get("placements") or []
        if not ps or not placements:
            code = "no_placements"
            conditions += [_result(c, Verdict.unknown, code) for c in window_conds]
            verdicts.append(Verdict.unknown)
        elif ps.get("completeness") != "complete":
            code = "placements_incomplete"
            conditions += [_result(c, Verdict.unknown, code) for c in window_conds]
            verdicts.append(Verdict.unknown)
        else:
            by_subject: dict[str, dict[str, list[Fact]]] = {
                c.feature_id: defaultdict(list) for c in window_conds
            }
            for c in window_conds:
                for f in facts[c.feature_id]:
                    by_subject[c.feature_id][f.subject_id].append(f)
            per_cond: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for p in placements:
                subjects = [window_subject(p["building_id"], w, p["floor"]) for w in p["windows"]]
                here = []
                for i, c in enumerate(window_conds):
                    windows = [by_subject[c.feature_id].get(s, []) for s in subjects]
                    r = _window_condition(c, windows, hard)
                    per_cond[i].append(r)
                    here.append(r)
                placements_out.append(
                    {
                        "placement_id": p["placement_id"],
                        "building_id": p["building_id"],
                        "floor": p["floor"],
                        "floor_label": p.get("floor_label"),
                        "verdict": and3(r["verdict"] for r in here),
                    }
                )
            # Все условия — на одном размещении: сначала AND внутри размещения, потом по ним.
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
            if category_window == Verdict.no and all(
                r["verdict"] != Verdict.no for r in conditions[len(category_conds) :]
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
    verdict = and3(verdicts)
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
    return out


def _any_of(ast: QueryAST, conds: list[Condition], results: list[dict]) -> list[dict]:
    """OR-группы обязательных условий уровня отеля из графа `logic` AST: группа «да», если «да»
    хотя бы одно её условие (трёхзначно, `or3`). Пример — «заселиться ночью»: круглосуточная
    стойка ИЛИ самозаселение. Остальной граф пока — AND, как без `logic`."""
    if not ast.logic:
        return results
    nodes = {n.node_id: n for n in ast.logic}
    by_atom = {c.atom_id: i for i, c in enumerate(conds) if c.atom_id}
    grouped: set[int] = set()
    merged = []
    for n in ast.logic:
        if n.op != "OR":
            continue
        idx = [
            by_atom[nodes[ch].atom_id]
            for ch in n.children
            if ch in nodes and nodes[ch].op == "ATOM" and nodes[ch].atom_id in by_atom
        ]
        if len(idx) < 2:
            continue
        grouped.update(idx)
        rs = [results[i] for i in idx]
        verdict = or3(r["verdict"] for r in rs)
        code = next((r.get("reason_code") for r in rs if r.get("reason_code")), "unknown")
        extra = {"fact_ids": [x for r in rs for x in r["fact_ids"]], "any_of": rs}
        merged.append(
            _result(conds[idx[0]], verdict, code if verdict == Verdict.unknown else None, **extra)
        )
    return [r for i, r in enumerate(results) if i not in grouped] + merged


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

    results = []
    for hotel in hotels:
        hid = hotel["hotel_id"]
        own = {fid: facts[fid].get(hid, []) for fid in features}
        levels = {fid: _level(fs) for fid, fs in own.items()}

        def split(conds: list[Condition], *, levels: dict[str, str] = levels) -> tuple:
            hotel_c = [c for c in conds if levels[c.feature_id] == "hotel"]
            cat_c = [c for c in conds if levels[c.feature_id] == "category"]
            win_c = [c for c in conds if levels[c.feature_id] == "window"]
            return hotel_c, cat_c, win_c

        hard_hotel, hard_cat, hard_win = split(list(ast.hard))
        soft_hotel, soft_cat, soft_win = split(list(ast.soft))
        hard = [evaluate(own[c.feature_id], c, hard=True) for c in hard_hotel]
        hard = _any_of(ast, hard_hotel, hard)
        # Обязательная часть запроса вне каталога (D34) не засчитывается как выполненная.
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
            if o.requiredness == "required"
        ]
        soft = [
            evaluate(own[c.feature_id], c, hard=False) | {"weight": c.weight} for c in soft_hotel
        ]

        categories: list[dict[str, Any]] = []
        best: dict[str, Any] | None = None
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
                hotel_verdicts = [h["verdict"] for h in hard]
                for rt in rts:
                    cat = evaluate_category(rt, hard_cat, hard_win, own, hard=True)
                    cat["verdict"] = and3([*hotel_verdicts, cat["verdict"]])
                    cat["status"] = guest_status([cat["verdict"]])
                    cat["status_label"] = GUEST_LABELS[cat["status"]]
                    categories.append(cat)
                categories.sort(key=lambda x: ORDER[x["status"]])
                best = categories[0]
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

        status = best["status"] if best else guest_status(h["verdict"] for h in hard)
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
