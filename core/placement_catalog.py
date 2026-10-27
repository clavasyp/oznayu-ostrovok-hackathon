"""Лёгкий адаптер справочника корпусов к агенту размещений D17 (без геобиблиотек)."""

import re
from collections import defaultdict

# Названия корпусов со слов отеля (ответы о размещении): новая версия пакета их не стирает.
HOTEL_ALIAS_STATUSES = {"hotelier", "simulated_hotelier"}
# D41-3: стена стороны — отрезок контура от 3 м; короче — излом контура, а не стена с окнами.
MIN_SEGMENT_M = 3.0
COURTYARD = "courtyard"
_BUILDING_STEMS = {"корпус", "здан", "блок", "башн", "строен", "флигел", "вилл", "коттедж"}
_ORDINALS = {
    "перв": "1", "втор": "2", "трет": "3", "четверт": "4", "пят": "5",
    "шест": "6", "седьм": "7", "восьм": "8", "девят": "9", "десят": "10",
}  # fmt: skip
_ENDINGS = sorted(
    "ого его ому ему ыми ими ий ый ой ая яя ое ее ые ие ую юю ом ем ым им ах ях ам ям ии ия ию "
    "ей е а у ы и я ю".split(),
    key=len,
    reverse=True,
)


def _stem(word: str) -> str:
    if word.isdigit() or len(word) <= 3:
        return word
    for end in _ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 3:
            word = word[: -len(end)]
            break
    return _ORDINALS.get(word, word)


def name_words(text: str) -> list[tuple[int, int, str]]:
    """Слова текста с позициями и основой: «во 2-м корпусе» → (0, 2, «во»), (3, 4, «2»),
    (7, 14, «корпус»). Порядковые — числом, «корп» — «корпус»; позиции — как в `text`."""
    norm = text.casefold().replace("ё", "е")
    words = []
    for m in re.finditer(r"[^\W_]+", norm):
        word = m.group()
        if re.fullmatch(r"\d+(?:й|ый|ий|ой|м|ом|го|ого)", word):
            word = re.match(r"\d+", word).group()
        elif (
            words
            and words[-1][2].isdigit()
            and word in {"й", "ый", "ий", "ой", "м", "ом", "го", "ого"}
        ):
            continue  # «2-й»: суффикс порядкового после дефиса
        words.append((m.start(), m.end(), "корпус" if word == "корп" else _stem(word)))
    return words


def name_key(name: str) -> str:
    """Ключ названия корпуса: без регистра, знаков и окончаний. «Корпус №2,», «в корпусе 2»,
    «корп. 2», «2-й корпус» и «второй корпус» дают «корпус 2». Пустой ключ — названия нет."""
    stems = [stem for _, _, stem in name_words(name)]
    if len(stems) == 2 and stems[0].isdigit() and stems[1] in _BUILDING_STEMS:
        stems.reverse()  # «2-й корпус» → «корпус 2»
    return " ".join(stems)


def _alias_key(alias: dict) -> tuple[str, str | None]:
    return name_key(alias["name"]), alias.get("status")


def side_segments(facades: list[dict], building_id: str) -> dict[str, list[str]]:
    """Сторона (8 румбов и `courtyard`) → отрезки корпуса от 3 м (D41-3).

    Номер «окнами на сторону» — на одной из этих стен. Двор — только отрезки с
    `is_courtyard is True`: `null` двор не исключает, но и не доказывает. Длина неизвестна —
    отрезок не берём.
    """
    out: dict[str, list[str]] = defaultdict(list)
    for f in facades:
        length = f.get("length_m")
        if f["building_id"] != building_id or length is None or length < MIN_SEGMENT_M:
            continue
        out[f["side"]].append(f["facade_id"])
        if f.get("is_courtyard") is True:
            out[COURTYARD].append(f["facade_id"])
    return {side: sorted(ids) for side, ids in out.items()}


def hotel_aliases(evidence: dict | None) -> list[dict]:
    """Названия корпуса, подтверждённые отелем или имитатором, из `hotel_building.evidence`."""
    return [
        a
        for a in (evidence or {}).get("hotel_aliases", [])
        if a.get("status") in HOTEL_ALIAS_STATUSES
    ]


def with_alias(evidence: dict | None, alias: dict) -> dict:
    """`evidence` связи «отель — корпус» с ещё одним названием; то же название с тем же статусом
    не дублируется (первое подтверждение остаётся со своей цитатой и датой)."""
    evidence = dict(evidence or {})
    have = list(evidence.get("hotel_aliases", []))
    if all(_alias_key(a) != _alias_key(alias) for a in have):
        evidence["hotel_aliases"] = have + [alias]
    return evidence


def placement_maps(links: list[dict], facades: list[dict], hotel_id: str) -> tuple[dict, dict]:
    alias_candidates, sides = defaultdict(set), defaultdict(list)
    bids = {link["building_id"] for link in links if link["hotel_id"] == hotel_id}
    for bid in bids:
        alias_candidates[bid.casefold()].add(bid)
    for link in links:
        if link["hotel_id"] != hotel_id:
            continue
        for alias in link.get("evidence", {}).get("hotel_aliases", []):
            if alias.get("status") in {"hotelier", "curated", "ostrovok_content"} and alias.get(
                "source"
            ):
                key = alias["name"].strip().casefold().replace("ё", "е")
                if name_key(key):  # пустое название или одни знаки — не название
                    alias_candidates[key].add(link["building_id"])
    for facade in facades:
        if facade["building_id"] not in bids:
            continue
        sides[(facade["building_id"], facade["side"])].append(facade["facade_id"])
        if facade["is_courtyard"] is True:
            sides[(facade["building_id"], "courtyard")].append(facade["facade_id"])
    aliases = {a: next(iter(b)) for a, b in alias_candidates.items() if len(b) == 1}
    directions = {key: ids[0] for key, ids in sides.items() if len(ids) == 1}
    return aliases, directions


def placement_choices(
    links: list[dict], buildings: list[dict], facades: list[dict], hotel_id: str
) -> list[dict]:
    """Корпуса для уточняющего вопроса: OSM-адрес/этажность и все неоднозначные стороны.

    Название из OSM не становится названием корпуса со слов отеля. Этажность OSM
    не доказывает этажи категории. Вариант выбирается по устойчивому building_id.
    """
    aliases, directions = placement_maps(links, facades, hotel_id)
    bids = {link["building_id"] for link in links if link["hotel_id"] == hotel_id}
    by_id = {building["id"]: building for building in buildings}
    result = []
    for bid in sorted(bids):
        tags = by_id.get(bid, {}).get("tags") or {}
        address = (
            tags.get("addr:full")
            or ", ".join(str(tags[k]) for k in ("addr:street", "addr:housenumber") if tags.get(k))
            or None
        )
        levels = tags.get("building:levels")
        levels = (
            int(levels)
            if isinstance(levels, str)
            and levels.isascii()
            and levels.isdigit()
            and 0 < int(levels) <= 200
            else None
        )
        sides = defaultdict(set)
        for facade in facades:
            if facade["building_id"] == bid:
                sides[facade["side"]].add(facade["facade_id"])
                if facade["is_courtyard"] is True:
                    sides["courtyard"].add(facade["facade_id"])
        result.append(
            {
                "building_id": bid,
                "mapped_address": address,
                "mapped_name": tags.get("name"),
                "mapped_levels": levels,
                "hotel_aliases": sorted(
                    alias
                    for alias, target in aliases.items()
                    if target == bid and alias != bid.casefold()
                ),
                "unambiguous_facades": [
                    {"side": side, "facade_id": fid}
                    for (building, side), fid in sorted(directions.items())
                    if building == bid
                ],
                # Несколько стен на сторону: номер «окнами на сторону» — на одной из них (D41-3),
                # без уточнения у отеля; в размещения идут стены от 3 м (`side_segments`).
                "ambiguous_sides": [
                    {
                        "side": side,
                        "facade_ids": sorted(ids),
                        "reason": "room_on_one_of_facades",
                    }
                    for side, ids in sorted(sides.items())
                    if len(ids) > 1
                ],
                "category_floors_verified": False,
            }
        )
    return result
