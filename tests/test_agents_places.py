from datetime import date

import pytest

from agents.catalog import load_catalog, load_places, matches
from agents.search import parse_query
from agents.search.compact import CompactAST, CompactAtom, Param, to_full_ast
from agents.search.text import normalize

TODAY = date(2026, 10, 25)
PLACES = load_places()


def parse(text):
    return parse_query(text, today=TODAY, mode="rules").ast


def places_of(ast):
    return [o.place for o in ast.out_of_catalog if o.place]


def test_dictionary_is_consistent():
    assert PLACES.near_m == 800 and len(PLACES.categories) >= 40
    for c in PLACES.categories.values():
        assert c.label and c.patterns and c.osm, c.id
        assert all(alt for alt in c.osm), c.id
    assert {f["category"] for f in PLACES.osm_filters()} == set(PLACES.categories)
    # То, что уже есть в каталоге характеристик, словарь мест не перехватывает.
    for words in ("аптека", "детская площадка", "супермаркет", "ветклиника", "коворкинг", "бар"):
        assert PLACES.find(normalize(words)) == [], words


def test_osm_tags_match():
    italian, pool, theme = (PLACES.get(c) for c in ("italian", "pool", "theme_park"))
    assert matches({"amenity": "restaurant", "cuisine": "italian;pizza"}, italian)
    assert matches({"amenity": "fast_food", "cuisine": "pizza"}, italian)
    assert not matches({"amenity": "restaurant", "cuisine": "georgian"}, italian)
    assert not matches({"cuisine": "italian"}, italian)  # без amenity — не заведение
    assert matches({"leisure": "swimming_pool"}, pool)
    assert not matches({"leisure": "swimming_pool", "access": "private"}, pool)
    assert matches({"attraction": "roller_coaster"}, theme)  # «*» — любое значение


@pytest.mark.parametrize(
    ("text", "category", "radius", "source", "on_site"),
    [
        ("Хорошо бы рядом итальянский ресторан", "italian", 800, "policy", False),
        ("чтобы в 5 минутах пешком была кофейня", "coffee", 400, "guest", False),
        ("японский ресторан не дальше 300 м от отеля", "japanese", 300, "guest", False),
        ("музей в десяти минутах", "museum", 800, "guest", False),
        ("бассейн рядом с отелем", "pool", 800, "policy", False),
        ("отель с бассейном", "pool", None, None, True),
        ("ресторан в отеле", "restaurant", None, None, True),
        ("хочу грузинскую кухню", "georgian", 800, "policy", False),
        ("отель рядом с парком", "park", 800, "policy", False),
        ("отель у сквера", "park", 800, "policy", False),
        ("парк в 15 минутах", "park", 1200, "guest", False),
        ("с парком поблизости", "park", 800, "policy", False),  # рядом, а не в самом отеле
        ("отель с садом", "park", None, None, True),
    ],
)
def test_nearby_place_rules(text, category, radius, source, on_site):
    (place,) = places_of(parse(text))
    assert (place.category, place.radius_m, place.radius_source, place.on_site) == (
        category,
        radius,
        source,
        on_site,
    )


def test_park_is_a_place_and_greenery_stays_e03():
    """«Парк рядом» — место словаря (`park`, считается по паркам пакета), а доля зелени вокруг —
    E03. «Детский сад», «вид на парк», «парк-отель», аквапарк и парк аттракционов — не парк."""
    ast = parse("зелёный район и парк рядом")
    assert [c.feature_id for c in ast.soft] == ["E03"]
    assert [p.category for p in places_of(ast)] == ["park"]
    assert [p.category for p in places_of(parse("зелёная зона рядом"))] == ["park"]
    for text in (
        "детский сад рядом",
        "вид на парк",
        "парк-отель",
        "рядом аквапарк",
        "рядом парк аттракционов",
        "парковка у отеля",
    ):
        assert "park" not in [p.category for p in places_of(parse(text))], text
    park = PLACES.get("park")
    assert matches({"leisure": "park"}, park)
    assert not matches({"leisure": "park", "access": "private"}, park)
    assert not matches({"leisure": "garden"}, park)  # сад без названия — чаще двор или участок
    assert matches({"leisure": "garden", "name": "Аптекарский огород"}, park)
    assert not matches({"leisure": "garden", "name": "Сад", "garden:type": "residential"}, park)


def test_listed_places_share_near():
    """«Рядом с парками и хорошими ресторанами» — «рядом» и у второго места перечисления."""
    ast = parse("у моря рядом с парками и хорошими ресторанами")
    assert [c.feature_id for c in ast.soft] == ["W01"] and not ast.unparsed_spans
    park, restaurant = places_of(ast)
    assert (park.category, park.radius_m) == ("park", 800)
    assert (restaurant.category, restaurant.radius_m) == ("restaurant", 800)
    assert restaurant.quality == "хорошими" and not restaurant.on_site
    # Без «рядом» у первого «ресторан» — про сам отель: перечисление его не берёт.
    assert [p.category for p in places_of(parse("парк и ресторан"))] == ["park"]


def test_place_words_do_not_steal_from_sea_and_catalog():
    ast = parse("музей в пяти минутах от моря")
    assert [(c.feature_id, c.value) for c in ast.hard] == [("W01", 400)]  # 5 минут — до моря
    assert places_of(ast)[0].radius_source == "policy"
    ast = parse("недалеко от моря и рядом пекарня")
    assert [c.feature_id for c in ast.soft] == ["W01"] and places_of(ast)[0].category == "bakery"
    assert [c.feature_id for c in parse("детская площадка рядом").soft] == ["F01"]
    assert places_of(parse("смотровая площадка рядом"))[0].category == "viewpoint"


def test_hotel_amenity_word_without_near_is_left_to_model():
    """«Бассейн» без «рядом» и без «с» — может быть и про сам отель: правило не угадывает."""
    assert places_of(parse("бассейн")) == []


def _compact(value, params, text):
    atom = CompactAtom(
        atom_id="a1",
        text=text,
        requiredness="desired",
        provenance="explicit",
        feature_id="",
        field="",
        op="",
        value=value,
        unit="",
        params=[Param(key=k, value=v) for k, v in params.items()],
        mapping="unmatched",
        importance="medium",
    )
    return CompactAST(
        city="",
        date_from="",
        date_to="",
        adults="",
        children_ages=[],
        pets=[],
        context_text=[],
        atoms=[atom],
        or_groups=[],
        negated_atoms=[],
        clarify=[],
    )


def test_model_path_gets_place_from_category_and_distance():
    """Модель даёт ID категории и расстояние словами гостя; метры считает код (D8)."""
    text = "суши в 5 минутах"
    params = {"nearest_contract": "nearby_place", "distance": "5", "distance_unit": "минут"}
    ast = to_full_ast(_compact("japanese", params, text), text, load_catalog(), parsed_by="llm")
    (o,) = ast.out_of_catalog
    assert o.normalized == "nearby_place:japanese"
    assert (o.place.radius_m, o.place.radius_source) == (400, "guest")
    # Категории нет в словаре — часть остаётся вне каталога как есть, без догадки.
    text = "бар с живой музыкой"
    ast = to_full_ast(
        _compact("live_music_bar", {"nearest_contract": "nearby_place"}, text),
        text,
        load_catalog(),
        parsed_by="llm",
    )
    (o,) = ast.out_of_catalog
    assert o.place is None and o.normalized == "live_music_bar"
