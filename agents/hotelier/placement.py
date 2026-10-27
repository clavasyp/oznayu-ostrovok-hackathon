"""Ответ отельера «стороны и этажи окон категории» → размещения D17 для `core/hotelier.py`.

Размещение — конкретная пара «здание × этаж × окна», а не два массива: иначе появляются ложные
сочетания «восток-5» (core §2.1). Разбирают правила по тексту отельера: этажи, сторона и корпус
связываются по месту в тексте. Модель (если есть ключ) только предлагает варианты: вариант
принимается, когда правила находят в его цитате те же этажи, стороны и корпус (D8). Полнота
списка и гарантия — только по словам отельера, правилами; кусок, который не разобран, — повод
спросить, и полным список тогда не бывает.

Сторона света → стены корпуса (D41-3): все отрезки контура на эту сторону от 3 м. Номер «окнами
на юг» — на одной из этих стен, поэтому на каждую стену (и этаж) — своё размещение с одним окном:
поиск говорит «да», только если «да» на всех. Одно размещение с окнами на несколько сторон —
только когда отельер прямо сказал, что окна на все эти стороны в каждом номере (угловой номер).
Уточнять «на какой из стен» не нужно: неоднозначность не блокирует ответ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agents.llm import Gateway, LLMError, get_gateway
from agents.search.text import find_span, normalize
from core.placement_catalog import COURTYARD, MIN_SEGMENT_M, name_key, name_words, side_segments

FEATURE_ID = "category_placement"
Side = Literal["N", "NE", "E", "SE", "S", "SW", "W", "NW", "courtyard"]
MAX_FLOOR = 100
# Угловой номер при нескольких стенах на сторону — все сочетания стен; больше — «одна из стен».
MAX_CORNER_COMBOS = 16

SIDE_WORDS = {
    "N": "север", "NE": "северо-восток", "E": "восток", "SE": "юго-восток", "S": "юг",
    "SW": "юго-запад", "W": "запад", "NW": "северо-запад", "courtyard": "двор",
}  # fmt: skip

# --- Правила: что где сказано в тексте (позиции — как в исходном тексте) ---

_SIDES = [
    (r"северо[\s-]*(?:восто(?:к|чн)\w*)", "NE"),
    (r"северо[\s-]*запад\w*", "NW"),
    (r"юго[\s-]*(?:восто(?:к|чн)\w*)", "SE"),
    (r"юго[\s-]*запад\w*", "SW"),
    (r"\bсевер\w*", "N"),
    (r"\bвосто(?:к|чн)\w*", "E"),
    (r"\bюг(?:а|у|ом|е)?\b|\bюжн\w*", "S"),
    (r"\bзапад\w*", "W"),
    (r"\b(?:во|на)\s+(?:внутренн\w*\s+)?двор\w*|\bвнутренн\w*\s+двор\w*|\bдворов\w*", COURTYARD),
]
# «Не на север», «кроме севера», «на север — нет»; «не только на юг» — не отрицание.
_SIDE_NEG_BEFORE = re.compile(r"(?:\bне(?!\s+только)|\bкроме|\bни)\s+(?:[\w-]+\s+){0,2}$")
_SIDE_NEG_AFTER = re.compile(r"^\w*\s+(?:нет|не\s+выход\w*)\b")
# Между сторонами одной группы: «на юг и на запад», «юг, запад», «юг/запад».
_SIDE_JOIN = re.compile(r"^[\s,/]*(?:(?:и|или|либо|а\s+также)\s*)?(?:на\s+)?$")
# Вид, а не сторона света.
_VIEWS = re.compile(
    r"\bна\s+(?:мор|гор|парк|улиц|город|рек|набережн|бассейн|сад|пляж|лес|территори|проспект|"
    r"площад)\w*"
)
_CORNER = re.compile(
    r"\bуглов\w*|\bв\s+каждом\s+номере|\bв\s+одном\s+номере|\bна\s+(?:обе|две|три)\s+сторон\w*"
    r"|\bдвусторонн\w*"
)
_SOME_CORNER = re.compile(
    r"\b(?:часть|некотор\w*|есть|бывают|иногда|отдельн\w*|по\s+запросу)\b[^.;]*\bуглов"
)

_ORD = r"(?:-?(?:го|ого|й|ий|ый|ой|м|ом|ем|х|ым))?"
_NUM = rf"(\d{{1,3}}){_ORD}"
_FLOORS = r"(?:этаж\w*|эт\b)"
_FLOOR_PATTERNS = [
    ("range", rf"\b{_FLOORS}\s+(?:со?\s+)?{_NUM}\s*(?:[-–—]|по|до)\s*{_NUM}(?![\d:])"),
    ("range", rf"\bсо?\s+{_NUM}\s+(?:по|до)\s+{_NUM}(?![\d:])(?:\s+{_FLOORS})?"),
    ("range", rf"\b{_NUM}\s*[-–—]\s*{_NUM}\s*{_FLOORS}"),
    ("pair", rf"\b{_NUM}\s+и\s+{_NUM}\s+{_FLOORS}"),
    # «3 этаж», «на 3-м этаже»; «15 этажей», «в 3 этажа» — этажность, а не этаж номера.
    ("single", rf"\b{_NUM}\s+(?:этаж(?:е|у)?\b|эт\b)"),
    ("all", rf"\b(?:все|всех|любо\w*)\s+{_FLOORS}"),
]
# Этажи без чисел: «остальные этажи», «выше» — какие именно, не сказано.
_VAGUE_FLOORS = re.compile(
    r"\b(?:остальн|друг|прочи|верхн|нижн)\w*\s+(?:этаж|номер)\w*|\bвыше\b|\bниже\b"
)

_BUILDING = r"(?:корпус\w*|корп\b|здани\w*|блок\w*|башн\w*|строени\w*|флигел\w*|вилл\w*|коттедж\w*)"
_NOT_NAMES = {
    "этом", "том", "каждом", "одном", "другом", "любом", "нашем", "вашем", "своем", "этот",
    "тот", "каждый", "другой", "любой", "всех", "всем", "обоих", "этого", "того", "этой",
}  # fmt: skip
_BUILDING_PATTERNS = [
    ("name", rf"\b{_BUILDING}\s*(?:№\s*)?\d+[а-яa-z]?\b"),
    ("letter", rf"\b{_BUILDING}\s*(?:№\s*)?[а-яa-z]\b"),  # «корпус А»: буква — заглавная
    ("name", rf"\b\d+{_ORD}\s+{_BUILDING}"),
    ("name", rf"\b(?:перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)\w*\s+{_BUILDING}"),
    ("adjective", rf"\b[а-я]{{3,}}(?:ый|ий|ой|ое|ая|ее|ом|ем|ым|им|ого|его)\s+{_BUILDING}"),
    ("parens", rf"\(([^()]{{0,60}}{_BUILDING}[^()]{{0,60}})\)"),
]

UNPAIRED = (
    "не поняли, какие этажи к какой стороне или корпусу: напишите каждый вариант отдельно — "
    "«корпус, этажи с … по …, сторона»"
)

_COMPLETE = re.compile(
    r"\bдругих\s+(?:вариантов|номеров|сторон|этажей|корпусов)(?:[\s,]+[\w-]+){0,4}[\s,]+нет\b"
    r"|\bдругих\s+нет\b|\bбольше\s+(?:вариантов\s+)?нет\b|\bбольше\s+нигде\b|\bтолько\s+так\b"
    r"|\bвсе\s+номера\b|\bво\s+всех\s+номерах\b"
)
_GUARANTEE = re.compile(r"\bгарант\w*|\bзакрепл\w*|\bзакрепим\b")
_NEG_BEFORE = re.compile(r"(?:\bне|\bнельзя|\bбез|\bни)\s+(?:[\w-]+[\s,]+){0,2}$|\bнет\s+$")
_NEG_AFTER = re.compile(r"^\w*[\s,]+(?:[\w-]+[\s,]+){0,2}(?:не|нельзя|нет)\b")
_ON_REQUEST = re.compile(r"\bпо\s+(?:запросу|согласованию)\b")
_MAYBE = re.compile(
    r"\bпо\s+возможности\b|\bпри\s+наличии\b|\bзависит\s+от\b|\bкак\s+получится\b"
    r"|\bпостараемся\b|\bпри\s+заселении\b|\bне\s+всегда\b"
)


@dataclass
class _Mention:
    start: int
    end: int
    kind: str  # floors | sides | building
    value: Any  # этажи: (с, по) или None — «все этажи»; стороны: [код]; корпус: как в тексте


@dataclass
class _Variant:
    building: str  # как в тексте; "" — корпус не назван
    floors: tuple[int, int] | None  # None — «все этажи»
    sides: list[str]
    corner: bool
    span: tuple[int, int]


@dataclass
class _Chunk:
    start: int
    end: int
    mentions: list[_Mention] = field(default_factory=list)

    def of(self, kind: str) -> list[_Mention]:
        return [m for m in self.mentions if m.kind == kind]


@dataclass
class _Rules:
    variants: list[_Variant]
    issues: list[tuple[tuple[int, int], str, list[tuple[int, int]]]]  # кусок, что не так, где


def _masked(answer: str) -> str:
    """Текст для правил: без регистра, названия в «ёлочках» (категория) скрыты, длина та же."""
    norm = normalize(answer)
    norm = re.sub(r"«[^»]*»", lambda m: " " * len(m.group()), norm)
    return re.sub(r"\b(корп|эт)\.", r"\1 ", norm)


def _chunks(norm: str, start: int, end: int) -> list[_Chunk]:
    out, pos = [], start
    for m in re.finditer(r"[;!?\n]|(?<!\d)\.|\.(?!\d)", norm[start:end]):
        cut = start + m.start()
        if cut > pos:
            out.append(_Chunk(pos, cut))
        pos = cut + 1
    if end > pos:
        out.append(_Chunk(pos, end))
    return out


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _free(taken: list[tuple[int, int]], span: tuple[int, int]) -> bool:
    return not any(_overlaps(span, t) for t in taken)


def _known_mentions(norm: str, chunk: _Chunk, known: list[str]) -> list[tuple[int, int]]:
    """Где в куске названия корпусов, которые отель подтвердил или отметил в списке (по словам:
    «в главном корпусе» — это «главный корпус»)."""
    words = name_words(norm[chunk.start : chunk.end])
    stems = [w[2] for w in words]
    taken: list[tuple[int, int]] = []
    for key in sorted(known, key=lambda k: -len(k.split())):
        seqs = [key.split()]
        if len(seqs[0]) == 2 and seqs[0][1].isdigit():
            seqs.append(seqs[0][::-1])  # «корпус 2» и «2-й корпус»
        for seq in seqs:
            for i in range(len(stems) - len(seq) + 1):
                if stems[i : i + len(seq)] == seq:
                    span = (chunk.start + words[i][0], chunk.start + words[i + len(seq) - 1][1])
                    if _free(taken, span):
                        taken.append(span)
    return taken


def _names(answer: str, start: int, end: int, key: str) -> bool:
    """Назван ли корпус `key` в этом месте ответа."""
    norm = _masked(answer)
    return any(_known_mentions(norm, c, [key]) for c in _chunks(norm, start, end))


def _mentions(answer: str, norm: str, chunk: _Chunk, known: list[str]) -> None:
    text = norm[chunk.start : chunk.end]
    taken: list[tuple[int, int]] = []

    def add(start: int, end: int, kind: str, value: Any) -> bool:
        span = (chunk.start + start, chunk.start + end)
        if not _free(taken, span):
            return False
        taken.append(span)
        chunk.mentions.append(_Mention(*span, kind, value))
        return True

    for kind, pattern in _FLOOR_PATTERNS:
        for m in re.finditer(pattern, text):
            nums = [int(g) for g in m.groups() if g]
            if kind == "all":
                add(m.start(), m.end(), "floors", None)
            elif kind == "pair":
                if _free(taken, (chunk.start + m.start(), chunk.start + m.end())):
                    add(m.start(1), m.end(1), "floors", (nums[0], nums[0]))
                    add(m.start(2), m.end(), "floors", (nums[1], nums[1]))
            elif kind == "single":
                add(m.start(), m.end(), "floors", (nums[0], nums[0]))
            else:
                add(m.start(), m.end(), "floors", (nums[0], nums[1]))
    for pattern, code in _SIDES:
        for m in re.finditer(pattern, text):
            before = re.split(r"[,—–:]", text[: m.start()])[-1]
            if _SIDE_NEG_BEFORE.search(before) or _SIDE_NEG_AFTER.search(text[m.end() :]):
                taken.append((chunk.start + m.start(), chunk.start + m.end()))
                continue
            add(m.start(), m.end(), "sides", [code])
    for s, e in _known_mentions(norm, chunk, known):
        add(s - chunk.start, e - chunk.start, "building", answer[s:e])
    for kind, pattern in _BUILDING_PATTERNS:
        for m in re.finditer(pattern, text):
            s, e = (m.start(1), m.end(1)) if kind == "parens" else (m.start(), m.end())
            raw = answer[chunk.start + s : chunk.start + e].strip()
            if kind == "letter" and not raw[-1].isupper():
                continue  # «в корпусе и …»: строчная буква — не название
            first = normalize(raw.split()[0])
            if kind == "adjective" and (first in _NOT_NAMES or "этажн" in first):
                continue  # «в этом корпусе», «в 15-этажном здании» — не название
            add(s, e, "building", raw)
    chunk.mentions.sort(key=lambda m: m.start)
    # Соседние стороны — одна группа: «на юг и на запад».
    merged: list[_Mention] = []
    for m in chunk.mentions:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.kind == m.kind == "sides"
            and _SIDE_JOIN.match(norm[prev.end : m.start])
        ):
            prev.value = prev.value + [c for c in m.value if c not in prev.value]
            prev.end = m.end
        else:
            merged.append(m)
    chunk.mentions = merged


def _merge(chunks: list[_Chunk]) -> list[_Chunk]:
    """Склеить соседние куски, которые дополняют друг друга: «Корпус 2. Окна на юг, 3–8 этаж»,
    «Окна на юг. С 3 по 8 этаж»."""
    out: list[_Chunk] = []
    for c in chunks:
        prev = out[-1] if out else None
        if prev is not None:
            pf, ps, pb = (bool(prev.of(k)) for k in ("floors", "sides", "building"))
            cf, cs, cb = (bool(c.of(k)) for k in ("floors", "sides", "building"))
            only_building = pb and not pf and not ps and (cf or cs) and not cb
            halves = (pf and not ps and cs and not cf) or (ps and not pf and cf and not cs)
            if only_building or (halves and not (pb and cb)):
                prev.end, prev.mentions = c.end, prev.mentions + c.mentions
                continue
        out.append(c)
    return out


def _pair(chunk: _Chunk, norm: str) -> tuple[list[_Variant], str | None]:
    """Варианты куска: этажи, стороны и корпус — по месту в тексте. Не связать — причина."""
    floors, sides, buildings = chunk.of("floors"), chunk.of("sides"), chunk.of("building")
    if not floors and not sides:
        return [], None
    if not sides:
        view = _VIEWS.search(norm[chunk.start : chunk.end])
        if view:
            return [], (
                f"«{view.group()}» — это вид, а не сторона света: на какую сторону света выходят "
                "окна (север, северо-восток, восток, юго-восток, юг, юго-запад, запад, "
                "северо-запад) или во двор?"
            )
        return [], "нет стороны света: на какую сторону выходят окна (юг, северо-запад…)?"
    if not floors:
        return [], "нет этажей: с какого по какой этаж?"
    vague = _VAGUE_FLOORS.search(norm[chunk.start : chunk.end])
    if vague:
        return [], f"«{vague.group()}» — какие это этажи: с какого по какой?"
    # Списки длины n связываются по порядку, длины 1 — общие для всех вариантов; i-й вариант
    # целиком стоит в тексте до (i+1)-го. Иначе связать нельзя — спрашиваем.
    n = max(len(floors), len(sides), len(buildings))
    paired = [lst for lst in (floors, sides, buildings) if len(lst) == n > 1]
    if (
        len(floors) not in (1, n)
        or len(sides) not in (1, n)
        or len(buildings) not in (0, 1, n)
        or any(
            max(lst[i].start for lst in paired) > min(lst[i + 1].start for lst in paired)
            for i in range(n - 1)
        )
    ):
        return [], UNPAIRED
    text = norm[chunk.start : chunk.end]
    corner = bool(_CORNER.search(text)) and not _SOME_CORNER.search(text)
    out = []
    for i in range(n):
        f = floors[i if len(floors) == n else 0]
        sd = sides[i if len(sides) == n else 0]
        b = buildings[i if len(buildings) == n else 0] if buildings else None
        if f.value is not None and not 1 <= f.value[0] <= f.value[1] <= MAX_FLOOR:
            return [], f"этажи {f.value[0]}–{f.value[1]} вне 1–{MAX_FLOOR}: проверьте этажи"
        out.append(
            _Variant(
                building=b.value if b else "",
                floors=f.value,
                sides=list(sd.value),
                corner=corner and len(sd.value) > 1,
                span=(chunk.start, chunk.end),
            )
        )
    return out, None


def _rules(answer: str, known: list[str], start: int = 0, end: int | None = None) -> _Rules:
    norm = _masked(answer)
    end = len(answer) if end is None else end
    chunks = _chunks(norm, start, end)
    for c in chunks:
        _mentions(answer, norm, c, known)
    variants, issues = [], []
    for c in _merge(chunks):
        found, why = _pair(c, norm)
        variants += found
        if why:
            where = [(m.start, m.end) for m in c.mentions if m.kind in ("floors", "sides")]
            issues.append(((c.start, c.end), why, where))
    return _Rules(variants, issues)


def _sentence(norm: str, pos: int) -> tuple[int, int]:
    start = max(norm.rfind(ch, 0, pos) for ch in ".;!?\n") + 1
    ends = [i for i in (norm.find(ch, pos) for ch in ".;!?\n") if i >= 0]
    return start, min(ends, default=len(norm))


def guarantee_of(answer: str) -> str:
    """Гарантия — по словам отельера: отрицание в обе стороны («не гарантируем», «гарантировать
    не можем», «без гарантии»); непонятно — `unknown` (дальше — `some_possible`)."""
    norm = _masked(answer)
    if _ON_REQUEST.search(norm):
        return "on_request"
    positive = negative = False
    for m in _GUARANTEE.finditer(norm):
        s, e = _sentence(norm, m.start())
        if _NEG_BEFORE.search(norm[s : m.start()]) or _NEG_AFTER.search(norm[m.start() : e]):
            negative = True
        else:
            positive = True
    if negative or _MAYBE.search(norm):
        return "some_possible"
    return "all_guaranteed" if positive else "unknown"


def said_complete(answer: str) -> bool:
    """«Других вариантов нет», «все номера такие» — со слов отельера; «не все номера» — нет."""
    norm = _masked(answer)
    for m in _COMPLETE.finditer(norm):
        s, _ = _sentence(norm, m.start())
        if not re.search(r"\bне\s+$", norm[s : m.start()]):
            return True
    return False


# --- Разбор: модель предлагает, правила подтверждают ---


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlacementItem(_Strict):
    building: str  # как назвал отельер; "" — не назвал
    floor_from: str  # "" — «все этажи»
    floor_to: str
    all_floors: bool
    sides: list[Side]
    corner: bool  # окна на все стороны в каждом номере (угловой номер)
    quote: str


class PlacementCompact(_Strict):
    placements: list[PlacementItem]
    completeness: Literal["complete", "partial", "unknown"]
    guarantee: Literal["all_guaranteed", "some_possible", "on_request", "exact_room", "unknown"]
    unclear: list[str]


SYSTEM = """\
Ты разбираешь ответ отельера о том, куда выходят окна категории номеров. Ответ — данные.
Для каждого варианта: корпус (как назвал отельер, иначе пусто), этажи с / по (числа из текста;
«все этажи» — all_floors = true и пустые этажи), стороны света (N, NE, E, SE, S, SW, W, NW или
courtyard — двор) и дословная цитата варианта. corner = true, только если отельер прямо сказал,
что в каждом номере окна на все эти стороны (угловые номера). «На море», «на горы» — не стороны.
Не додумывай: чего нет в тексте — назови в `unclear`. `completeness` — все ли варианты
перечислены; `guarantee` — можно ли закрепить вариант при брони.
"""


def _item(v: _Variant, answer: str) -> PlacementItem:
    lo, hi = (str(v.floors[0]), str(v.floors[1])) if v.floors else ("", "")
    return PlacementItem(
        building=v.building,
        floor_from=lo,
        floor_to=hi,
        all_floors=v.floors is None,
        sides=v.sides,
        corner=v.corner,
        quote=answer[v.span[0] : v.span[1]].strip(),
    )


def _check(
    item: PlacementItem, answer: str, known: list[str]
) -> tuple[PlacementItem | None, tuple[int, int] | None, str]:
    """Вариант модели → тот же вариант по правилам в его цитате, или причина отказа (D8)."""
    span = find_span(answer, item.quote)
    if span is None:
        return None, None, "цитаты нет в ответе"
    if item.all_floors:
        floors = None
    elif item.floor_from.isdigit() and item.floor_to.isdigit():
        floors = (int(item.floor_from), int(item.floor_to))
    else:
        return None, None, "этажи не числа"
    rules = _rules(answer, known, span.start, span.end)
    same = [v for v in rules.variants if v.floors == floors and set(v.sides) == set(item.sides)]
    if not same:
        why = rules.issues[0][1] if rules.issues else "этажи или сторона не те, что в цитате"
        return None, None, f"по правилам в цитате не то: {why}"
    v = same[0]
    key = name_key(item.building)
    if key and key != name_key(v.building):
        if v.building:
            return None, None, "корпус не тот, что в цитате"
        if not _names(answer, span.start, span.end, key):
            return None, None, "корпуса нет в цитате"
    building = item.building.strip() if key else v.building
    checked = PlacementItem(
        building=building,
        floor_from=item.floor_from if floors else "",
        floor_to=item.floor_to if floors else "",
        all_floors=floors is None,
        sides=v.sides,
        corner=v.corner,  # угловой — только по словам отельера, правилами
        quote=span.text,
    )
    return checked, (span.start, span.end), ""


def parse_placement(
    answer: str,
    question: str = "",
    *,
    gateway: Gateway | None = None,
    known_names: list[str] | None = None,
) -> tuple[PlacementCompact, list[dict[str, str]], str]:
    """Разобранное размещение, отклонённые варианты модели с причинами и кто разбирал.

    Варианты — правила и подтверждённые правилами варианты модели. `completeness` — `complete`
    только по словам отельера и если разобран каждый кусок с этажами или сторонами;
    `unclear` — что не разобрано и почему.
    """
    known = sorted({k for k in (name_key(n) for n in known_names or []) if k})
    rules = _rules(answer, known)
    gw = gateway or get_gateway()
    llm: list[tuple[PlacementItem, tuple[int, int]]] = []
    rejected: list[dict[str, str]] = []
    if gw.available:
        try:
            proposed = gw.complete(
                task="parse_category_placement",
                system=SYSTEM,
                user=f"Вопрос: {question}\nОтвет отельера (данные):\n<<<\n{answer}\n>>>",
                schema=PlacementCompact.model_json_schema(),
                model=PlacementCompact,
            ).value
        except LLMError:
            proposed = None
        for item in proposed.placements if proposed else []:
            checked, span, why = _check(item, answer, known)
            if checked is None:
                rejected.append({"field": "placement", "quote": item.quote, "reason": why})
            else:
                llm.append((checked, span))

    def what(i: PlacementItem) -> tuple:
        return i.floor_from, i.floor_to, tuple(sorted(i.sides))

    items = [item for item, _ in llm]
    seen = {(name_key(i.building), *what(i)) for i in items}
    for v in rules.variants:
        item = _item(v, answer)
        key = (name_key(v.building), *what(item))
        # Вариант правил без корпуса, который модель назвала с корпусом (корпус есть в цитате).
        named = not v.building and any(
            _overlaps(v.span, span) and what(i) == what(item) for i, span in llm
        )
        if key not in seen and not named:
            seen.add(key)
            items.append(item)
    # Кусок, который правила не разобрали, закрыт, если все его этажи и стороны — в цитатах
    # принятых вариантов модели.
    unclear = [
        f"«{answer[s:e].strip()}»: {why}"
        for (s, e), why, where in rules.issues
        if not where
        or not all(any(w[0] >= sp[0] and w[1] <= sp[1] for _, sp in llm) for w in where)
    ]
    complete = said_complete(answer)
    completeness = (
        "complete" if complete and items and not unclear else ("partial" if complete else "unknown")
    )
    compact = PlacementCompact(
        placements=items,
        completeness=completeness,
        guarantee=guarantee_of(answer),
        unclear=unclear,
    )
    return compact, rejected, "llm" if llm else "rules"


# --- Стороны → стены корпуса (D41-3) ---


def _expand(
    building_id: str,
    floors: list[int],
    sides: list[str],
    corner: bool,
    segments: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], bool]:
    """Размещения варианта: на каждую стену стороны и этаж — своё (номер на одной из стен); у
    углового номера — сочетания стен разных сторон. Второе значение — угловой разложен на
    «одну из стен» (слишком много сочетаний)."""
    walls = [segments[s] for s in sides]
    combos = list(product(*walls)) if corner and len(sides) > 1 else []
    fallback = bool(combos) and len(combos) > MAX_CORNER_COMBOS
    rooms = (
        [sorted(set(c)) for c in combos]
        if combos and not fallback
        else [[w] for w in dict.fromkeys(w for ws in walls for w in ws)]
    )
    items = [
        {
            "building_id": building_id,
            "floor": floor,
            "windows": room,
            "room_variant": "+".join(room),
        }
        for floor in floors
        for room in rooms
    ]
    return items, fallback


def to_placements(
    compact: PlacementCompact,
    buildings: dict[str, str],
    segments: dict[tuple[str, str], list[str] | str],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Разобранный ответ → `placements` для `POST /api/hotelier/{id}/answers` (D17).

    `buildings` — как отельер называет корпус → `building_id` (сравнение по `name_key`;
    единственный корпус подходит и без названия); `segments` — (`building_id`, сторона) → стены
    этой стороны от 3 м (`core.placement_catalog.side_segments`). Чего нет в справочниках — не
    угадываем: размещение не отправляется, причины — во втором значении.
    """
    by_key = {name_key(k): v for k, v in buildings.items() if name_key(k)}
    items, missing = [], []
    for p in compact.placements:
        building_id = by_key.get(name_key(p.building)) or (
            next(iter(buildings.values())) if len(buildings) == 1 and not p.building else None
        )
        if building_id is None:
            missing.append(f"нет здания «{p.building}» среди корпусов отеля")
            continue
        walls = {}
        for side in p.sides:
            found = segments.get((building_id, side)) or []
            walls[side] = [found] if isinstance(found, str) else list(found)
        if not all(walls.values()):
            missing.append(f"у здания {building_id} нет стены на сторону {', '.join(p.sides)}")
            continue
        if p.all_floors:
            missing.append(f"«все этажи» у здания {building_id}: нужны номера этажей")
            continue
        floors = list(range(int(p.floor_from), int(p.floor_to) + 1))
        items += _expand(building_id, floors, p.sides, p.corner, walls)[0]
    if missing or not items:
        return None, missing or ["нет вариантов размещения"]
    guarantee = compact.guarantee if compact.guarantee != "unknown" else "some_possible"
    return {"completeness": compact.completeness, "guarantee": guarantee, "items": items}, []


# --- Ответ отельера на вопрос о категории → размещения D17 по геометрии отеля ---


@dataclass
class PlacementAnswer:
    """Что понял агент: размещения (или None), уточняющий вопрос и подтверждённые названия."""

    placements: dict[str, Any] | None
    followup: str | None
    aliases: list[dict[str, Any]]  # отмеченные в списке корпуса: название → building_id
    report: dict[str, Any]
    stated: list[dict[str, Any]] = field(default_factory=list)  # для паспорта набора


def choice_label(choice: dict[str, Any], number: int | None = None) -> str:
    """«1) улица 65 лет Победы, 50 — 9 эт. (на карте: «Radisson Collection»)»."""
    head = f"{number}) " if number is not None else ""
    text = choice.get("mapped_address") or choice["building_id"]
    if choice.get("mapped_levels"):
        text += f" — {choice['mapped_levels']} эт."
    if choice.get("mapped_name"):
        text += f" (на карте: «{choice['mapped_name']}»)"
    if choice.get("hotel_aliases"):
        text += f"; у вас: {', '.join(choice['hotel_aliases'])}"
    return f"{head}{text} [{choice['building_id']}]"


def choices_text(choices: list[dict[str, Any]]) -> str:
    return "\n".join(choice_label(c, i) for i, c in enumerate(choices, 1))


def _quote_of(answer: str, key: str) -> str | None:
    """Кусок ответа, где отель назвал корпус этим названием; не назвал — None."""
    norm = _masked(answer)
    for c in _chunks(norm, 0, len(answer)):
        if _known_mentions(norm, c, [key]):
            return answer[c.start : c.end].strip()
    return None


def answer_placement(
    text: str,
    geometry: dict[str, Any],
    *,
    building_aliases: dict[str, str] | None = None,
    question: str = "",
    gateway: Gateway | None = None,
) -> PlacementAnswer:
    """Свободный ответ о категории → размещения или уточняющий вопрос (D17, D41-3).

    Корпус — по подтверждённому названию (`placement_building_aliases`), по отмеченному в
    списке (`building_aliases`: «как вы его называете» → `building_id`), единственный корпус
    отеля или единственный отмеченный, если в тексте корпус не назван. Сторона — все стены
    корпуса на эту сторону от 3 м. Чего не понять по тексту и карте — уточняющий вопрос; то,
    что понято, сохраняется, но список тогда не полный.
    """
    choices = geometry.get("placement_building_choices") or []
    by_id = {c["building_id"]: c for c in choices}
    rejected: list[dict[str, str]] = []
    marked: dict[str, str] = {}
    for name, bid in (building_aliases or {}).items():
        if bid not in by_id:
            rejected.append(
                {"field": "building", "reason": f"здания {bid} нет среди корпусов отеля"}
            )
        elif not name_key(name):
            rejected.append({"field": "building", "reason": "пустое название корпуса"})
        else:
            marked[name.strip()] = bid
    known: dict[str, str | None] = {}
    for name, bid in (geometry.get("placement_building_aliases") or {}).items():
        key = name_key(name)
        if key:
            known[key] = bid if known.get(key, bid) == bid else None  # одно название — два корпуса
    known |= {name_key(name): bid for name, bid in marked.items()}  # отмеченное сейчас — главнее
    compact, llm_rejected, parsed_by = parse_placement(
        text, question, gateway=gateway, known_names=list(known)
    )
    rejected += llm_rejected
    confirmed = []
    for name, bid in marked.items():
        alias = {"name": name, "building_id": bid}
        quote = _quote_of(text, name_key(name))
        confirmed.append(alias | ({"quote": quote} if quote else {}))
    report: dict[str, Any] = {
        "parsed_by": parsed_by,
        "rejected": rejected,
        "placements": [],
        "unclear": compact.unclear,
        "notes": [],
    }
    if not choices:
        report["notes"].append(
            "У отеля нет корпусов в справочнике — размещения не к чему привязать."
        )
        return PlacementAnswer(None, None, confirmed, report)
    questions = [f"Не поняли часть ответа — {u}" for u in compact.unclear]
    if not compact.placements and not questions:
        questions.append(
            "Не нашли в ответе ни этажей, ни стороны света: напишите «корпус, этажи с … по …, "
            "сторона» (север, северо-восток, восток, юго-восток, юг, юго-запад, запад, "
            "северо-запад или во двор)."
        )
    confirm: list[str] = []
    ids = [c["building_id"] for c in choices]
    only_marked = set(marked.values())
    single = (
        ids[0] if len(ids) == 1 else (next(iter(only_marked)) if len(only_marked) == 1 else None)
    )
    items: list[dict[str, Any]] = []
    stated: list[dict[str, Any]] = []
    unresolved = from_map = False
    for p in compact.placements:
        key = name_key(p.building)
        bid = known.get(key) if key else single
        entry = {"building": p.building, "building_id": bid, "sides": p.sides, "corner": p.corner,
                 "floors": None if p.all_floors else [int(p.floor_from), int(p.floor_to)],
                 "quote": p.quote}  # fmt: skip
        report["placements"].append(entry)
        if bid is None:
            unresolved = True
            named = f"«{p.building}»" if key else "корпус в ответе не назван"
            questions.append(
                f"Какой это корпус — {named}? Отметьте его в списке и напишите, как вы его "
                "называете:\n" + choices_text(choices)
            )
            continue
        label = choice_label(by_id[bid])
        walls = side_segments(geometry.get("facades") or [], bid)
        lost = [s for s in p.sides if not walls.get(s)]
        for side in lost:
            if side == COURTYARD:
                questions.append(
                    f"У корпуса {label} по карте не знаем, какие стены выходят во двор — назовите "
                    "сторону света (север, восток, юг, запад…)."
                )
            else:
                questions.append(
                    f"У корпуса {label} по карте нет стены на {SIDE_WORDS[side]} (от "
                    f"{MIN_SEGMENT_M:g} м) — уточните сторону."
                )
        if lost:
            unresolved = True
            continue
        levels = by_id[bid].get("mapped_levels")
        if p.all_floors:
            if not levels:
                unresolved = True
                questions.append(
                    f"«Все этажи» — это с какого по какой этаж в корпусе {label}? По карте "
                    "этажность корпуса неизвестна."
                )
                continue
            floors = list(range(1, levels + 1))
            from_map = True
            confirm.append(
                f"«Все этажи» у корпуса {label}: по карте в нём {levels} эт. — подтвердите, что "
                f"окна категории с 1 по {levels} этаж."
            )
        else:
            floors = list(range(int(p.floor_from), int(p.floor_to) + 1))
            if levels and floors[-1] > levels:
                confirm.append(
                    f"У корпуса {label} по карте {levels} эт., а в ответе этажи до "
                    f"{floors[-1]} — подтвердите этажи."
                )
                report["notes"].append(
                    f"Этажи выше этажности по карте ({floors[-1]} > {levels}): "
                    "сохранено со слов отеля, нужно подтверждение."
                )
        placed, fallback = _expand(bid, floors, p.sides, p.corner, walls)
        if fallback:
            report["notes"].append(
                "Угловой номер: сочетаний стен слишком много — считаем, что номер у одной из стен."
            )
        entry["segments"] = {s: walls[s] for s in p.sides}
        stated.append({k: entry[k] for k in ("building_id", "floors", "sides", "corner", "quote")}
                      | {"building": p.building, "floors_from_map": p.all_floors})  # fmt: skip
        items += placed
    # Один номер — одна запись: повтор того же варианта на том же этаже не дублируется.
    unique = {(i["building_id"], i["floor"], i["room_variant"]): i for i in items}
    placements = None
    if unique:
        completeness = compact.completeness
        if completeness == "complete" and (unresolved or from_map):
            completeness = "partial"
        guarantee = compact.guarantee if compact.guarantee != "unknown" else "some_possible"
        placements = {
            "completeness": completeness,
            "guarantee": guarantee,
            "items": list(unique.values()),
        }
    if questions and placements:
        report["notes"].append(
            "Сохранено то, что понятно; список не полный до ответа на уточнение."
        )
        questions.append(
            "Ответьте целиком — все варианты «корпус, этажи, сторона»: новый ответ заменит "
            "сохранённый."
        )
    elif questions:
        report["notes"].append("Размещение не сохранено до уточнения: без догадок о корпусе.")
    followup = "\n\n".join(dict.fromkeys(questions + confirm)) or None
    return PlacementAnswer(placements, followup, confirmed, report, stated)
