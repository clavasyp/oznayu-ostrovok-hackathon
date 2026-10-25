"""Офлайн-разбор правилами: известные шаблоны запросов → компактный AST (core §7.3, v4b §9).

Работает без ключа LLM и первым в цепочке: если правила покрыли весь текст, модель не нужна.
Что правила не поняли, остаётся в `unparsed_spans` — ничего не теряется молча.

Политика (пороги «недалеко», «повыше», «тихо») — здесь, в коде, с пояснениями: значения
не придумывает модель. Обязательность: явные слова гостя («обязательно», «только» /
«хотелось бы», «лучше»); без них конкретное требование к номеру — обязательное, а близость,
тишина и окружение — желательные (как в примере трека).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from dataclasses import field as dfield
from datetime import date

from agents.catalog import Catalog, load_places
from agents.search.compact import CompactAST, CompactAtom, CompactClarify, OrGroup, Param
from agents.search.negation import negate
from agents.search.text import marker_requiredness, normalize

# --- Пороги политики (не измерения): меняются здесь и видны в разборе ---
NEAR_BEACH_M = 800  # «недалеко от моря» — как в примере трека
NEAR_CENTER_M = 1500
NEAR_STOP_M = 400
NEAR_FOOD_M = 500
NEAR_PLAYGROUND_M = 500
NEAR_PHARMACY_M = 1000
NEAR_VET_M = 3000
STEP_FREE_MM = 20  # «без ступеней», «душ без бортика»: порог не выше 2 см
DESK_MIN_MM = 600  # «нормальный рабочий стол»: ширина не меньше 60 см
QUIET_ROAD_M = 200  # «тихий» снаружи: до оживлённой дороги не ближе 200 м
QUIET_BARS_MAX = 2  # и не больше двух баров в 300 м
BARS_LIKED_MIN = 3
QUIET_INDOOR_DBA = 30  # тишина в спальне ночью — ориентир ВОЗ, LAeq ≤ 30 дБ(A)
HIGH_FLOOR_MIN = 5
FEW_SUN_HOURS = 1
GREEN_SHARE_MIN = 30
CONSTRUCTION_M = 300
WARM_SEA_C = 22
MAX_ASCENT_M = 10


WORD_NUMBERS = {
    **dict.fromkeys(["один", "одна", "одного", "первый", "первого", "первом", "первая"], 1),
    **dict.fromkeys(["два", "две", "двух", "второй", "второго", "втором", "вторая"], 2),
    **dict.fromkeys(["три", "трех", "третий", "третьего", "третьем", "третья"], 3),
    **dict.fromkeys(["четыре", "четырех", "четвертый", "четвертого", "четвертом"], 4),
    **dict.fromkeys(["пять", "пяти", "пятый", "пятого", "пятом"], 5),
    **dict.fromkeys(["шесть", "шести", "шестого", "шестом"], 6),
    **dict.fromkeys(["семь", "семи", "седьмого", "седьмом"], 7),
    **dict.fromkeys(["восемь", "восьми", "восьмого", "восьмом"], 8),
    **dict.fromkeys(["девять", "девяти", "девятого", "девятом"], 9),
    **dict.fromkeys(["десять", "десяти", "десятого", "десятом"], 10),
    **dict.fromkeys(["пятнадцать", "пятнадцати"], 15),
    **dict.fromkeys(["двадцать", "двадцати"], 20),
    **dict.fromkeys(["тридцать", "тридцати", "полчаса"], 30),
}
NUM = r"(?:\d+(?:[.,]\d+)?|" + "|".join(sorted(WORD_NUMBERS, key=len, reverse=True)) + r")"

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}  # fmt: skip
MONTH_RE = r"(?P<m>январ|феврал|март|апрел|ма[йея]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*"
CITIES = [
    (r"\b(?:сочи|адлер\w*|сириус\w*|красн\w+ полян\w*|лазаревск\w*|хост\w*|дагомыс\w*|лоо)\b",
     "sochi"),
    (r"\bмоскв\w*\b", "moscow"),
    (r"\b(?:петербург\w*|питер\w*|спб)\b", "spb"),
    (r"\bказан\w*\b", "kazan"),
    (r"\bкалининград\w*\b", "kaliningrad"),
    (r"\bминск\w*\b", "minsk"),
    (r"\bстамбул\w*\b", "istanbul"),
    (r"\bдуба[йея]\w*\b", "dubai"),
]  # fmt: skip


@dataclass
class Atom:
    feature_id: str
    field: str = ""
    op: str = ""
    value: str = ""
    unit: str = ""
    params: dict[str, str] = dfield(default_factory=dict)
    importance: str = "medium"
    mapping: str = "exact"


@dataclass
class Hit:
    start: int
    end: int
    atoms: list[Atom]
    default: str  # обязательность без явных слов гостя: required | desired | unspecified
    clarify: list[CompactClarify] = dfield(default_factory=list)
    # Слова, на которые правило опиралось как на контекст («ночью», «при закрытых окнах»).
    context: list[tuple[int, int]] = dfield(default_factory=list)
    any_of: bool = False  # атомы — варианты одного требования (OR), а не все сразу


@dataclass
class Ctx:
    raw: str
    norm: str
    today: date
    month: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    time_local: str | None = None
    context: list[tuple[int, int]] = dfield(default_factory=list)

    @property
    def arrival_local(self) -> str | None:
        if self.date_from and self.time_local:
            return f"{self.date_from.isoformat()}T{self.time_local}:00"
        return None

    def sentence_bounds(self, pos: int) -> tuple[int, int]:
        start = max(self.norm.rfind(c, 0, pos) for c in ".;!?") + 1
        ends = [i for c in ".;!?" if (i := self.norm.find(c, pos)) >= 0]
        return start, min(ends) if ends else len(self.norm)

    def sentence(self, pos: int) -> str:
        start, end = self.sentence_bounds(pos)
        return self.norm[start:end]

    def matches_in_sentence(self, pattern: re.Pattern, pos: int) -> list[tuple[int, int]]:
        start, end = self.sentence_bounds(pos)
        return [
            (start + m.start(), start + m.end()) for m in pattern.finditer(self.norm[start:end])
        ]


def number(token: str) -> str:
    token = token.strip()
    return str(WORD_NUMBERS[token]) if token in WORD_NUMBERS else token.replace(",", ".")


Rule = Callable[[Ctx], Iterator[Hit]]
RULES: list[Rule] = []


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


def finditer(pattern: str, ctx: Ctx) -> Iterator[re.Match]:
    return re.finditer(pattern, ctx.norm)


# --- Вид и солнце ---


@rule
def sea_view(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"вид\w*\s+на\s+море|морск\w+\s+вид\w*|(?:видеть|видно|смотреть\s+на)\s+(?:\w+\s+)?море\b"
        r"(?:\s+(?:прямо\s+)?(?:из|с)\s+(?:окна|окон|номера|кровати|постели))?"
        r"|(?:красив\w+\s+)?мор\w*\s+(?:прямо\s+)?(?:из|с)\s+(?:окна|окон|номера|балкона|кровати|постели)"
    )
    for m in finditer(pattern, ctx):
        # Вид с кровати строже вида из точки окна (v1 №28) — соответствие частичное.
        mapping = "partial" if re.search(r"кроват|постел", m.group()) else "exact"
        yield Hit(m.start(), m.end(), [Atom("V01", value="true", mapping=mapping)], "required")


@rule
def mountain_view(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"вид\w*\s+на\s+горы|горн\w+\s+вид\w*|горы\s+из\s+окна|видеть\s+горы", ctx):
        yield Hit(m.start(), m.end(), [Atom("V02", value="true")], "required")


@rule
def landmark_view(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?P<t>[а-я]+\s+[а-я]+)\s+(?:обязательно\s+)?(?:был|была|было)\s+"
        r"вид(?:ен|на|но)\s+из\s+окна"
    )
    for m in finditer(pattern, ctx):
        target = ctx.raw[m.start("t") : m.end("t")]
        yield Hit(
            m.start(), m.end(), [Atom("V03", value="true", params={"target": target})], "required"
        )


@rule
def sunset(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:(?:видеть|смотреть)\s+(?:с\s+него\s+)?)?(?:вид\w*\s+на\s+)?(?:сам\w*\s+)?закат\w*"
        r"(?:\s+в\s+море)?(?:\s+(?:из|с)\s+(?:номера|окна|балкона|него))?"
    )
    for m in finditer(pattern, ctx):
        # «Закат в море» — диск у морского горизонта: строже, чем V05 (v1 №29).
        mapping = "partial" if "в море" in m.group() else "exact"
        params = {"date": ctx.date_from.isoformat()} if ctx.date_from else {}
        clarify = [] if ctx.date_from or ctx.month else [_need_dates()]
        atom = Atom("V05", value="true", params=params, mapping=mapping)
        yield Hit(m.start(), m.end(), [atom], "required", clarify)


@rule
def sun_hours(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:(?P<neg>поменьше|меньше|без|отсутстви\w*)\s+(?:\w+\s+)?)?"
        r"(?P<adj>прям\w+\s+|утренн\w+\s+|дневн\w+\s+)?солнц\w*"
        r"(?:\s+(?:было\s+)?(?P<when>утром|днем|после\s+обеда|за\s+завтраком|в\s+часы\s+сна))?"
    )
    windows = [(r"завтрак", ("07:00", "10:00")), (r"утр", ("07:00", "11:00")),
               (r"после\s+обеда", ("13:00", "18:00")),
               (r"\bдн(?:ем|евн)", ("11:00", "16:00"))]  # fmt: skip
    for m in finditer(pattern, ctx):
        if re.fullmatch(r"солнц\w*", m.group()):  # одно слово «солнце» без смысла — не условие
            continue
        window = next((w for k, w in windows if re.search(k, m.group())), None)
        params: dict[str, str] = {"month": str(ctx.month)} if ctx.month else {}
        if window:
            params |= {"time_from": window[0], "time_to": window[1]}
        op, value = ("lte", str(FEW_SUN_HOURS)) if m.group("neg") else ("gte", str(FEW_SUN_HOURS))
        atom = Atom("S02" if window else "S01", "hours", op, value, "h", params)
        clarify = [] if ctx.month else [_need_dates()]
        yield Hit(m.start(), m.end(), [atom], "desired", clarify)


def _need_dates() -> CompactClarify:
    return CompactClarify(
        question="На какие даты поездка? Без них не посчитать солнце и закат.",
        about="даты поездки",
        options=[],
    )


# --- Тишина и окружение ---

INDOOR = re.compile(
    r"в номере|внутри|в комнате|спальн\w*|(?:при\s+|с\s+)?(?:открыт|закрыт)\w*\s+(?:окн|створк)\w*"
    r"|чутко сплю|спать|сплю|\bсон\b|ночью|днем|в дневное время"
)
OUTDOOR = re.compile(r"снаружи|на улице|уличн\w*|дорог\w*|трасс\w*|вокруг|район\w*|по ночному шуму")


@rule
def quiet(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"\b(?:тих\w*|тишин\w*|потише)\b", ctx):
        sent = ctx.sentence(m.start())
        if OUTDOOR.search(sent):
            context = ctx.matches_in_sentence(OUTDOOR, m.start())
            yield Hit(m.start(), m.end(), _quiet_outside(), "desired", context=context)
        elif INDOOR.search(sent):
            params = {}
            if re.search(r"открыт\w+\s+(?:окн|створк)|с\s+открытым\s+окном", sent):
                params["windows_state"] = "open"
            elif re.search(r"закрыт\w+\s+окн", sent):
                params["windows_state"] = "closed"
            atoms = [Atom("N05", "LAeq_dBA", "lte", str(QUIET_INDOOR_DBA), "dB(A)")]
            if "windows_state" in params:  # замер при тех же окнах, что у гостя
                atoms.append(Atom("N05", "windows_state", "eq", params["windows_state"]))
            context = ctx.matches_in_sentence(INDOOR, m.start())
            yield Hit(m.start(), m.end(), atoms, "desired", context=context)
        else:
            clarify = CompactClarify(
                question="«Тихий» — важна тишина в самом номере или подальше от дорог и баров?",
                about=ctx.raw[m.start() : m.end()],
                options=["в номере", "подальше от дорог и баров", "и то и другое"],
            )
            yield Hit(m.start(), m.end(), _quiet_outside(), "desired", [clarify])


def _quiet_outside() -> list[Atom]:
    return [
        Atom("N02", "distance_m", "gte", str(QUIET_ROAD_M), "m", importance="medium"),
        Atom("N04", "count", "lte", str(QUIET_BARS_MAX), importance="low"),
    ]


_FEW_BARS = r"(?<!не\s)(?:поменьше|меньше|мало|немного|минимум|без|подальше\s+от)"
_MANY_BARS = r"(?<!не\s)(?:много|побольше|куча)"


@rule
def bars(ctx: Ctx) -> Iterator[Hit]:
    bar = r"\bбар(?:ы|ов|ам|ами|ах)?"
    pattern = (
        rf"(?:(?P<pre>{_FEW_BARS}|{_MANY_BARS})\s+)?(?:"
        rf"{bar}\s+(?:\w+\s+)?(?:рядом|недалеко|поблизости)(?:\s+нравятся)?"
        rf"|(?:рядом|недалеко|близость|поблизости)\s+(?:(?P<mid>{_FEW_BARS}|{_MANY_BARS})\s+)?"
        rf"(?:[кс]\s+)?{bar}\b)"
        rf"(?:\s+(?:было|будет|бы)?\s*(?P<post>{_FEW_BARS}|{_MANY_BARS})\b)?"
    )
    for m in finditer(pattern, ctx):
        # «Мало / поменьше баров рядом», «чтобы баров было мало» — меньше; «бары рядом
        # нравятся», «рядом много баров» — больше. Без слова о количестве — бары нужны.
        word = m.group("pre") or m.group("mid") or m.group("post") or ""
        few = re.fullmatch(_FEW_BARS, word) is not None
        op, value = ("lte", QUIET_BARS_MAX) if few else ("gte", BARS_LIKED_MIN)
        atom = Atom("N04", "count", op, str(value), importance="low")
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def green(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"зелен\w+\s+район\w*|много\s+зелен\w*|в\s+зелени", ctx):
        yield Hit(
            m.start(), m.end(), [Atom("E03", "", "gte", str(GREEN_SHARE_MIN), "%")], "desired"
        )


@rule
def park(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:подходящ\w+\s+)?парк\w*(?:\s+для\s+прогулок)?(?:\s+(?:рядом|поблизости))?"
        r"|(?:рядом|поблизости)\s+парк\w*"
    )
    for m in finditer(pattern, ctx):
        if re.search(r"парковк", m.group()):
            continue
        # Доля зелени — не близость парка: соответствие частичное, только мягкое.
        atom = Atom("E03", "", "gte", str(GREEN_SHARE_MIN), "%", mapping="partial")
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def construction(ctx: Ctx) -> Iterator[Hit]:
    pattern = r"(?:поменьше|меньше|без)\s+(?:\w+\s+)?строек|строй\w+\s+рядом|без\s+стро\w+"
    for m in finditer(pattern, ctx):
        atom = Atom("X03", "distance_m", "gte", str(CONSTRUCTION_M), "m")
        yield Hit(m.start(), m.end(), [atom], "desired")


# --- Пешком и транспорт ---

# Сравнение перед числом входит в совпадение правила и задаёт оператор:
# «не дальше 500 м» → lte, «не ближе 200 м» → gte. Иначе «не дальше» висело бы непонятым.
CMP_LTE = r"не\s+(?:больше|более|дальше|дольше)|максимум|в\s+пределах|до"
CMP_GTE = r"не\s+(?:ближе|меньше|менее|короче)|минимум|хотя\s+бы|от"
CMP = rf"(?:(?P<cmp>{CMP_LTE}|{CMP_GTE})\s+)?"
DIST_UNIT = r"(?P<unit>минут\w*|мин|метр\w*|м|км|километр\w*)"
CMP_BEFORE = re.compile(rf"\b(?P<w>{CMP_LTE}|{CMP_GTE})\s+$")


def cmp_op(word: str | None, default: str = "lte") -> str:
    if not word:
        return default
    return "gte" if re.fullmatch(CMP_GTE, word.strip()) else "lte"


def measured(ctx: Ctx, target: str) -> Iterator[tuple[re.Match, str]]:
    """Расстояние или время до цели с числом: «до пляжа [обязательно] не дальше 10 минут»
    и «не дальше 500 м от моря». Возвращает совпадение и оператор."""
    patterns = (
        rf"(?:до|к|от)\s+(?:ближайш\w+\s+)?{target}\s+(?:обязательно\s+)?{CMP}"
        rf"(?P<n>{NUM})\s*{DIST_UNIT}(?:\s+пешком)?",
        rf"{CMP}(?P<n>{NUM})\s*{DIST_UNIT}\s+(?:пешком\s+)?(?:до|от)\s+(?:ближайш\w+\s+)?{target}",
    )
    for pattern in patterns:
        for m in finditer(pattern, ctx):
            yield m, cmp_op(m.group("cmp"))


@rule
def beach_distance(ctx: Ctx) -> Iterator[Hit]:
    for m, op in measured(ctx, r"(?:пляж\w*|мор\w*)"):
        atom = Atom(
            "W01", "distance_m", op, number(m.group("n")), m.group("unit"), importance="high"
        )
        yield Hit(m.start(), m.end(), [atom], "required")
    vague = (
        r"(?:недалеко|близко|рядом|поближе|ближе|недолго\s+идти|близк\w+)\s+(?:от|к|до)?\s*"
        r"(?:мор\w*|пляж\w*)|\bу\s+моря|близк\w+\s+пляж\w*|(?:меньше|короче)\s+(?:пути|идти)\s+"
        r"до\s+(?:мор\w*|пляж\w*)"
    )
    for m in finditer(vague, ctx):
        atom = Atom("W01", "distance_m", "lte", str(NEAR_BEACH_M), "m", importance="high")
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def landmark_distance(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:недолго\s+идти|недалеко|близко|рядом|поближе|пешком)\s+(?:до|от|к)\s+"
        r"(?P<t>набережн\w*|центр\w*)"
    )
    for m in finditer(pattern, ctx):
        center = m.group("t").startswith("центр")
        params = {"target_kind": "center"} if center else {"target": "набережная"}
        meters = str(NEAR_CENTER_M)
        yield Hit(
            m.start(), m.end(), [Atom("W03", "distance_m", "lte", meters, "m", params)], "desired"
        )
    # С числом: «не дальше 1 км от центра», «до набережной не больше 10 минут».
    for m, op in measured(ctx, r"(?P<t>набережн\w*|центр\w*)"):
        center = m.group("t").startswith("центр")
        params = {"target_kind": "center"} if center else {"target": "набережная"}
        atom = Atom("W03", "distance_m", op, number(m.group("n")), m.group("unit"), params)
        yield Hit(m.start(), m.end(), [atom], "required")


@rule
def stairs(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:до\s+(?P<to>пляж\w*|мор\w*|набережн\w*|остановк\w*)\s+(?:\w+\s+)?без\s+лестниц\w*"
        r"|без\s+лестниц\w*\s+до\s+(?P<to2>пляж\w*|мор\w*|набережн\w*|остановк\w*))"
    )
    for m in finditer(pattern, ctx):
        to = m.group("to") or m.group("to2")
        params = (
            {"target_kind": "stop"} if to.startswith("остановк")
            else {"target": "набережная"} if to.startswith("набережн")
            else {"target_kind": "beach"}
        )  # fmt: skip
        # Ноль известных лестниц в OSM не доказывает, что лестниц нет: соответствие частичное.
        atom = Atom("W05", "known_step_sections", "eq", "0", params=params, mapping="partial")
        yield Hit(m.start(), m.end(), [atom], "required")


@rule
def stop_distance(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:недалеко|близко|рядом|поближе|меньше\s+(?:пешего\s+)?(?:времени|идти))\s+"
        r"(?:до|от|к)\s+(?:ближайшей\s+)?(?:остановк\w*|метро|станци\w*)"
        r"|(?:до|от)\s+(?:ближайшей\s+)?остановк\w*\s+(?:\w+\s+){0,3}?(?:недалеко|поменьше|близко)"
        r"|(?:остановк\w*|метро|станци\w*)\s+(?:\w+\s+)?(?:недалеко|рядом|поблизости)"
    )
    for m in finditer(pattern, ctx):
        params = {"target_kind": "stop"}
        atom = Atom("W02", "distance_m", "lte", str(NEAR_STOP_M), "m", params)
        yield Hit(m.start(), m.end(), [atom], "desired")
    # С числом: «не дальше 300 метров до остановки», «до метро не больше 5 минут».
    for m, op in measured(ctx, r"(?:остановк\w*|метро|станци\w*)"):
        params = {"target_kind": "stop"}
        atom = Atom("W02", "distance_m", op, number(m.group("n")), m.group("unit"), params)
        yield Hit(m.start(), m.end(), [atom], "required")


@rule
def ascent(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"без\s+(?:крут\w+\s+|больш\w+\s+)?подъем\w*|меньш\w+\s+подъем\w*", ctx):
        kind = "stop" if "остановк" in ctx.sentence(m.start()) else "beach"
        params = {"target_kind": kind}
        atom = Atom("W04", "dsm_positive_dz_estimate_m", "lte", str(MAX_ASCENT_M), "m", params)
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def transfers(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"без\s+пересад\w*", ctx):
        feature = "T02" if "вокзал" in ctx.norm and "аэропорт" not in ctx.norm else "T01"
        params = {"arrival_local": ctx.arrival_local} if ctx.arrival_local else {}
        yield Hit(
            m.start(), m.end(), [Atom(feature, "transfers", "eq", "0", params=params)], "required"
        )


@rule
def public_transport(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"(?:только\s+)?общественн\w+\s+транспорт\w*", ctx):
        params = {"arrival_local": ctx.arrival_local} if ctx.arrival_local else {}
        atom = Atom("T03", "route_exists", "eq", "true", params=params)
        yield Hit(m.start(), m.end(), [atom], "required")


# --- Места рядом ---


@rule
def food_open(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:открыт\w+\s+)?(?:еда|кафе|поесть|перекусить)\s+(?:\w+\s+){0,3}?"
        r"(?:поблизости|рядом|в\s+пешей\s+доступности|недалеко|близко)"
        r"|(?:поздн\w+|открыт\w+|ночн\w+)\s+кафе(?:\s+рядом)?|(?:была\s+)?открыта\s+еда"
        r"(?:\s+в\s+пешей\s+доступности)?"
    )
    for m in finditer(pattern, ctx):
        params = {"arrival_local": ctx.arrival_local} if ctx.arrival_local else {}
        atom = Atom("L02", "distance_m", "lte", str(NEAR_FOOD_M), "m", params)
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def pharmacy(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:круглосуточн\w+\s+|ночн\w+\s+|открыт\w+\s+)?аптек\w*(?:,?\s+открыт\w+)?"
        r"(?:\s+(?:рядом|поблизости|недалеко))?"
    )
    for m in finditer(pattern, ctx):
        params = {"arrival_local": ctx.arrival_local} if ctx.arrival_local else {}
        atom = Atom("L01", "distance_m", "lte", str(NEAR_PHARMACY_M), "m", params)
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def vet(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"ветклиник\w*(?:\s+(?:недалеко|рядом|поблизости))?|ветеринар\w*", ctx):
        atom = Atom("P04", "distance_m", "lte", str(NEAR_VET_M), "m")
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def beach_surface(ctx: Ctx) -> Iterator[Hit]:
    pattern = r"(?P<s>песчан|галечн)\w*\s+пляж\w*|пляж\w*\s+с\s+(?P<s2>песк|гальк)\w*"
    for m in finditer(pattern, ctx):
        sand = (m.group("s") or m.group("s2")).startswith("пес")
        atom = Atom("B01", "surface", "eq", "sand" if sand else "pebble")
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def playground(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"(?:детск\w+\s+|игров\w+\s+)?площадк\w*(?:\s+рядом)?", ctx):
        atom = Atom("F01", "distance_m", "lte", str(NEAR_PLAYGROUND_M), "m")
        yield Hit(m.start(), m.end(), [atom], "desired")


# Слова вокруг места: «рядом» (радиус — политика словаря мест), «в 5 минутах» (радиус гостя),
# «в отеле» / «отель с …» (место в самом отеле). «Рядом с морем» и «в 5 минутах от моря» —
# про море, а не про место: такие слова не забираем у правил моря и центра.
_BE = r"(?:\s+(?:был\w*|находил\w*|есть))?"
_NEAR_WORD = (
    r"(?:рядом|недалеко|поблизости|неподалеку|близко|возле|около|под\s+боком|по\s+соседству"
    r"|в\s+(?:шаговой|пешей)\s+доступности)"
)
_DIST = (
    rf"(?:в|не\s+дальше|не\s+более|в\s+пределах|до)\s+(?P<n>{NUM})\s*"
    r"(?P<u>минут\w*|мин|метр\w*|м|км|километр\w*)(?!\w)(?:\s+(?:пешком|ходьбы))?"
)
_SITE = r"(?:в|при)\s+(?:самом\s+)?(?:отел\w*|гостиниц\w*|здании)|на\s+территории(?:\s+отел\w*)?"
_PLACE_BEFORE = re.compile(
    rf"(?:(?P<near>{_NEAR_WORD}(?:\s+(?:с|со|от|к)(?:\s+отел\w*)?)?)"
    rf"|{_DIST}(?:\s+от\s+отел\w*)?|(?P<site>{_SITE}|собственн\w*|сво(?:й|им|ей|ю|е|его))"
    rf"|(?P<with>\bсо?)){_BE}\s+$"
)
_PLACE_AFTER = re.compile(
    rf"{_BE}\s+(?:(?P<near>{_NEAR_WORD}(?:\s+(?:с|со|от|к)\s+отел\w*|(?!\s+(?:с|со|от|к|до)\b)))"
    rf"|{_DIST}(?:\s+от\s+отел\w*|(?!\s+(?:от|до)\b))|(?P<site>{_SITE}))"
)


def _named_place(raw: str, start: int, end: int) -> bool:
    """«в Зимнем театре», «ресторан «Пушкин»» — конкретное место (цель), а не «театр рядом»."""
    if re.match(r"\s*[«\"„]|\s+[А-ЯЁA-Z]", raw[end:]):
        return True
    prev = re.search(r"(\S+)\s+$", raw[:start])
    if not prev or not prev.group(1)[0].isupper():
        return False
    head = raw[: prev.start()].rstrip()
    return bool(head) and head[-1] not in ".!?"  # заглавная не в начале предложения — имя


@rule
def nearby_place(ctx: Ctx) -> Iterator[Hit]:
    """«Рядом итальянский ресторан», «музей в 10 минутах» — вне каталога, но с категорией из
    словаря мест (задача 4, D34): проверка ждёт таблицу «отель × места рядом» в пакете города."""
    for start, end, category in load_places().find(ctx.norm):
        if _named_place(ctx.raw, start, end):
            continue  # названное место — цель маршрута, его разберут «доехать до» или модель
        before = _PLACE_BEFORE.search(ctx.norm, max(0, start - 60), start)
        after = _PLACE_AFTER.match(ctx.norm, end)
        mods = [m for m in (before, after) if m]
        if category.needs_near and not mods:
            continue  # «бассейн», «ресторан» без «рядом» — скорее про сам отель; решит модель
        params = {"nearest_contract": "nearby_place"}
        for m in mods:
            if m.group("n"):
                params |= {"distance": number(m.group("n")), "distance_unit": m.group("u")}
            if m.group("site") or m.groupdict().get("with"):
                params["on_site"] = "true"
        atom = Atom("", value=category.id, params=params, mapping="unmatched")
        yield Hit(before.start() if before else start, after.end() if after else end,
                  [atom], "desired")  # fmt: skip


@rule
def trip_to_target(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"(?:доехать|добраться|дойти)\s+до\s+(?P<t>[\w-]+(?:\s+[\w-]+)?)", ctx):
        target = ctx.raw[m.start("t") : m.end("t")]
        if re.match(r"(?:него|нее|неё|них|отеля)\b", m.group("t")):
            continue  # «дойти до него» — цель в другом месте фразы, пусть разберёт модель
        atom = Atom("", value=target, params={"nearest_contract": "trip_to_target"},
                    mapping="unmatched")  # fmt: skip
        yield Hit(m.start(), m.end(), [atom], "required")
    for m in finditer(r"съездить\s+в\s+(?P<t>[\w-]+(?:\s+[\w-]+)?)", ctx):
        target = ctx.raw[m.start("t") : m.end("t")]
        atom = Atom("", value=target, params={"nearest_contract": "same_day_round_trip"},
                    mapping="unmatched")  # fmt: skip
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def subjective(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(
        r"\b(?:уютн\w*|атмосферн\w*|романтичн\w*|стильн\w*|вкусн\w+(?:\s+\w+)?\s+ед\w*)", ctx
    ):
        atom = Atom("subjective", value=ctx.raw[m.start() : m.end()], mapping="unmatched")
        yield Hit(m.start(), m.end(), [atom], "desired")


# --- Номер и отель: только отельер или размещение ---


@rule
def floors(ctx: Ctx) -> Iterator[Hit]:
    n = rf"(?P<n>{NUM})"
    patterns = [
        (rf"(?:только\s+)?(?:на\s+)?(?P<a>{NUM})\s+или\s+(?P<b>{NUM})\s+этаж\w*", "in", "required"),
        (rf"выше\s+{n}\s+этаж\w*\s+не\s+рассматрива\w*", "lte", "required"),
        (rf"(?:этаж\w*\s+)?не\s+выше\s+{n}(?:\s+этаж\w*)?", "lte", "required"),
        (rf"(?:номер\s+)?(?:только\s+)?на\s+{n}\s+этаж\w*", "eq", "required"),
        (
            r"(?:этаж\w*\s+(?:\w+\s+){0,2}?повыше|повыше|(?:более\s+)?высок\w+\s+этаж\w*)",
            "gte",
            "desired",
        ),
    ]
    for pattern, op, default in patterns:
        for m in finditer(pattern, ctx):
            if op == "lte" and "этаж" not in m.group() and "этаж" not in ctx.sentence(m.start()):
                continue
            if op == "in":
                value = f"{number(m.group('a'))},{number(m.group('b'))}"
            elif op == "gte":
                value = str(HIGH_FLOOR_MIN)
            else:
                value = number(m.group("n"))
            yield Hit(m.start(), m.end(), [Atom("floor", "ordinal", op, value)], default)


@rule
def courtyard(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"окна\s+(?:\w+\s+)?(?:выходили\s+)?во\s+двор|во\s+двор", ctx):
        yield Hit(m.start(), m.end(), [Atom("courtyard", "is_courtyard", "eq", "true")], "required")


# Вопросы отельеру: поля — ровно `parsed_fields` вопроса (контракт core/hotelier.py). «Есть» там,
# где у вопроса нет поля да / нет, — «значение не none» (schemas.PRESENCE в agents/hotelier).
def _simple(
    pattern: str, feature: str, field_: str, op: str = "eq", value: str = "true", **kw
) -> Rule:
    def fn(ctx: Ctx) -> Iterator[Hit]:
        for m in finditer(pattern, ctx):
            yield Hit(m.start(), m.end(), [Atom(feature, field_, op, value, **kw)], "required")

    fn.__name__ = f"rule_{feature}_{field_}"
    return rule(fn)


_simple(r"балкон\w*|лоджи\w*", "M02", "private_balcony")
_simple(r"\bтеррас\w*(?!\s+общ)", "M02", "private_terrace")
_simple(r"кондиционер\w*|кондей", "S04", "hvac_type", "ne", "none")
_simple(
    r"(?:полноценн\w+\s+|полн\w+\s+)?затемнени\w*|блэкаут|blackout"
    r"|полн\w+\s+темнот\w*(?:\s+в\s+(?:комнате|номере))?|плотн\w+\s+штор\w*",
    "S05",
    "opaque_coverage",
    value="full",
)
_simple(r"(?:(?:нормальн|рабоч|удобн|пригодн)\w*\s+){1,2}стол(?:а|ом)?\b(?:\s+для\s+работы)?"
        r"|\bстол(?:а|ом)?\s+для\s+работы", "R03", "desk_clear_width_mm", "gte",
        str(DESK_MIN_MM), unit="mm")  # fmt: skip
_simple(
    r"(?:собственн\w+\s+|сво\w+\s+)?парковк\w*(?:\s+у\s+отеля|\s+в\s+отеле)?",
    "T04",
    "parking_owner",
    value="own",
)
_simple(r"(?:детск\w+\s+)?кроватк\w*", "F03", "cot_dimensions", "ne", "none")
_simple(r"смежн\w+\s+номер\w*", "F04", "internal_connecting_door")
_simple(r"детск\w+\s+бассейн\w*", "F05", "pool_id", "ne", "none")
_simple(
    r"(?:своя\s+|приватн\w+\s+)?кухн\w*\s+в\s+номере|кухонн\w+\s+зон\w*",
    "M03",
    "private_kitchenette",
)
_simple(r"\bчайник\w*", "M04", "in_room_kettle")
_simple(r"\bсейф\w*", "M05", "access", "ne", "none")
_simple(
    r"душ\w*\s+без\s+(?:бортик|порог)\w*",
    "A04",
    "shower_threshold_mm",
    "lte",
    str(STEP_FREE_MM),
    unit="mm",
)


@rule
def lift(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"лифт\w*", ctx):
        if re.search(r"не\s+пользу\w*\s*$", ctx.norm[max(0, m.start() - 20) : m.start()]):
            continue  # «не пользуется лифтом» — не требование лифта
        yield Hit(m.start(), m.end(), [Atom("A03", "lift_id", "ne", "none")], "required")


@rule
def step_free(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:вход\w*\s+(?:с\s+улицы\s+)?|от\s+улицы\s+до\s+номера\s+(?:\w+\s+)?)?"
        r"без\s+ступен\w*|пандус\w*"
    )
    for m in finditer(pattern, ctx):
        # Путь до номера целиком строже, чем вход в вестибюль (A02): соответствие частичное.
        mapping = "partial" if "до номера" in m.group() else "exact"
        atom = Atom("A02", "threshold_mm", "lte", str(STEP_FREE_MM), "mm", mapping=mapping)
        yield Hit(m.start(), m.end(), [atom], "required")


@rule
def pets(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        rf"(?:с\s+)?(?P<sp>собак\w*|\bпес\b|\bпса\b|кошк\w*|\bкот(?:ом|а)?\b|питом\w*|животн\w*)"
        rf"(?:[\s,]+(?:\w+\s+){{0,2}}?(?P<kg>{NUM})\s*(?:кг|килограмм\w*))?"
    )
    for m in finditer(pattern, ctx):
        sp = m.group("sp")
        if re.match(r"собак|пес|пса", sp):
            atoms = [Atom("P01", "species", "in", "dog,dog_cat,any")]
        elif re.match(r"кошк|кот", sp):
            atoms = [Atom("P01", "species", "in", "cat,dog_cat,any")]
        else:
            atoms = [Atom("P01", "species", "ne", "none")]
        if m.group("kg"):
            atoms.append(Atom("P01", "weight_kg", "gte", number(m.group("kg")), "kg"))
        yield Hit(m.start(), m.end(), atoms, "required")


@rule
def charger(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:зарядк\w*|зарядн\w+\s+станци\w*)(?:\s+(?:для\s+электромобил\w*|type\s*2|тайп\s*2))?"
        r"(?P<near>\s+(?:поблизости|рядом))?"
    )
    for m in finditer(pattern, ctx):
        if m.group("near"):
            # Зарядка где-то рядом — не зарядка отеля (T05): другой смысл, вне каталога.
            atom = Atom("T05", value=m.group(), mapping="different_meaning")
            yield Hit(m.start(), m.end(), [atom], "desired")
            continue
        if re.search(r"type\s*2|тайп\s*2", m.group()):
            atom = Atom("T05", "connector", "eq", "type2")
        else:
            atom = Atom("T05", "connector", "ne", "none")
        yield Hit(m.start(), m.end(), [atom], "required")


@rule
def checkin(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:заселени\w*|заселиться|заезд\w*|приняли|принимали|заселят)\s+(?:\w+\s+){0,2}?"
        r"(?:ночью|после\s+полуночи|поздно|поздним\s+вечером)|ночн\w+\s+заселени\w*|поздн\w+\s+заезд\w*"
    )
    for m in finditer(pattern, ctx):
        # Заселиться ночью можно при круглосуточной стойке ИЛИ при самозаселении.
        atoms = [
            Atom("L04", "front_desk_hours", "eq", "24/7"),
            Atom("L04", "self_checkin", "eq", "true"),
        ]
        yield Hit(m.start(), m.end(), atoms, "required", any_of=True)


@rule
def wifi(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        rf"(?:(?:проверенн\w+|быстр\w+|хорош\w+)\s+)?(?:wi-?fi|вай-?фай|интернет\w*)"
        rf"(?:\s+[\w-]+){{0,4}}?\s+(?P<n>{NUM})\s*(?:мегабит\w*|мбит\w*|mbps)"
    )
    for m in finditer(pattern, ctx):
        upload = re.search(r"отдач|upload|выгрузк", m.group())
        field_ = "upload_Mbps" if upload else "download_Mbps"
        atom = Atom("R01", field_, "gte", number(m.group("n")), "Mbit/s")
        yield Hit(m.start(), m.end(), [atom], "required")
    for m in finditer(r"стабильн\w+\s+(?:wi-?fi|вай-?фай|интернет\w*)", ctx):
        atom = Atom("R02", "latency_ms", "lte", "100", "ms", mapping="partial")
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def built_before(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"(?:построен\w*|постройки)\s+до\s+(?P<y>\d{4})(?:\s+года)?", ctx):
        yield Hit(m.start(), m.end(), [Atom("H01", "year", "lt", m.group("y"))], "required")


@rule
def warm_sea(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"тепл\w+\s+мор\w*|температур\w+\s+(?:воды|моря)", ctx):
        params = {"month": str(ctx.month)} if ctx.month else {}
        atom = Atom("B02", "mean_C", "gte", str(WARM_SEA_C), "°C", params)
        yield Hit(m.start(), m.end(), [atom], "desired")


@rule
def hotel_renovation(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"в\s+(?:самом\s+)?отеле\s+не\s+будет\s+ремонт\w*"
        r"|ремонт\w*\s+(?:в\s+(?:самом\s+)?отеле|внутри)"
    )
    for m in finditer(pattern, ctx):
        atom = Atom("", value="ремонт в отеле в даты поездки", mapping="unmatched")
        yield Hit(m.start(), m.end(), [atom], "required")


# --- Контекст: город, даты, время ---


def _context(ctx: Ctx) -> tuple[str, int]:
    city = ""
    for pattern, city_id in CITIES:
        for m in finditer(pattern, ctx):
            city = city or city_id
            ctx.context.append((m.start(), m.end()))
    year_re = r"(?:\s+(?P<y>\d{4})(?:\s*(?:года|г\.?|-го))?)?"
    dates = rf"(?P<d1>\d{{1,2}})(?:\s*[–-]\s*(?P<d2>\d{{1,2}}))?\s+{MONTH_RE}{year_re}"
    for m in finditer(dates, ctx):
        month = _month(m.group("m"))
        year = int(m.group("y")) if m.group("y") else None
        d1 = _date(year, month, int(m.group("d1")), ctx.today)
        d2 = _date(year, month, int(m.group("d2")), ctx.today) if m.group("d2") else None
        if d1 and ctx.date_from is None:
            ctx.date_from, ctx.date_to, ctx.month = d1, d2, month
        ctx.context.append((m.start(), m.end()))
    for m in finditer(rf"\bв\s+{MONTH_RE}", ctx):
        ctx.month = ctx.month or _month(m.group("m"))
        ctx.context.append((m.start(), m.end()))
    for m in finditer(r"\b(?:в\s+)?(?P<h>[01]?\d|2[0-3])[:.](?P<mi>[0-5]\d)\b", ctx):
        ctx.time_local = ctx.time_local or f"{int(m.group('h')):02d}:{m.group('mi')}"
        ctx.context.append((m.start(), m.end()))
    for m in finditer(r"\bв\s+час\s+ночи\b", ctx):
        ctx.time_local = ctx.time_local or "01:00"
        ctx.context.append((m.start(), m.end()))
    for m in finditer(
        r"(?:в\s+)?(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)", ctx
    ):
        ctx.context.append((m.start(), m.end()))
    guests = (
        r"с\s+(?:малыш\w*|реб[её]нк\w*|детьми|мамой|дедушк\w*|бабушк\w*)|с\s+детск\w+\s+коляск\w*"
        r"|вдвоем|на\s+машине|(?:из|в|до)\s+аэропорт\w*|(?:с|из|на)\s+вокзал\w*"
        r"|(?:на|в)\s+(?:годовщин\w*|день\s+рождени\w*|медовый\s+месяц|выходные|отпуск\w*"
        r"|командировк\w*)|романтическ\w+\s+выходн\w*"
    )
    for m in finditer(guests, ctx):
        ctx.context.append((m.start(), m.end()))
    adults = 2 if re.search(r"\bвдвоем\b|\bс\s+(?:женой|мужем|девушкой|парнем)\b", ctx.norm) else 0
    return city, adults


def _month(token: str) -> int:
    return next(v for k, v in MONTHS.items() if token.startswith(k))


def _date(year: int | None, month: int, day: int, today: date) -> date | None:
    try:
        if year:
            return date(year, month, day)
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


# --- Сборка ---


def requiredness(ctx: Ctx, pos: int, default: str) -> str:
    return marker_requiredness(ctx.norm, pos) or default


# Отрицание прямо перед фразой: «без собак», «не на первом этаже». Где «без» — часть смысла
# («без ступеней», «без пересадок»), оно внутри совпадения правила и сюда не попадает.
NEGATION_BEFORE = re.compile(r"(?:^|[^\w-])(?P<w>не|без)\s+$")
# «Балкон не нужен», «вид не важен» — требование снято, а не перевёрнуто. «Не обязательно» —
# не снято, а смягчено: это решает обязательность (`marker_requiredness`).
DISMISSED_AFTER = re.compile(r"^\s*(?:[—–-]\s*)?не\s*(?:нуж|важ)\w*")
# «Хвосты» понятых фраз: «рядом с аптекой» — расстояние понято, «рядом» не должно висеть отдельно.
TAIL_BEFORE = re.compile(
    r"(?P<w>\b(?:рядом|поблизости|недалеко|неподалеку|близко|возле|около))\s+(?:(?:с|со|от|к|до)\s+)?$"
)
TAIL_AFTER = re.compile(r"^\s+(?:рядом|поблизости|недалеко|неподалеку|близко)\b")


def _negate(atom: Atom, catalog: Catalog) -> Atom | None:
    """Обратное условие того же смысла; нельзя выразить — None (кусок останется непонятым)."""
    feature = catalog.get(atom.feature_id)
    if feature is None or atom.mapping != "exact":
        return None
    flipped = negate(feature, atom.field, atom.op, atom.value)
    if flipped is None:
        return None
    return replace(atom, op=flipped[0], value=flipped[1])


def _apply_comparison(ctx: Ctx, h: Hit) -> Hit:
    """Сравнение прямо перед числом, которого нет в шаблоне правила: тоже входит в совпадение."""
    if len(h.atoms) != 1 or not re.match(rf"{NUM}\b", ctx.norm[h.start :]):
        return h
    m = CMP_BEFORE.search(ctx.norm[: h.start])
    if not m:
        return h
    atom = h.atoms[0]
    if atom.op in ("lte", "gte", "lt", "gt"):
        atom = replace(atom, op=cmp_op(m.group("w")))
    return replace(h, start=m.start("w"), atoms=[atom])


def _apply_negation(ctx: Ctx, h: Hit, catalog: Catalog) -> Hit | None:
    if DISMISSED_AFTER.match(ctx.norm[h.end :]):
        return None
    m = NEGATION_BEFORE.search(ctx.norm[: h.start])
    if not m:
        return h
    if len(h.atoms) != 1 or h.any_of:
        return None  # отрицание сложной фразы правилами не выразить — оставляем непонятым
    atom = _negate(h.atoms[0], catalog)
    if atom is None:
        return None
    return replace(h, start=m.start("w"), atoms=[atom], clarify=[])


def parse_rules(
    raw: str, catalog: Catalog, today: date
) -> tuple[CompactAST, dict[str, tuple[int, int]]]:
    """Разбор правилами → компактный AST и точные позиции атомов."""
    norm = normalize(raw)
    ctx = Ctx(raw=raw, norm=norm, today=today)
    city, adults = _context(ctx)

    hits = [h for r in RULES for h in r(ctx)]
    # Пересечения: оставляем более длинное совпадение, при равной длине — раннее правило.
    hits.sort(key=lambda h: (-(h.end - h.start), h.start))
    kept: list[Hit] = []
    for h in hits:
        if all(h.end <= k.start or h.start >= k.end for k in kept):
            kept.append(h)
    # Отрицание: обратное условие или ничего (кусок останется непонятым), но не противоположное.
    kept = [_apply_comparison(ctx, h) for h in kept]
    kept = [n for h in kept if (n := _apply_negation(ctx, h, catalog))]
    kept.sort(key=lambda h: h.start)
    for h in kept:
        if m := TAIL_BEFORE.search(ctx.norm[: h.start]):
            ctx.context.append((m.start("w"), h.start))
        if m := TAIL_AFTER.match(ctx.norm[h.end :]):
            ctx.context.append((h.end, h.end + m.end()))

    atoms: list[CompactAtom] = []
    spans: dict[str, tuple[int, int]] = {}
    clarify: list[CompactClarify] = []
    or_groups: list[OrGroup] = []
    pets: list[str] = []
    for h in kept:
        req = requiredness(ctx, h.start, h.default)
        if h.any_of:
            ids = [f"a{len(atoms) + i + 1}" for i in range(len(h.atoms))]
            or_groups.append(OrGroup(group_id=f"g{len(or_groups) + 1}", atom_ids=ids))
        for a in h.atoms:
            atom_id = f"a{len(atoms) + 1}"
            spans[atom_id] = (h.start, h.end)
            atoms.append(
                CompactAtom(
                    atom_id=atom_id,
                    text=raw[h.start : h.end],
                    requiredness=req,
                    provenance="explicit",
                    feature_id=a.feature_id,
                    field=a.field,
                    op=a.op,
                    value=a.value,
                    unit=a.unit,
                    params=[Param(key=k, value=v) for k, v in a.params.items()],
                    mapping=a.mapping,
                    importance=a.importance,
                )
            )
            if a.feature_id == "P01" and a.field == "species":
                pets.append("cat" if re.search(r"кош|кот", norm[h.start : h.end]) else "dog")
        for c in h.clarify:
            if all(c.about != x.about for x in clarify):
                clarify.append(c)
        ctx.context += h.context

    compact = CompactAST(
        city=city,
        date_from=ctx.date_from.isoformat() if ctx.date_from else "",
        date_to=ctx.date_to.isoformat() if ctx.date_to else "",
        adults=str(adults) if adults else "",
        children_ages=[],
        pets=pets,
        context_text=[raw[s:e] for s, e in ctx.context],
        atoms=atoms,
        or_groups=or_groups,
        negated_atoms=[],
        clarify=clarify,
    )
    spans |= {f"ctx{i}": span for i, span in enumerate(ctx.context)}
    return compact, spans
