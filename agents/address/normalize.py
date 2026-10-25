"""Адрес → структура для сравнения (задача 3б, D33): адрес отеля с Островка и адрес здания OSM.

Адреса отелей приходят как попало: латиница и кириллица вперемешку, «str.1», «113к5», «Bld 1»,
индекс, страна, район, помещение. Правила разбирают адрес на тип улицы, название, дом, корпус,
строение и литеру. Название сравнивается по «скелету» латиницей: «Дорогомиловская» и
«Dorogomilovskaya», «Академика Анохина» и «Akademika Anohina» дают одно и то же.

Что правила не разобрали, может разобрать модель (`llm.py`), но её ответ проходит проверку:
каждое значение должно найтись в исходной строке (D8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Тип улицы: как пишут → канонический тип. Сравниваем без точки на конце. «Вал» — не тип:
# в Москве это часть названия («улица Земляной Вал», «Трёхгорный Вал»).
STREET_TYPES: dict[str, str] = {
    **dict.fromkeys(["улица", "ул", "ulitsa", "ulica", "ul", "street", "st", "str"], "улица"),
    **dict.fromkeys(
        ["проспект", "пр-кт", "просп", "пр-т", "prospekt", "prospect", "propspekt", "avenue"],
        "проспект",
    ),
    **dict.fromkeys(["переулок", "пер", "pereulok", "per", "lane"], "переулок"),
    **dict.fromkeys(["шоссе", "ш", "shosse", "highway"], "шоссе"),
    **dict.fromkeys(["бульвар", "б-р", "бул", "bulvar", "boulevard", "blvd"], "бульвар"),
    **dict.fromkeys(["проезд", "пр-д", "proezd", "drive", "dr"], "проезд"),
    **dict.fromkeys(["площадь", "пл", "ploshchad", "square", "sq"], "площадь"),
    **dict.fromkeys(["набережная", "наб", "naberezhnaya", "embankment", "emb"], "набережная"),
    **dict.fromkeys(["тупик", "tupik"], "тупик"),
    **dict.fromkeys(["аллея", "alleya"], "аллея"),
    **dict.fromkeys(["линия", "liniya"], "линия"),
    **dict.fromkeys(["квартал", "kvartal"], "квартал"),
    **dict.fromkeys(["деревня", "дер", "village", "посёлок", "поселок", "пос"], "населённый пункт"),
}
# Части адреса, которые к зданию не относятся: страна, город, район, индекс.
_DROP_WORDS = re.compile(
    r"^(?:россия|russia|russian\s+federation|рф|г\.?\s*москва|москва|moscow|moskva|"
    r"г\.?|город|city|domodedovo|домодедово)$"
)
_DISTRICT = re.compile(
    r"вн\.?\s*тер\.?|муниципальн\w*\s+округ|\bр-н\b|район|district|поселение|\bмо\b|\bао\b"
)
_TAIL_NOISE = re.compile(  # помещение, квартира, этаж — внутри здания, не адрес здания
    r"\b(?:помещ\w*|пом|кв|квартира|apartment|apt|офис|оф|office|floor|этаж|room|комн\w*)\b.*$"
)
_POSTCODE = re.compile(r"\b(\d{6})\b")
_SPACE_AFTER_DOT = re.compile(
    r"\b(ул|пр|пер|ш|наб|пл|д|стр|корп|к|с|лит|вл|зд|str|bld|bldg)\.(?=\S)"
)
_ORDINAL = re.compile(r"^(\d{1,2})(?:-?(?:я|й|ая|ый|ий|ой|ья|е|ya|y|yy|iy|aya|st|nd|rd|th))$")
_LATIN_LETTER = str.maketrans("abvgde", "абвгде")

# Компоненты дома: номер с дробью и литерой, корпус, строение, литера, владение. После метки
# («к», «с», «стр») сразу может идти цифра: «29 с3», «113к5».
_MARKS = r"д|дом|house|nr|no|№|зд|здание|вл|владение|building|bld|bldg"
_HOUSE_TOKEN = re.compile(
    rf"(?P<mark>\b(?:{_MARKS})\b\.?\s*)?"
    r"(?<![\w-])(?P<num>\d{1,4}(?:/\d{1,4}|-\d{1,4}(?![\w-]))?)"  # «20/1», «21-23»
    r"(?:\s?(?P<let>[а-яa-e])(?![\w])(?!\.?\s*\d))?"  # «19/21 к.2»: «к» — корпус, не литера
    r"(?:(?P<k_att>к|k)(?P<k_num>\d{1,3})|(?P<s_att>с|стр)(?P<s_num>\d{1,3}))?"
    r"(?![\w-])"
)
_K = r"(?:к|корп|корпус|k|korp|korpus)(?![а-яa-z])\.?\s*\d{1,3}"
_S = r"(?:с|стр|строение|str|stroenie|bld|bldg|building)(?![а-яa-z])\.?\s*(?:\d{1,3}|[а-я](?!\w))"
_L = r"(?:лит|литера|литер|lit)(?![а-яa-z])\.?\s*[а-яa-e](?!\w)"
_CORPUS = re.compile(r"\b(?:к|корп|корпус|k|korp|korpus)(?![а-яa-z])\.?\s*(?P<v>\d{1,3})\b")
_STRUCTURE = re.compile(
    r"\b(?:с|стр|строение|str|stroenie|bld|bldg|building)(?![а-яa-z])\.?\s*"
    r"(?P<v>\d{1,3}|[а-я](?!\w))"
)
_LETTER = re.compile(r"\b(?:лит|литера|литер|lit)(?![а-яa-z])\.?\s*(?P<v>[а-яa-e])\b")
# Хвост после номера дома — только корпус, строение, литера: иначе число — часть названия
# («улица 8 Марта, 13», «26 Бакинских Комиссаров»).
_HOUSE_TAIL = re.compile(rf"(?:[\s,.]*(?:{_K}|{_S}|{_L}))*[\s,.]*")

_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
        "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
        "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
        "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
        "'": "", "’": "", "`": "", "ʼ": "",
    }
)  # fmt: skip
_SKELETON = [("shch", "sh"), ("sch", "sh"), ("kh", "h"), ("ts", "c"), ("tz", "c"), ("x", "ks")]


def skeleton(word: str) -> str:
    """Слово латиницей без различий транслитерации: й / y / i / j, х / kh / h, ц / ts / c."""
    s = word.lower().replace("ё", "е").translate(_TRANSLIT)
    for a, b in _SKELETON:
        s = s.replace(a, b)
    s = s.replace("y", "i").replace("j", "i")
    return re.sub(r"([a-z])\1+", r"\1", s)  # «Basmannaya» = «Басманная»; цифры не трогаем


@dataclass(frozen=True)
class Address:
    raw: str
    street_type: str | None = None  # канонический тип: «улица», «проспект», «переулок»…
    street: str | None = None  # название как в адресе, без типа: «малая дмитровка»
    ordinal: str | None = None  # «1-я Тверская-Ямская» → «1»
    house: str | None = None  # «29», «20/1», «119а»
    corpus: str | None = None
    structure: str | None = None
    possession: bool = False  # «вл.» — владение, а не дом
    postcode: str | None = None
    dropped: tuple[str, ...] = field(default=())  # что отброшено: помещение, район, «formerly…»

    @property
    def street_key(self) -> str | None:
        if not self.street:
            return None
        words = sorted(skeleton(w) for w in re.split(r"\s+", self.street) if w)
        return " ".join(([self.ordinal] if self.ordinal else []) + words) or None

    @property
    def house_key(self) -> str | None:
        if not self.house:
            return None
        key = self.house
        if self.corpus:
            key += f"к{self.corpus}"
        if self.structure:
            key += f"с{self.structure}"
        return key

    @property
    def complete(self) -> bool:
        return bool(self.street_key and self.house)

    def text(self) -> str:
        """Нормальная запись для человека: «улица Малая Дмитровка, 29 с3»."""
        street = " ".join(
            p for p in (self.street_type, f"{self.ordinal}-я" if self.ordinal else None,
                        self.street) if p
        )  # fmt: skip
        house = self.house_key or ""
        return ", ".join(p for p in (street, house) if p)


def _clean(raw: str) -> tuple[str, list[str]]:
    dropped: list[str] = []
    s = raw.lower().replace("ё", "е").replace(";", ",")
    for m in re.finditer(r"\([^)]*\)", s):
        dropped.append(m.group().strip("()"))
    s = re.sub(r"\([^)]*\)", " ", s)
    s = _SPACE_AFTER_DOT.sub(r"\1. ", s)
    return s, dropped


def _parts(s: str, dropped: list[str]) -> tuple[list[str], str | None]:
    postcode = None
    kept = []
    for part in s.split(","):
        part = part.strip()
        if m := _POSTCODE.search(part):
            postcode = postcode or m.group(1)
            part = _POSTCODE.sub(" ", part).strip()
        if m := _TAIL_NOISE.search(part):
            dropped.append(m.group().strip())
            part = part[: m.start()].strip()
        if d := _DISTRICT.search(part):
            head = part[: d.start()].rstrip()
            if not re.search(r"\d", head):
                dropped.append(part)  # «р-н Ново-Переделкино», «вн.тер.г. … Тверской»
                continue
            # «Mokhovaya 44 Tsentralny District …» — район в конце части: улицу и дом оставляем
            street, _, district = head.rpartition(" ")
            head = street if street and not re.search(r"\d", district) else head
            dropped.append(part[len(head) :].strip())
            part = head
        words = [w for w in re.split(r"\s+", part) if w]
        if not words or all(_DROP_WORDS.match(w) for w in words) or _DROP_WORDS.match(part):
            continue
        # «Moscow 109316 Russia», «Moskva, Russia» внутри части — только хвост из городов и стран
        while words and _DROP_WORDS.match(words[-1]):
            words.pop()
        kept.append(" ".join(words))
    return [p for p in kept if p], postcode


def _street(text: str) -> tuple[str | None, str | None, str | None]:
    words = [w.strip(".") for w in re.split(r"\s+", text) if w.strip(".")]
    street_type = None
    ordinal = None
    name = []
    for w in words:
        if street_type is None and w in STREET_TYPES:
            street_type = STREET_TYPES[w]
        elif ordinal is None and (m := _ORDINAL.match(w)):
            ordinal = m.group(1)
        elif not _DROP_WORDS.match(w):
            name.append(w)
    return street_type, " ".join(name) or None, ordinal


def parse_address(raw: str) -> Address:
    """Разбор правилами. Не понял дом или улицу — соответствующие поля пустые, без догадок."""
    s, dropped = _clean(raw)
    parts, postcode = _parts(s, dropped)
    # Дом — первое отдельное число (не «1-я», не «800-летия»), после которого в его части только
    # корпус, строение, литера: в «улица 65 лет Победы, д. 67» дом — 67. Улица — текст перед домом
    # в той же части, а если дом стоит отдельной частью («Ulitsa Gasheka, 9») — предыдущая часть.
    found = [(i, h) for i, part in enumerate(parts) for h in _HOUSE_TOKEN.finditer(part)]
    good = [(i, h) for i, h in found if _HOUSE_TAIL.fullmatch(parts[i][h.end() :])]
    for i, m in (good or found)[:1]:
        part = parts[i]
        before = part[: m.start()].strip(" .")
        before = re.sub(rf"\b(?:{_MARKS})\.?$", "", before)
        street_text = before.strip(" .") or (parts[i - 1] if i else "")
        if not street_text and i + 1 < len(parts) and not _HOUSE_TOKEN.search(parts[i + 1]):
            street_text = parts[i + 1]  # «вл. 5, Ленинградское шоссе» — улица после дома
        tail = " ".join([part[m.start() :], *parts[i + 1 :]])
        return _with_house(raw, street_text, m, tail, postcode, dropped)
    street_type, street, ordinal = _street(parts[0]) if parts else (None, None, None)
    return Address(
        raw=raw,
        street_type=street_type,
        street=street,
        ordinal=ordinal,
        postcode=postcode,
        dropped=tuple(dropped),
    )


def _with_house(
    raw: str, street_text: str, m: re.Match[str], tail: str, postcode: str | None, dropped: list
) -> Address:
    street_type, street, ordinal = _street(street_text)
    house = m.group("num")
    if m.group("let"):
        house += m.group("let").translate(_LATIN_LETTER)
    corpus = m.group("k_num")
    structure = m.group("s_num")
    rest = tail[m.end() - m.start() :]
    if corpus is None and (c := _CORPUS.search(rest)):
        corpus = c.group("v")
    if structure is None and (st := _STRUCTURE.search(rest)):
        structure = st.group("v")
    if (lt := _LETTER.search(rest)) and not m.group("let"):
        house += lt.group("v").translate(_LATIN_LETTER)
    mark = (m.group("mark") or "").strip(" .")
    return Address(
        raw=raw,
        street_type=street_type,
        street=street,
        ordinal=ordinal,
        house=house,
        corpus=corpus,
        structure=structure,
        possession=mark in ("вл", "владение"),
        postcode=postcode,
        dropped=tuple(dropped),
    )


def osm_addresses(tags: dict[str, str]) -> list[Address]:
    """Адреса здания OSM: `addr:*` и второй адрес угловых домов `addr2:*`."""
    out = []
    for prefix in ("addr", "addr2"):
        street = tags.get(f"{prefix}:street") or tags.get(f"{prefix}:place")
        house = tags.get(f"{prefix}:housenumber")
        if street and house:
            out.append(parse_address(f"{street}, {house}"))
    return out
