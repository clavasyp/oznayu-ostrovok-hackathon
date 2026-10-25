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
from dataclasses import dataclass
from dataclasses import field as dfield
from datetime import date

from agents.catalog import Catalog
from agents.search.compact import CompactAST, CompactAtom, CompactClarify, Param
from agents.search.text import normalize

# --- Пороги политики (не измерения): меняются здесь и видны в разборе ---
NEAR_BEACH_M = 800  # «недалеко от моря» — как в примере трека
NEAR_CENTER_M = 1500
NEAR_STOP_M = 400
NEAR_FOOD_M = 500
NEAR_PLAYGROUND_M = 500
NEAR_PHARMACY_M = 1000
NEAR_VET_M = 3000
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

REQ_MARKERS = re.compile(
    r"\b(обязательн\w*|только|нужн\w*|необходим\w*|должн\w*|точно|строго|непременно|"
    r"не рассматрива\w*|важно)\b"
)
DES_MARKERS = re.compile(
    r"\b(хотелось бы|хочется|желательно|лучше|был[аио]? бы|плюс\w*|бонус\w*|приятн\w*|"
    r"хорошо бы|по возможности|неплохо|удобн\w*|здорово|предпочтительн\w*|поменьше|побольше|"
    r"поближе|подальше|повыше|пониже|нравятся|нравится)\b"
)

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
CUISINES = {
    "итальянск": "italian", "грузинск": "georgian", "японск": "japanese", "китайск": "chinese",
    "французск": "french", "узбекск": "uzbek", "армянск": "armenian", "индийск": "indian",
    "мексиканск": "mexican", "тайск": "thai", "вьетнамск": "vietnamese", "корейск": "korean",
    "кавказск": "caucasian", "русск": "russian", "местн": "regional",
}  # fmt: skip


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
            atom = Atom("N05", "laeq_dba", "lte", str(QUIET_INDOOR_DBA), "dB(A)", params)
            context = ctx.matches_in_sentence(INDOOR, m.start())
            yield Hit(m.start(), m.end(), [atom], "desired", context=context)
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


@rule
def bars(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:(?P<neg>поменьше|меньше|без|подальше\s+от)\s+)?(?:"
        r"\bбар(?:ы|ов|ам|ами|ах)?\s+(?:\w+\s+)?(?:рядом|недалеко|поблизости)(?:\s+нравятся)?"
        r"|(?:рядом|недалеко|близость|поблизости)\s+(?:к\s+)?\bбар(?:ы|ов|ам|ами|ах)?\b)"
    )
    for m in finditer(pattern, ctx):
        # «Поменьше баров рядом» — меньше; «бары рядом нравятся» — больше.
        op, value = ("lte", QUIET_BARS_MAX) if m.group("neg") else ("gte", BARS_LIKED_MIN)
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


@rule
def beach_distance(ctx: Ctx) -> Iterator[Hit]:
    unit = r"(?P<unit>минут\w*|мин|метр\w*|м|км|километр\w*)"
    exact = (
        rf"(?:до|к)\s+(?:пляж\w*|мор\w*)\s+(?:обязательно\s+)?(?:не\s+больше|не\s+более|максимум|"
        rf"не\s+дальше|в\s+пределах|до)?\s*(?P<n>{NUM})\s*{unit}(?:\s+пешком)?"
        rf"|(?P<n2>{NUM})\s*(?P<unit2>минут\w*|мин|метр\w*|м|км)\s+(?:пешком\s+)?(?:до|от)\s+"
        rf"(?:пляж\w*|мор\w*)"
    )
    for m in finditer(exact, ctx):
        n, u = (
            (m.group("n"), m.group("unit")) if m.group("n") else (m.group("n2"), m.group("unit2"))
        )
        atom = Atom("W01", "distance_m", "lte", number(n), u, importance="high")
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


@rule
def nearby_place(ctx: Ctx) -> Iterator[Hit]:
    """«Рядом итальянский ресторан» — пока вне каталога, с категорией для задачи 4 (D34)."""
    keys = "|".join(CUISINES)
    pattern = rf"(?P<c>{keys})\w*\s+(?:ресторан\w*|кухн\w*|кафе|ед\w*)"
    for m in finditer(pattern, ctx):
        cuisine = CUISINES[m.group("c")]
        atom = Atom("", value=f"cuisine={cuisine}", params={"nearest_contract": "nearby_place"},
                    mapping="unmatched")  # fmt: skip
        yield Hit(m.start(), m.end(), [atom], "desired")


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


def _simple(pattern: str, feature: str, field_: str, default: str = "required", **kw) -> Rule:
    def fn(ctx: Ctx) -> Iterator[Hit]:
        for m in finditer(pattern, ctx):
            yield Hit(m.start(), m.end(), [Atom(feature, field_, "eq", "true", **kw)], default)

    fn.__name__ = f"rule_{feature}_{field_}"
    return rule(fn)


_simple(r"балкон\w*|лоджи\w*", "M02", "private_balcony")
_simple(r"\bтеррас\w*(?!\s+общ)", "M02", "private_terrace")
_simple(r"кондиционер\w*|кондей", "S04", "has_ac")
_simple(
    r"(?:полноценн\w+\s+|полн\w+\s+)?затемнени\w*|блэкаут|blackout"
    r"|полн\w+\s+темнот\w*(?:\s+в\s+(?:комнате|номере))?|плотн\w+\s+штор\w*",
    "S05",
    "full_blackout",
)
_simple(r"(?:(?:нормальн|рабоч|удобн|пригодн)\w*\s+){1,2}стол(?:а|ом)?\b(?:\s+для\s+работы)?"
        r"|\bстол(?:а|ом)?\s+для\s+работы", "R03", "has_desk")  # fmt: skip
_simple(
    r"(?:собственн\w+\s+|сво\w+\s+)?парковк\w*(?:\s+у\s+отеля|\s+в\s+отеле)?", "T04", "own_parking"
)
_simple(r"(?:детск\w+\s+)?кроватк\w*", "F03", "cot_available")
_simple(r"смежн\w+\s+номер\w*", "F04", "connecting_rooms")
_simple(r"детск\w+\s+бассейн\w*", "F05", "kids_pool")
_simple(
    r"(?:своя\s+|приватн\w+\s+)?кухн\w*\s+в\s+номере|кухонн\w+\s+зон\w*",
    "M03",
    "private_kitchenette",
)
_simple(r"\bчайник\w*", "M04", "in_room_kettle")
_simple(r"\bсейф\w*", "M05", "has_safe")
_simple(r"душ\w*\s+без\s+(?:бортик|порог)\w*", "A04", "step_free_shower")


@rule
def lift(ctx: Ctx) -> Iterator[Hit]:
    for m in finditer(r"лифт\w*", ctx):
        if re.search(r"не\s+пользу\w*\s*$", ctx.norm[max(0, m.start() - 20) : m.start()]):
            continue  # «не пользуется лифтом» — не требование лифта
        yield Hit(m.start(), m.end(), [Atom("A03", "has_lift", "eq", "true")], "required")


@rule
def step_free(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:вход\w*\s+(?:с\s+улицы\s+)?|от\s+улицы\s+до\s+номера\s+(?:\w+\s+)?)?"
        r"без\s+ступен\w*|пандус\w*"
    )
    for m in finditer(pattern, ctx):
        # Путь до номера целиком строже, чем вход в вестибюль (A02): соответствие частичное.
        mapping = "partial" if "до номера" in m.group() else "exact"
        yield Hit(
            m.start(),
            m.end(),
            [Atom("A02", "step_free", "eq", "true", mapping=mapping)],
            "required",
        )


@rule
def pets(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        rf"(?:с\s+)?(?P<sp>собак\w*|\bпес\b|\bпса\b|кошк\w*|\bкот(?:ом|а)?\b|питом\w*|животн\w*)"
        rf"(?:[\s,]+(?:\w+\s+){{0,2}}?(?P<kg>{NUM})\s*(?:кг|килограмм\w*))?"
    )
    for m in finditer(pattern, ctx):
        atoms = [Atom("P01", "pets_allowed", "eq", "true")]
        if m.group("kg"):
            atoms.append(Atom("P01", "max_weight_kg", "gte", number(m.group("kg")), "kg"))
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
        atoms = [Atom("T05", "has_charger", "eq", "true")]
        if re.search(r"type\s*2|тайп\s*2", m.group()):
            atoms.append(Atom("T05", "connector", "eq", "type2"))
        yield Hit(m.start(), m.end(), atoms, "required")


@rule
def checkin(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        r"(?:заселени\w*|заселиться|заезд\w*|приняли|принимали|заселят)\s+(?:\w+\s+){0,2}?"
        r"(?:ночью|после\s+полуночи|поздно|поздним\s+вечером)|ночн\w+\s+заселени\w*|поздн\w+\s+заезд\w*"
    )
    for m in finditer(pattern, ctx):
        # Политика заселения — ответ отельера общего вида, без параметров момента.
        yield Hit(m.start(), m.end(), [Atom("L04", "checkin_possible", "eq", "true")], "required")


@rule
def wifi(ctx: Ctx) -> Iterator[Hit]:
    pattern = (
        rf"(?:(?:проверенн\w+|быстр\w+|хорош\w+)\s+)?(?:wi-?fi|вай-?фай|интернет\w*)"
        rf"(?:\s+[\w-]+){{0,4}}?\s+(?P<n>{NUM})\s*(?:мегабит\w*|мбит\w*|mbps)"
    )
    for m in finditer(pattern, ctx):
        upload = re.search(r"отдач|upload|выгрузк", m.group())
        field_ = "upload_mbps" if upload else "download_mbps"
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
        r"|вдвоем|на\s+машине"
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
    sentence_start = max(ctx.norm.rfind(c, 0, pos) for c in ".;!?") + 1
    sub_start = max(ctx.norm.rfind(c, sentence_start, pos) for c in ",:—–()") + 1
    sub_start = max(sub_start, ctx.norm.rfind(" а ", sentence_start, pos) + 1, sentence_start)
    ends = [i for c in ".;!?,:—–()" if (i := ctx.norm.find(c, pos)) >= 0]
    if (i := ctx.norm.find(" а ", pos)) >= 0:
        ends.append(i)
    sub = ctx.norm[sub_start : min(ends) if ends else len(ctx.norm)]
    req, des = REQ_MARKERS.search(sub), DES_MARKERS.search(sub)
    if req and not des:
        return "required"
    if des and not req:
        return "desired"
    sent = ctx.sentence(pos)
    req, des = REQ_MARKERS.search(sent), DES_MARKERS.search(sent)
    if req and not des:
        return "required"
    if des and not req:
        return "desired"
    return default


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
    kept.sort(key=lambda h: h.start)

    atoms: list[CompactAtom] = []
    spans: dict[str, tuple[int, int]] = {}
    clarify: list[CompactClarify] = []
    pets: list[str] = []
    for h in kept:
        req = requiredness(ctx, h.start, h.default)
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
            if a.feature_id == "P01" and a.field == "pets_allowed":
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
        or_groups=[],
        negated_atoms=[],
        clarify=clarify,
    )
    spans |= {f"ctx{i}": span for i, span in enumerate(ctx.context)}
    return compact, spans
