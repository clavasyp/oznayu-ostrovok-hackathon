"""Объяснение выдачи (задача 3, core §7.3): «закат виден, но только с 5-го этажа».

Модель не пишет текст и не знает цифр (D8):
- код собирает **утверждения** с ID из результата поиска (`core/search.py`): вердикт условия,
  значение из факта с паспортом, причину «неизвестно», этажи размещений;
- модель возвращает только **порядок ID и связки** из короткого списка («но», «при этом»…);
- код собирает текст, **валидатор** сверяет каждое число в тексте с утверждениями и что ни одно
  обязательное утверждение не пропало. Не сошлось, нет ключа, модель недоступна — шаблон.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agents.catalog import Catalog, load_catalog
from agents.hotelier.schemas import label as field_label
from agents.llm import Gateway, LLMError, get_gateway
from agents.query_ast import QueryAST
from core.facts import Fact, Status

Kind = Literal["fails", "unknown", "floors", "fits", "out_of_catalog", "soft"]
# Порядок шаблона: сначала то, что мешает, потом то, что уточнить, потом то, что подходит.
TEMPLATE_ORDER: dict[str, int] = {
    "fails": 0,
    "out_of_catalog": 1,
    "unknown": 2,
    "floors": 3,
    "fits": 4,
    "soft": 5,
}
MUST = frozenset({"fails", "unknown", "floors", "out_of_catalog"})
LINKS = ("", "и", "но", "а", "при этом", "зато", "однако", "причём")
VERDICT_WORD = {"yes": "да", "no": "нет", "unknown": "нужно уточнить"}
STATUS_NOTE = {
    Status.estimate: "оценка",
    Status.simulated_hotelier: "ответ имитатора отельера",
    Status.hotelier: "со слов отеля",
    Status.ostrovok_content: "данные Островка",
    Status.curated: "проверено командой",
}
DEFAULT_FIELDS = ("distance_m", "count", "mean", "mean_C", "aboveground_levels", "visible")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class Claim:
    claim_id: str
    kind: Kind
    text: str
    fact_ids: list[str] = field(default_factory=list)

    @property
    def numbers(self) -> list[str]:
        return _NUM.findall(self.text)


@dataclass
class Explanation:
    text: str
    claims: list[Claim]
    by: Literal["llm", "template"]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "by": self.by,
            "notes": self.notes,
            "claims": [
                {"claim_id": c.claim_id, "kind": c.kind, "text": c.text, "fact_ids": c.fact_ids}
                for c in self.claims
            ],
        }


# --- Утверждения из результата поиска ---


def _fmt_number(x: float) -> str:
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.1f}".replace(".", ",")


def _fmt_value(value: Any, unit: str | None) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if unit == "m":
            return (
                f"{_fmt_number(round(value / 100) / 10)} км"
                if value >= 1000
                else f"{round(value)} м"
            )
        unit_ru = {"°C": " °C", "%": "%", "min": " мин", "h": " ч", "kg": " кг", "mm": " мм"}
        return _fmt_number(round(value, 1)) + unit_ru.get(unit or "", "")
    if isinstance(value, str) and len(value) <= 40:
        return value
    return None


# Значения перечислений — словами, а не кодами: «dog_cat» → «пускают собак и кошек».
VALUE_WORDS: dict[str, dict[str, str]] = {
    "species": {"dog": "пускают собак", "cat": "пускают кошек", "dog_cat": "пускают собак и кошек",
                "any": "пускают любых животных", "none": "с животными нельзя",
                "present": "с животными можно, вид не указан"},
    "hvac_type": {"split": "сплит-система", "central": "центральный", "fan_coil": "фанкойлы",
                  "other": "другой тип"},
    "connector": {"type2": "Type 2", "ccs": "CCS", "chademo": "CHAdeMO", "gbt": "GB/T",
                  "schuko": "бытовая розетка", "other": "другой разъём"},
    "parking_owner": {"own": "своя", "third_party": "партнёрская", "street": "на улице"},
    "charging_basis": {"per_day": "за сутки", "per_stay": "за проживание", "free": "бесплатно"},
    "fee_basis": {"per_night": "за ночь", "per_stay": "за проживание"},
    "bed_configuration": {"double": "двуспальная", "twin": "раздельные",
                          "double_or_twin": "двуспальная или раздельные", "other": "другая"},
    "opaque_coverage": {"full": "полностью", "partial": "частично",
                        "present": "шторы есть, закрывают ли полностью — не указано"},
    "windows_state": {"closed": "окна закрыты", "open": "окна открыты"},
    "hvac_state": {"on": "кондиционер включён", "off": "кондиционер выключен"},
    "surface": {"sand": "песок", "pebble": "галька", "rock": "камни", "mixed": "смешанный"},
    "flag": {"green": "зелёный флаг", "yellow": "жёлтый флаг", "red": "красный флаг",
             "closed": "пляж закрыт"},
}  # fmt: skip
PRESENCE_WORDS = {"present": "есть", "none": "нет"}  # «лифт есть», «кроватки нет»


def value_words(field_name: str | None, value: Any) -> str | None:
    """Код перечисления → слова; не перечисление — None."""
    if not isinstance(value, str):
        return None
    return VALUE_WORDS.get(field_name or "", {}).get(value) or PRESENCE_WORDS.get(value)


def _indirect(item: dict[str, Any]) -> bool:
    """Условие не прямое: «не равно», «= нет / ложь», множество с «нет» — так правила
    переворачивают «без собак» (species ∈ {кошки, нет}). «≠ нет» — это «есть X» (кондиционер,
    лифт, кроватка): прямое условие, подпись — название характеристики, а не «Лифтом: да»."""
    op, value = item.get("cond_op"), item.get("cond_value")
    return (
        (op == "ne" and value != "none")
        or (op == "eq" and (value is False or value == "none"))
        or (op == "in" and isinstance(value, list) and "none" in value)
    )


def _evidence(item: dict[str, Any], catalog: Catalog, fact: Callable[[str], Fact | None]) -> str:
    """Значение из первого факта условия и его происхождение: «810 м, оценка»."""
    f = next((x for fid in item.get("fact_ids", []) if (x := fact(fid))), None)
    if f is None:
        return ""
    feature = catalog.get(item.get("feature_id", ""))
    field_name = item.get("field") or (feature.default_field if feature else None)
    v = f.value
    if isinstance(v, dict):
        v = (
            v.get(field_name)
            if field_name
            else next((v[k] for k in DEFAULT_FIELDS if k in v), None)
        )
    spec = feature.field_spec(field_name) if feature else None
    shown = value_words(field_name, v) or _fmt_value(v, spec.unit if spec else None)
    # «есть только оценка» скажет причина — второй раз «оценка» не нужна.
    note = None if item.get("reason_code") == "estimate_only" else STATUS_NOTE.get(f.status)
    return ", ".join(x for x in (shown, note) if x)


UNKNOWN_SEP = ": нужно уточнить"


SIDES = {"С": "север", "СВ": "северо-восток", "В": "восток", "ЮВ": "юго-восток", "Ю": "юг",
         "ЮЗ": "юго-запад", "З": "запад", "СЗ": "северо-запад"}  # fmt: skip


def _where(sides: dict[str, list[int]]) -> str:
    """{"ЮЗ": [7, 8, 9], "З": [0]} → «окна на юго-запад, этажи 7–9; на запад — все этажи»."""
    parts = []
    for side, floors in sides.items():
        named = [f for f in floors if f]
        if not named or 0 in floors:
            where = "все этажи"
        else:
            where = f"этаж {named[0]}" if len(named) == 1 else f"этажи {_floor_ranges(named)}"
        parts.append(f"на {SIDES[side]}, {where}" if side in SIDES else where)
    return "; ".join(parts)


def _windows_evidence(item: dict[str, Any], catalog: Catalog) -> str:
    """Окна отеля без привязки категорий (`core.search.hotel_windows`): где условие гостя
    выполняется по расчёту и где — только по оценке.

    Полярность — как у гостя: «без вида на закат» (НЕ, или условие `visible = false`) —
    подходят окна, где заката нет."""
    w = item.get("windows") or {}
    negated = bool(item.get("negated"))
    good, bad = ("no", "yes") if negated else ("yes", "no")
    positive = (w.get("want") is not False) != negated  # подходят окна, где это есть
    feature = catalog.get(item.get("feature_id") or "")
    short = feature.short if feature else "это"
    covered = sum(len(f) for f in (w.get(bad) or {}).values()) == w.get("count")
    if w.get(good):
        where = _where(w[good])
        return (
            f"по расчёту — окна {where}" if positive else f"по расчёту {short}: нет — окна {where}"
        )
    if covered:  # «ни одно окно» скажет причина (`no_window_known`)
        return "" if positive else f"по расчёту {short} — во всех окнах"
    if positive and not negated and w.get("estimate"):
        return f"по оценке — окна {_where(w['estimate'])}"
    if positive and not negated and w.get("estimate_none"):
        return "по оценке — ни из одного окна"
    return ""


def _merge_heads(a: str, b: str) -> str:
    """Подписи с одной причиной: у одной характеристики — общая («можно с животным» и «можно
    с животным (масса)» → первая), у разных — через запятую."""
    if b.startswith(a) or b.lower() in a.lower():
        return a
    if a.startswith(b):
        return b
    return f"{a}, {b}"


def _label(item: dict[str, Any], catalog: Catalog) -> str:
    feature = catalog.get(item.get("feature_id") or "")
    # По направлению условия: «бары и клубы рядом» тому, кто просил от двух баров (ревью №21).
    short = feature.short_for(item.get("cond_op"), item.get("cond_value")) if feature else None
    label = short or item.get("text_span") or "условие"
    field_name = item.get("field")
    if (
        feature
        and feature.source == "hotelier"
        and field_name
        and field_name != feature.default_field
    ):
        label = f"{label} ({field_label(field_name).split(',')[0]})"  # «можно с животным (масса)»
    if item.get("op") == "OR" and item.get("text_span"):
        label = item["text_span"]
    if item.get("negated"):
        # Слова гостя («без собак»), а не «не можно с животным».
        span = (item.get("text_span") or "").strip()
        if span and re.match(r"(?:не|без|нет|никаких)\b", span.lower()):
            return span
        return f"не {span}" if span else f"не {label}"
    if _indirect(item) and (span := (item.get("text_span") or "").strip()):
        return span  # «без собак: нет», а не «можно с животным: нет»
    return label


def _condition_claim(
    claim_id: str,
    item: dict[str, Any],
    catalog: Catalog,
    fact: Callable[[str], Fact | None],
    soft: bool = False,
) -> Claim:
    verdict = str(item.get("verdict"))
    label = _label(item, catalog)
    if item.get("reason_code") == "out_of_catalog":
        text = f"«{item.get('text_span') or label}»: такого мы пока не знаем"
        return Claim(claim_id, "out_of_catalog", text)
    if item.get("windows"):
        evidence = _windows_evidence(item, catalog)
        if item.get("negated"):  # причина сказана про само условие, а не про «без него»
            item = item | {"reason": "уточним у отеля, где будет номер"}
    else:
        evidence = "" if item.get("items") else _evidence(item, catalog, fact)
    if verdict == "unknown":
        reason = (item.get("reason") or "").rstrip(".")
        reason = reason[:1].lower() + reason[1:] if reason else ""
        tail = ", ".join(x for x in (evidence, reason) if x)
        text = f"{label}{UNKNOWN_SEP}" + (f" — {tail}" if tail else "")
    else:
        text = f"{label}: {VERDICT_WORD.get(verdict, verdict)}" + (
            f" ({evidence})" if evidence else ""
        )
    kind: Kind = "soft" if soft else {"yes": "fits", "no": "fails"}.get(verdict, "unknown")  # type: ignore[assignment]
    return Claim(claim_id, kind, text, list(item.get("fact_ids", [])))


def _floor_ranges(floors: list[int]) -> str:
    """[3, 4, 5, 7] → «3–5, 7»."""
    out, start, prev = [], None, None
    for f in sorted(set(floors)):
        if start is None:
            start = prev = f
        elif f == prev + 1:
            prev = f
        else:
            out.append(f"{start}–{prev}" if start != prev else str(start))
            start = prev = f
    if start is not None:
        out.append(f"{start}–{prev}" if start != prev else str(start))
    return ", ".join(out)


def _floors_claim(
    claim_id: str,
    category: dict[str, Any],
    catalog: Catalog,
    label: Callable[[dict[str, Any]], str] | None = None,
) -> Claim | None:
    """«Только с 5-го этажа»: по размещениям лучшей категории — одни этажи подходят, другие нет.
    `label` — подпись условия, как у пункта выше («без вида на море», а не «вид на море»)."""
    by_floor: dict[int, set[str]] = {}
    for p in category.get("placements") or []:
        if p.get("floor") is not None:
            by_floor.setdefault(p["floor"], set()).add(str(p.get("verdict")))
    # Этаж «да» — только если «да» на всех его размещениях: номер на одной из стен (D41-3).
    yes = sorted(f for f, vs in by_floor.items() if vs == {"yes"})
    no = sorted(f for f, vs in by_floor.items() if vs == {"no"})
    if not yes or not no:
        return None
    window = [
        c
        for c in category.get("conditions", [])
        if str(c.get("feature_id", ""))[:1] in "VSN"
        or c.get("feature_id") in ("floor", "courtyard")
    ]
    label = label or (lambda c: _label(c, catalog))
    what = ", ".join(dict.fromkeys(label(c) for c in window)) or "условия окна"
    if min(yes) > max(no):
        where = f"только с {min(yes)}-го этажа"
    elif max(yes) < min(no):
        where = f"только до {max(yes)}-го этажа"
    else:
        where = f"только на этажах {_floor_ranges(yes)}"
    name = category.get("name")
    in_cat = f" в категории «{name}»" if name else ""
    return Claim(claim_id, "floors", f"{what}: {where}{in_cat}")


def build_claims(
    result: dict[str, Any],
    fact: Callable[[str], Fact | None],
    catalog: Catalog | None = None,
    max_soft: int = 2,
    ast: QueryAST | None = None,
) -> list[Claim]:
    """Утверждения о том, почему отель получил свой статус. Цифры — только из фактов."""
    catalog = catalog or load_catalog()
    claims: list[Claim] = []
    # Какое поле факта сравнивалось и как — из условия AST (в результате поиска этого нет).
    conds = {c.atom_id: c for c in [*ast.hard, *ast.soft] if c.atom_id} if ast else {}

    def with_field(item: dict[str, Any]) -> dict[str, Any]:
        c = conds.get(item.get("atom_id") or "")
        if c is None:
            return item
        extra = {"cond_op": c.op, "cond_value": c.value}
        if c.field and not item.get("field"):
            extra["field"] = c.field
        return item | extra

    def next_id() -> str:
        return f"c{len(claims) + 1}"

    def add(claim: Claim) -> None:
        same = next((c for c in claims if c.text == claim.text), None)
        if same is None and claim.kind == "unknown":
            # Одна и та же причина «нужно уточнить» — один раз: «лифт, кондиционер: нужно
            # уточнить — нет данных»; у одной характеристики — общая подпись.
            head, _, reason = claim.text.partition(UNKNOWN_SEP)
            for c in claims:
                c_head, _, c_reason = c.text.partition(UNKNOWN_SEP)
                if c.kind == "unknown" and reason and c_reason == reason:
                    c.text = f"{_merge_heads(c_head, head)}{UNKNOWN_SEP}{reason}"
                    same = c
                    break
        if same is None:
            claims.append(claim)
        else:  # одно и то же утверждение дважды не говорим — объединяем факты
            same.fact_ids += [f for f in claim.fact_ids if f not in same.fact_ids]

    for item in result.get("hard", []):
        add(_condition_claim(next_id(), with_field(item), catalog, fact))
    best_id = result.get("best_category")
    best = next((c for c in result.get("categories", []) if c["room_type_id"] == best_id), None)
    # Условие окна в категории подписываем так же, как пункт выше: там уже есть НЕ из графа и
    # слова гостя, а у условий категории — только ID (ревью №21).
    labels: dict[str, str] = {}
    todo = list(result.get("hard", []))
    while todo:
        item = todo.pop()
        todo += item.get("items") or []
        if item.get("atom_id"):
            labels[item["atom_id"]] = _label(with_field(item), catalog)

    def window_label(c: dict[str, Any]) -> str:
        return labels.get(c.get("atom_id") or "") or _label(with_field(c), catalog)

    if best and (floors := _floors_claim(next_id(), best, catalog, window_label)):
        add(floors)
    soft = sorted(result.get("soft", []), key=lambda s: -float(s.get("weight", 0)))
    for item in [s for s in soft if str(s.get("verdict")) == "yes"][:max_soft]:
        add(_condition_claim(next_id(), with_field(item), catalog, fact, soft=True))
    return claims


# --- Сборка текста: модель (порядок и связки) или шаблон ---


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExplainCompact(_Strict):
    """Рабочая схема для модели: только порядок ID и связки из списка, без своего текста."""

    order: list[str]
    links: list[Literal["", "и", "но", "а", "при этом", "зато", "однако", "причём"]]


SYSTEM = """\
Ты упорядочиваешь готовые утверждения о том, почему отель подходит гостю или нет. Утверждения —
данные, их текст ты не меняешь и своих слов не добавляешь. Верни:
- `order` — ID утверждений в том порядке, в котором их естественно сказать гостю: главное —
  первым; все утверждения о том, что не подходит или что нужно уточнить, — обязательно;
- `links` — по связке перед каждым утверждением из `order` (для первого — ""). Связки только
  из списка: "", "и", "но", "а", "при этом", "зато", "однако", "причём". "" — новая фраза.
"""


def assemble(claims: list[Claim], order: list[str], links: list[str]) -> str:
    by_id = {c.claim_id: c for c in claims}
    parts: list[str] = []
    for i, cid in enumerate(order):
        text = by_id[cid].text
        link = links[i] if i < len(links) else ""
        if i == 0:
            parts.append(text[:1].upper() + text[1:])
        elif link:
            parts.append(f", {link} {text}")
        else:
            parts.append(f". {text[:1].upper() + text[1:]}")
    return "".join(parts) + ("." if parts else "")


def problems(claims: list[Claim], order: list[str], links: list[str], text: str) -> list[str]:
    """Проверка: ID существуют и не повторяются, обязательные утверждения на месте, связки из
    списка, каждое число в тексте есть в утверждениях."""
    out = []
    ids = {c.claim_id for c in claims}
    if len(set(order)) != len(order) or not set(order) <= ids:
        out.append("порядок ссылается на неизвестные или повторяющиеся утверждения")
    missing = [c.claim_id for c in claims if c.kind in MUST and c.claim_id not in order]
    if missing:
        out.append(f"пропущены обязательные утверждения: {', '.join(missing)}")
    if any(link not in LINKS for link in links):
        out.append("связка не из списка")
    allowed = {n for c in claims for n in c.numbers}
    stray = [n for n in _NUM.findall(text) if n not in allowed]
    if stray:
        out.append(f"числа не из фактов: {', '.join(stray)}")
    return out


def template(claims: list[Claim]) -> Explanation:
    ordered = sorted(claims, key=lambda c: (TEMPLATE_ORDER[c.kind], c.claim_id))
    links, prev = [], None
    for c in ordered:
        # «но» между тем, что мешает, и тем, что подходит; иначе — новая фраза.
        switch = prev is not None and (prev.kind in MUST) != (c.kind in MUST)
        links.append("но" if switch else "")
        prev = c
    order = [c.claim_id for c in ordered]
    return Explanation(assemble(claims, order, links), claims, "template")


def explain(
    result: dict[str, Any],
    fact: Callable[[str], Fact | None],
    *,
    ast: QueryAST | None = None,
    use_llm: bool = False,
    gateway: Gateway | None = None,
) -> Explanation:
    """Объяснение одного результата поиска. `use_llm` — порядок и связки от модели."""
    claims = build_claims(result, fact, ast=ast)
    if not claims:
        return Explanation("Условий в запросе нет — показываем отель без оценки.", [], "template")
    if not use_llm:
        return template(claims)
    gw = gateway or get_gateway()
    if not gw.available:
        out = template(claims)
        out.notes.append("Объяснение по шаблону: ключ LLM не задан.")
        return out
    listing = "\n".join(f"{c.claim_id} [{c.kind}]: {c.text}" for c in claims)
    status = result.get("status_label") or result.get("status") or ""
    try:
        done = gw.complete(
            task="explain_result",
            system=SYSTEM,
            user=f"Статус отеля для гостя: {status}\nУтверждения (данные):\n{listing}",
            schema=ExplainCompact.model_json_schema(),
            model=ExplainCompact,
            max_tokens=1024,
        )
    except LLMError:
        out = template(claims)
        out.notes.append("Объяснение по шаблону: модель недоступна.")
        return out
    order, links = done.value.order, list(done.value.links)
    if not set(order) <= {c.claim_id for c in claims}:
        out = template(claims)
        out.notes.append("Объяснение по шаблону: порядок от модели ссылается на чужие ID.")
        return out
    text = assemble(claims, order, links)
    errs = problems(claims, order, links, text)
    if errs:
        out = template(claims)
        out.notes.append("Объяснение по шаблону: " + "; ".join(errs))
        return out
    return Explanation(text, claims, "llm")
