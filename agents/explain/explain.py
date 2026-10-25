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
DEFAULT_FIELDS = ("distance_m", "count", "mean", "mean_C", "aboveground_levels")
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
    shown = _fmt_value(v, spec.unit if spec else None)
    # «есть только оценка» скажет причина — второй раз «оценка» не нужна.
    note = None if item.get("reason_code") == "estimate_only" else STATUS_NOTE.get(f.status)
    return ", ".join(x for x in (shown, note) if x)


def _label(item: dict[str, Any], catalog: Catalog) -> str:
    feature = catalog.get(item.get("feature_id") or "")
    label = feature.short if feature else (item.get("text_span") or "условие")
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
    return f"не {label}" if item.get("negated") else label


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
    evidence = "" if item.get("items") else _evidence(item, catalog, fact)
    if verdict == "unknown":
        reason = (item.get("reason") or "").rstrip(".")
        reason = reason[:1].lower() + reason[1:] if reason else ""
        tail = ", ".join(x for x in (evidence, reason) if x)
        text = f"{label}: нужно уточнить" + (f" — {tail}" if tail else "")
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


def _floors_claim(claim_id: str, category: dict[str, Any], catalog: Catalog) -> Claim | None:
    """«Только с 5-го этажа»: по размещениям лучшей категории — одни этажи подходят, другие нет."""
    ps = [p for p in category.get("placements") or [] if p.get("floor") is not None]
    yes = [p["floor"] for p in ps if str(p.get("verdict")) == "yes"]
    no = [p["floor"] for p in ps if str(p.get("verdict")) == "no"]
    if not yes or not no:
        return None
    window = [
        c
        for c in category.get("conditions", [])
        if str(c.get("feature_id", ""))[:1] in "VSN"
        or c.get("feature_id") in ("floor", "courtyard")
    ]
    what = ", ".join(dict.fromkeys(_label(c, catalog) for c in window)) or "условия окна"
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
    # Какое поле факта сравнивалось — из условия AST (в результате поиска его нет).
    fields = {c.atom_id: c.field for c in [*ast.hard, *ast.soft] if c.atom_id} if ast else {}

    def with_field(item: dict[str, Any]) -> dict[str, Any]:
        found = fields.get(item.get("atom_id") or "")
        return item | {"field": found} if found and not item.get("field") else item

    def next_id() -> str:
        return f"c{len(claims) + 1}"

    def add(claim: Claim) -> None:
        same = next((c for c in claims if c.text == claim.text), None)
        if same is None:
            claims.append(claim)
        else:  # одно и то же утверждение дважды не говорим — объединяем факты
            same.fact_ids += [f for f in claim.fact_ids if f not in same.fact_ids]

    for item in result.get("hard", []):
        add(_condition_claim(next_id(), with_field(item), catalog, fact))
    best_id = result.get("best_category")
    best = next((c for c in result.get("categories", []) if c["room_type_id"] == best_id), None)
    if best and (floors := _floors_claim(next_id(), best, catalog)):
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
