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
    иметь ещё бы чтобы чтоб
    """.split()
)
_WORD = re.compile(r"[\w-]+", re.UNICODE)


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
        if inside:
            flush()
        elif word in STOPWORDS or word.isdigit():
            continue
        else:
            current.append(m)
    flush()
    return gaps
