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
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from typing import Any

from agents.query_ast import Condition, LogicNode, OutOfCatalog, QueryAST
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
    "visible",  # вид и солнце (V01, V05): {"visible": bool, "why": …}
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
COMPASS = ("С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ")
# Параметры, без которых факт отвечает на другой вопрос (D15): закат на дату — не «закат вообще».
# Для запроса без даты у окна есть свой факт: «нет» — только если закат за стеной весь год (D40).
KEY_PARAMS = {"V05": ("date",)}
WINDOW_IDS_PER_WINDOW = 2  # в fact_ids — не больше стольких фактов одного окна
WINDOW_IDS_MAX = 60
ALL_FLOORS = 0  # факт про весь фасад (D39): все этажи


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


# Числа «не меньше»: места карт отмечены не все (калькулятор `geo.place_count`) — меньше нужного
# по картам не значит «нет».
LOWER_BOUND = ("geo.place_count@",)


def _below_bound(f: Fact, op: str, ok: bool | None) -> bool:
    return (
        ok is False and op in ("gte", "gt") and (f.algorithm_version or "").startswith(LOWER_BOUND)
    )


def _unspecified(op: str, left: Any, right: Any) -> bool:
    """«Есть, тип не назван» против конкретного типа: не «нет», а «неизвестно» (D39)."""
    if left != PRESENT or op not in ("eq", "ne", "in"):
        return False
    wanted = right if isinstance(right, list) else [right]
    return any(w not in (PRESENT, ABSENT) for w in wanted)


def _params_match(fact: Fact, params: dict[str, Any]) -> bool:
    """Параметры запроса совпадают с параметрами факта; параметр-ключ (`KEY_PARAMS`) у факта
    есть, только если он есть и в запросе: факт на дату не отвечает на вопрос без даты (D15)."""
    if any(fact.params.get(k) != v for k, v in params.items()):
        return False
    return all(k in params or k not in fact.params for k in KEY_PARAMS.get(fact.feature_id, ()))


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
        out = _result(c, Verdict.unknown, _not_hard_code(known, facts), fact_ids=ids)
        if known:
            # Куда склоняются оценка или заявление Островка не для всех номеров: только для
            # порядка внутри «Нужно уточнить», вердикт остаётся «неизвестно».
            out["leaning"] = evaluate(known, c, hard=False)["verdict"]
        return out

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
    bound = False  # по картам меньше нужного, но число «не меньше»
    for fs in places.values():
        results: set[bool | None] = set()
        vague = False  # «есть, тип не назван» (D39) — не спорит с конкретным фактом того же места
        for f in fs:
            left = _number(f, c.field)
            if _unspecified(c.op, left, c.value):
                vague = True
            elif _below_bound(f, c.op, ok := _compare(c.op, left, c.value)):
                bound = True
                results.add(None)
            else:
                results.add(ok)
        if vague and not results & {True, False}:
            unspecified = True
            results.add(None)
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
    if bound:
        return _result(c, Verdict.unknown, "count_lower_bound", leaning=Verdict.no, **extra)
    return _result(c, Verdict.unknown, "not_comparable", **extra)


# «Рядом место типа X» (D34): пешком обычно на треть дальше, чем по прямой.
DETOUR = 1.3
PLACE_WEIGHT = 0.5  # пожелание «рядом X» в ранжировании — наравне со средним желательным


def place_item(o: OutOfCatalog, found: dict[str, Any] | None, known: int) -> dict[str, Any]:
    """Часть запроса «рядом место типа X» по местам пакета (PostGIS на запросе, D41).

    «Да» — место категории в радиусе с запасом на обход, с паспортом места и расстоянием.
    «Нет» не говорим: места в данных — не перепись (v11); не нашли — «неизвестно», и это
    склоняет вниз в ранжировании, а не отсекает."""
    place = o.place
    assert place is not None
    out: dict[str, Any] = {
        "atom_id": o.atom_id,
        "text_span": o.text_span,
        "label": place.label,
        "verdict": Verdict.unknown,
        "fact_ids": [],
        "nearby": {"category": place.category, "radius_m": place.radius_m},
    }

    def why(code: str) -> dict[str, Any]:
        return {"reason_code": code, "reason": reason_text(code)}

    if place.on_site or not place.radius_m:
        return out | why("place_on_site")
    if not known:
        return out | why("place_no_data")
    if found is None:
        return out | why("place_not_found") | {"leaning": Verdict.no}
    out["place"] = found
    if found["distance_m"] * DETOUR > place.radius_m:
        return out | why("place_far_on_foot") | {"leaning": Verdict.yes}
    if place.quality:
        # Место есть, а «хорошее» ли — оценок у нас нет (v11: рейтинг не факт без снимка).
        return out | why("place_quality_unknown") | {"leaning": Verdict.yes}
    return out | {"verdict": Verdict.yes}


WALK_M_PER_MIN = 80  # как в каталоге: «10 минут пешком» ≈ 800 м
TRIP_NEAR_M, TRIP_FAR_M = 2000, 8000  # по прямой: ближе — склоняется к «да», дальше — к «нет»


def trip_item(o: OutOfCatalog, target: dict[str, Any] | None, distance: int | None) -> dict:
    """«Доехать до X» по своим данным (D34): цель — место города по названию, расстояние по
    прямой и время пешком с запасом на обход. Это оценка (D18): время на транспорте мы пока не
    считаем, поэтому «неизвестно», а ближе к цели — выше в выдаче."""
    out: dict[str, Any] = {
        "atom_id": o.atom_id,
        "text_span": o.text_span,
        "label": f"до {o.normalized}" if o.normalized else o.text_span,
        "verdict": Verdict.unknown,
        "fact_ids": [],
    }
    if target is None:
        return out | {"reason_code": "trip_target_not_found",
                      "reason": reason_text("trip_target_not_found")}  # fmt: skip
    if distance is None:  # у отеля нет точки — расстояние не посчитать, в конец выдачи
        return out | {"reason_code": "trip_distance_unknown", "leaning": Verdict.no,
                      "reason": reason_text("trip_distance_unknown")}  # fmt: skip
    walk = round(distance * DETOUR / WALK_M_PER_MIN)
    leaning = (
        Verdict.yes if distance <= TRIP_NEAR_M else Verdict.no if distance > TRIP_FAR_M else None
    )
    return out | {
        "reason_code": "trip_estimate",
        "reason": reason_text("trip_estimate"),
        "trip": {
            "place_id": target["place_id"],
            "name": target["name"],
            "distance_m": distance,
            "walk_min": walk,
        },  # fmt: skip
        "leaning": leaning,
    }


def _trip_rank(items: list[dict[str, Any]]) -> int:
    """Ближе к цели поездки — выше; без цели — в конце."""
    trips = [i["trip"]["distance_m"] for i in items if i.get("trip")]
    return sum(trips) if trips else 10**9


def _not_hard_code(known: list[Fact], facts: list[Fact]) -> str:
    """Почему значение есть, а обязательное условие не закрыто: оценка — не то же, что
    заявление Островка не для всех номеров или расчёт с оговоркой."""
    if not known:
        return facts[0].unknown_reason or "unknown"
    statuses = {f.status for f in known}
    if Status.estimate in statuses:
        return "estimate_only"
    if Status.ostrovok_content in statuses:
        return "ostrovok_partial"
    return "not_hard_eligible"


# Заявление отеля обо всех номерах: ответ отеля (и имитатора) или проверка командой на уровне
# отеля относится к каждой категории, пока у категории нет своего факта того же места.
HOTEL_WIDE = frozenset({Status.hotelier, Status.simulated_hotelier, Status.curated})


def _category_facts(facts: list[Fact], room_type_id: str) -> list[Fact]:
    out = []
    for f in facts:
        if f.subject_id == room_type_id:
            out.append(f)
        elif f.subject_level == SubjectLevel.hotel and f.status in HOTEL_WIDE:
            # Место факта — категория: ответ отеля и свой факт категории сравниваются как одно
            # место (расхождение — «противоречие», а не «зависит от корпуса»).
            out.append(f.model_copy(update={"subject_id": room_type_id}))
    return out


def _placed(rt: dict[str, Any]) -> bool:
    return bool(rt.get("placement_set") and rt.get("placements"))


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


def _side(f: Fact) -> str:
    """Сторона света фасада окна (из паспорта расчёта), «?» — если её нет."""
    detail = f.depends_on[0] if f.depends_on and isinstance(f.depends_on[0], dict) else {}
    az = detail.get("facade_azimuth_deg")
    return "?" if az is None else COMPASS[int((float(az) + 22.5) // 45) % 8]


def _window_floor(subject_id: str) -> int:
    m = re.search(r":floor(\d+)$", subject_id)
    return int(m.group(1)) if m else ALL_FLOORS


def hotel_windows(c: Condition, facts: list[Fact], hard: bool) -> dict[str, Any]:
    """Условие окна, когда категории ещё не привязаны к фасадам и этажам (D17).

    Ответ по отелю — всегда «неизвестно»: какие корпуса у отеля, мы знаем не полностью, а где
    будет номер, скажет только отель. В пункте — `windows`: где по расчёту «да» и «нет»
    (сторона и этажи) и где «да» только по оценке (D18); `fact_ids` — факты этих окон.
    """
    windows: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        if f.subject_level in WINDOW_LEVELS and _params_match(f, c.params):
            windows[f.subject_id].append(f)
    if not windows:
        any_window = any(f.subject_level in WINDOW_LEVELS for f in facts)
        return _result(c, Verdict.unknown, "no_fact_params" if any_window else "no_placements")
    found: dict[str, dict[str, set[int]]] = {
        "yes": defaultdict(set),
        "no": defaultdict(set),
        "estimate": defaultdict(set),
    }
    ids: dict[str, list[str]] = {"yes": [], "no": [], "estimate": []}
    # Окна считаем поштучно: на одной стороне и этаже бывает несколько стен (фасады `seg-…`).
    counts, reasons, estimate_no = Counter(), Counter(), 0
    for subject, fs in windows.items():
        # Где «да» по расчёту — только по фактам, которые закрывают обязательное условие; оценка
        # отдельно — и для желательного условия тоже, чтобы не выдать её за расчёт.
        r = evaluate(fs, c, hard=True)
        kind = {Verdict.yes: "yes", Verdict.no: "no"}.get(r["verdict"])
        if kind is None:
            base = next((f.unknown_reason for f in fs if f.status == Status.unknown), None)
            reasons[base or r.get("reason_code")] += 1
            if r.get("reason_code") == "estimate_only":
                guess = evaluate(fs, c, hard=False)
                kind = "estimate" if guess["verdict"] == Verdict.yes else None
                estimate_no += guess["verdict"] == Verdict.no
                r = guess
        if kind is not None:
            counts[kind] += 1
            found[kind][_side(fs[0])].add(_window_floor(subject))
            ids[kind] += r["fact_ids"][:WINDOW_IDS_PER_WINDOW]
    summary: dict[str, Any] = {
        kind: {side: sorted(floors) for side, floors in sorted(sides.items())}
        for kind, sides in found.items()
    }
    summary |= {"count": len(windows), "want": c.value}
    n_no = counts["no"]
    if not found["yes"] and not found["estimate"] and estimate_no:
        # Точно «да» нигде, по оценке — тоже нигде: так и скажем, а не «есть только оценка».
        summary["estimate_none"] = estimate_no + n_no == len(windows)
    if found["yes"]:
        all_yes = counts["yes"] == len(windows)
        code, shown = ("window_all" if all_yes else "window_some"), ids["yes"]
    elif n_no == len(windows):
        code, shown = "no_window_known", ids["no"]
    elif found["estimate"]:
        code, shown = "estimate_only", ids["estimate"]
    else:
        top = reasons.most_common(1)
        code, shown = (top[0][0] if top else None) or "unknown", []
    return _result(c, Verdict.unknown, code, fact_ids=shown[:WINDOW_IDS_MAX], windows=summary)


# Внутри одного статуса — сначала отели, где условие окна по расчёту выполняется хотя бы
# в части окон, потом — по оценке; в конце — где по расчёту ни из одного окна (D40).
WINDOWS_RANK = {"window_all": 4, "window_some": 3, "no_window_known": 0}


def _windows_rank(items: list[dict[str, Any]]) -> int:
    ranks = []
    for item in items:
        w = item.get("windows")
        if not w or item.get("negated"):
            continue
        code = item.get("reason_code")
        if code in WINDOWS_RANK:
            ranks.append(WINDOWS_RANK[code])
        elif w.get("estimate"):
            ranks.append(2)
        else:
            ranks.append(0 if w.get("estimate_none") else 1)
    return min(ranks) if ranks else 1  # условий окна несколько — по худшему


def _hard_rank(items: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Внутри статуса: больше обязательных «да», меньше «неизвестно» — выше (а не по алфавиту);
    среди «неизвестно» выше те, где оценка или Островок говорят «да», ниже — где «нет»."""
    yes = sum(1 for h in items if h.get("verdict") == Verdict.yes)
    unknown = [h for h in items if h.get("verdict") == Verdict.unknown]
    leaning = sum({Verdict.yes: 1, Verdict.no: -1}.get(h.get("leaning"), 0) for h in unknown)
    return -yes, len(unknown), -leaning


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
        evaluate(_category_facts(facts[c.feature_id], rt["room_type_id"]), c, hard)
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
            missing = [
                # Категория не привязана к корпусам — сводка по окнам корпусов отеля (D17, D40).
                hotel_windows(c, facts[c.feature_id], hard)
                if code == "no_placements" and c.feature_id not in PLACEMENT_FIELDS
                else _result(c, Verdict.unknown, code)
                for c in window_conds
            ]
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
    wanted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in [*ast.hard, *ast.soft]:
        if c.params not in wanted[c.feature_id]:
            wanted[c.feature_id].append(c.params)
    # Параметры одни на все условия характеристики — хранилище отберёт факты по ним (даты).
    facts = {
        fid: store.facts_for(
            fid,
            ids,
            params=wanted[fid][0] if len(wanted[fid]) == 1 else None,
            key_params=KEY_PARAMS.get(fid, ()),
        )
        for fid in features
    }
    room_types = store.room_types(ids) if features else {}
    logic = Logic.of(ast)
    # «Доехать до X» — цель ищется один раз, расстояния — одним запросом на все отели.
    trips: dict[int, tuple[dict[str, Any] | None, dict[str, int]]] = {}
    for o in ast.out_of_catalog:
        if "trip_to_target" in o.nearest_contracts and ast.city_id:
            target = store.find_target(ast.city_id, o.normalized or o.text_span)
            dist = store.distances(ids, target) if target else {}
            trips[id(o)] = (target, dist)
    # «Рядом место типа X» — один запрос к местам на часть запроса, а не на отель.
    places = {
        id(o): store.nearest_places(ids, o.place.category, o.place.radius_m)
        for o in ast.out_of_catalog
        if o.place and not o.place.on_site and o.place.radius_m
    }

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
        soft = [
            evaluate(own[c.feature_id], c, hard=False) | {"weight": c.weight} for c in soft_hotel
        ]
        # Часть запроса вне каталога (D34): «рядом место типа X» считается по местам пакета;
        # остальное в обязательной части не засчитывается — «неизвестно» на своём месте в графе.
        for o in ast.out_of_catalog:
            required = o.requiredness == "required" or (
                logic is not None and o.atom_id in logic.atoms
            )
            if o.place:
                found, known = places.get(id(o), ({}, 0))
                item = place_item(o, found.get(hid), known)
                if required:
                    hard.append(item)
                else:
                    soft.append(item | {"weight": PLACE_WEIGHT})
            elif id(o) in trips:
                target, dist = trips[id(o)]
                item = trip_item(o, target, dist.get(hid))
                if required:
                    hard.append(item)
                else:
                    soft.append(item | {"weight": PLACE_WEIGHT})
            elif required:
                hard.append(
                    {
                        "atom_id": o.atom_id,
                        "text_span": o.text_span,
                        "verdict": Verdict.unknown,
                        "reason_code": "out_of_catalog",
                        "reason": reason_text("out_of_catalog"),
                        "fact_ids": [],
                    }
                )

        categories: list[dict[str, Any]] = []
        best: dict[str, Any] | None = None
        nodes: dict[str, Verdict] = {}
        if hard_cat or hard_win or soft_cat or soft_win:
            rts = room_types.get(hid, [])
            if (hard_win or soft_win) and not any(_placed(rt) for rt in rts):
                # Ни одна категория не привязана к корпусам и этажам — условия окна по всем окнам
                # корпусов отеля (D17, D40): сводка, где «да» и где «нет».
                hard += [hotel_windows(c, own[c.feature_id], hard=True) for c in hard_win]
                soft += [
                    hotel_windows(c, own[c.feature_id], hard=False) | {"weight": c.weight}
                    for c in soft_win
                ]
                hard_win, soft_win = [], []
            if not rts:
                hard += [_result(c, Verdict.unknown, "no_room_types") for c in hard_cat]
                soft += [
                    _result(c, Verdict.unknown, "no_room_types") | {"weight": c.weight}
                    for c in soft_cat
                ]
            elif hard_cat or hard_win or soft_cat or soft_win:
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
                categories.sort(key=lambda x: (ORDER[x["status"]], *_hard_rank(x["conditions"])))
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
    results.sort(
        key=lambda r: (
            ORDER[r["status"]],
            -_windows_rank(r["hard"]),
            *_hard_rank(r["hard"]),
            _trip_rank(r["hard"] + r["soft"]),
            -r["score"]["max"],
            -r["score"]["min"],
        )
    )
    return results
