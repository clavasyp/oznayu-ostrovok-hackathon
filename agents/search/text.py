"""Фрагменты запроса: поиск позиций и неразобранные куски.

Позиции — в Unicode code points (индексы строки Python), полуинтервал `[start, end)` (v11).
Позиции находит сервер, а не модель (v4b).
"""

from __future__ import annotations

import re

from agents.query_ast import Span

# Служебные и общие слова запроса: сами по себе условий не несут,
# поэтому неразобранными не считаются.
STOPWORDS = frozenset(
    """
    и а но или в во на с со к ко у о об от до по за из для при без про под над же ли не ни бы
    я мы нам мне меня нас мой моя моё наш наша наше вы вам вас
    хочу хотим хочется хотелось хотела хотел хотели нужен нужна нужно нужны надо
    был была было были быть будет будут есть чтобы чтоб что это этот эта эти так тоже ещё еще
    очень всё все всего сам сама само самом самый именно прямо тот та то как когда если можно
    там тут здесь номер номера номере номеров отель отеля отеле отелей гостиница гостиницы
    пожалуйста обязательно обязателен обязательна обязательны желательно лучше только плюсом
    плюс бонус хорошо здорово приятно удобно неплохо хоть хотя точно строго должен должна
    должно должны едем едём поедем приедем приезжаем ищу ищем подберите найдите подбери найди
    вариант варианты который которая которое которые всё-таки все-таки нормальный нормальная
    нормальное нормальные большим большой был бы рассматриваю рассматриваем тоже же уж
    приеду приедет приезжаю поехать поеду поехали какая какой какое какие какую где
    иметь ещё бы чтобы чтоб её ее его их него нее неё них принять приняли примут пустят быстро
    спать сплю сон сна днем днём ночью
    желанию необязательно необязателен необязательна необязательны
    """.split()
)
# Усилители: «подтверждённый», «проверенный» — система и так требует подтверждения.
_STOP_STEMS = re.compile(r"^(?:подтвержд|проверенн|гарантированн)\w*$")
_BREAK = re.compile(r"[.,;:!?—–()]")
_WORD = re.compile(r"[\w-]+", re.UNICODE)

# Обязательность — по словам гостя: «обязательно», «только» / «хотелось бы», «лучше».
REQ_MARKERS = re.compile(
    r"\b(обязател\w*|только|нуж(?:ен|н\w*)|необходим\w*|должн\w*|точно|строго|непременно|"
    r"не рассматрива\w*|важно)\b"
)
DES_MARKERS = re.compile(
    r"\b(хотелось бы|хочется|желательно|лучше|был[аио]? бы|плюс\w*|бонус\w*|приятн\w*|"
    r"хорошо бы|по возможности|по желанию|неплохо|удобн\w*|здорово|предпочтительн\w*|нравятся|"
    r"нравится)\b"
)
# «Подальше от дороги», «этаж повыше» — слова самого условия, а не смягчение: одни они дают
# желательное, но «обязательно подальше от дороги» — обязательное (D38).
COMPARATIVES = re.compile(r"\b(поменьше|побольше|поближе|подальше|повыше|пониже)\b")
# «Не обязательно», «не обязателен», «необязательно», «не важно» — это мягкое пожелание, а не
# обязательное условие.
NOT_REQUIRED = re.compile(r"\bне\s*(?:обязател|нуж(?:ен|н)|важ(?:ен|н)|принципиал)\w*")
_SENTENCE_END = ".;!?"
_CLAUSE_END = ".;!?,:—–()"


def _markers(text: str) -> str | None:
    relaxed = bool(NOT_REQUIRED.search(text))
    text = NOT_REQUIRED.sub(" ", text)
    req, des = bool(REQ_MARKERS.search(text)), bool(DES_MARKERS.search(text)) or relaxed
    if req and not des:
        return "required"
    if (des or COMPARATIVES.search(text)) and not req:
        return "desired"
    return None


def clause_bounds(norm: str, pos: int, ends: str = _CLAUSE_END) -> tuple[int, int]:
    """Границы части фразы вокруг позиции: по знакам препинания и по противительному «а»."""
    sentence_start = max(norm.rfind(c, 0, pos) for c in _SENTENCE_END) + 1
    start = max(norm.rfind(c, sentence_start, pos) for c in ends) + 1
    if ends == _CLAUSE_END:
        start = max(start, norm.rfind(" а ", sentence_start, pos) + 1)
    start = max(start, sentence_start)
    stops = [i for c in ends if (i := norm.find(c, pos)) >= 0]
    if ends == _CLAUSE_END and (i := norm.find(" а ", pos)) >= 0:
        stops.append(i)
    return start, min(stops) if stops else len(norm)


def marker_requiredness(norm: str, pos: int, *, sentence: bool = True) -> str | None:
    """Обязательность по словам гостя: сначала в своей части фразы, затем — во всём
    предложении (если `sentence`). Нет слов или они спорят — None."""
    start, end = clause_bounds(norm, pos)
    if found := _markers(norm[start:end]):
        return found
    s_start, s_end = clause_bounds(norm, pos, _SENTENCE_END)
    # «Обязательно: тихо, без ковролина» — слово перед двоеточием относится ко всему списку
    # после него, до конца предложения.
    colon = norm.rfind(":", s_start, pos)
    if colon >= 0 and (found := _markers(norm[s_start:colon])):
        return found
    # «Обязательно, чтобы не было …» — часть с «чтобы» продолжает слова перед запятой.
    if start > s_start and re.match(r"\s*чтоб", norm[start:end]):
        p_start, _ = clause_bounds(norm, start - 1)
        if found := _markers(norm[p_start : start - 1]):
            return found
    if not sentence:
        return None
    return _markers(norm[s_start:s_end])


def normalize(text: str) -> str:
    """Нижний регистр и «ё» → «е» без смены длины строки (позиции совпадают с исходной)."""
    out = []
    for ch in text:
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
    return "".join(out).replace("ё", "е")


def find_span(raw: str, fragment: str) -> Span | None:
    """Позиция фрагмента в запросе: точно → без регистра и «ё» → с любыми пробелами."""
    fragment = fragment.strip()
    if not fragment:
        return None
    start = raw.find(fragment)
    if start < 0:
        start = normalize(raw).find(normalize(fragment))
    if start >= 0:
        end = start + len(fragment)
        return Span(start=start, end=end, text=raw[start:end])
    words = _WORD.findall(normalize(fragment))
    if not words:
        return None
    m = re.search(r"\W+".join(map(re.escape, words)), normalize(raw))
    if not m:
        return None
    return Span(start=m.start(), end=m.end(), text=raw[m.start() : m.end()])


def covered_gaps(raw: str, covered: list[tuple[int, int]]) -> list[Span]:
    """Значимые слова вне всех разобранных фрагментов, склеенные в куски."""
    gaps: list[Span] = []
    current: list[re.Match] = []

    def flush() -> None:
        if current:
            s, e = current[0].start(), current[-1].end()
            gaps.append(Span(start=s, end=e, text=raw[s:e]))
            current.clear()

    for m in _WORD.finditer(raw):
        inside = any(s <= m.start() and m.end() <= e for s, e in covered)
        word = normalize(m.group())
        if current and _BREAK.search(raw[current[-1].end() : m.start()]):
            flush()  # кусок не переходит через знак препинания: у частей фразы своя обязательность
        if inside:
            flush()
        elif word in STOPWORDS or word.isdigit() or _STOP_STEMS.match(word):
            continue
        else:
            current.append(m)
    flush()
    return gaps
