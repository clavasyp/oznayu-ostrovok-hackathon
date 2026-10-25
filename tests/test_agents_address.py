import gzip
import json

import pytest

from agents.address.__main__ import main
from agents.address.llm import STREET_TYPE_VALUES, normalize
from agents.address.match import Building, BuildingIndex, compare, propose
from agents.address.normalize import STREET_TYPES, osm_addresses, parse_address, skeleton
from agents.llm import Gateway
from agents.llm.fake import FakeLLM


@pytest.mark.parametrize(
    ("raw", "street_type", "street_key", "house_key"),
    [  # настоящие адреса отелей Москвы из пакета
        ("Staraya Basmannaya ul., 13 str.1, Moskva, Russia, 105064, Москва", "улица",
         "basmanaia staraia", "13с1"),
        ("Ulitsa Bolshaya Dorogomilovskaya 4; Moscow, Moscow, Moscow, Москва", "улица",
         "bolshaia dorogomilovskaia", "4"),
        ("Ulitsa Energeticheskaya 20 Apartment 152,Ulitsa Energeticheskaya, Moscow, 111116",
         "улица", "energeticheskaia", "20"),
        ("ул. Малая Дмитровка, 29, стр. 3, Москва", "улица", "dmitrovka malaia", "29с3"),
        ("Волгоградский проспект, д. 113к5, Москва", "проспект", "volgogradski", "113к5"),
        ("пр-кт Ленинский, д 119А, Москва", "проспект", "leninski", "119а"),
        ("Каширское Шоссе,д. 39 стр. Б, Москва", "шоссе", "kashirskoe", "39сб"),
        ("1-ya Tverskaya-Yamskaya 36 bld. 1, Moscow, Russia, Moscow, 125047, Москва", None,
         "1 tverskaia-iamskaia", "36с1"),
        ("123001, Bolshaya Sadovaya street, building 3/1, 2nd floor Moscow, Москва", "улица",
         "bolshaia sadovaia", "3/1"),
        ("Volgogradskiy Propspekt 32k13, Moscow 109316 Russia, Москва", "проспект",
         "volgogradski", "32к13"),
        ("1st Graivoronovskiy Dr., 2A, Moscow 109518 Russia, Москва", "проезд",
         "1 graivoronovski", "2а"),
        ("ул. Земляной Вал, 52/16, стр. 3, Москва", "улица", "val zemlianoi", "52/16с3"),
        ("Trekhgorny Val 1, Moskva, Russia, 123022, Москва", None, "trehgorni val", "1"),
        ("вн.тер.г. муниципальный округ Тверской, ул Александра Невского, д. 27, помещ. 1/1",
         "улица", "aleksandra nevskogo", "27"),
        ("119019, Russia, Moscow, Bolshoy Karetny lane, 22, building 2, (formerly Starovagankovsky "
         "lane, 15), Москва", "переулок", "bolshoi karetni", "22с2"),
        ("Россия, г. Москва, ул. 800-летия Москвы, д.11, к.4, Москва", "улица",
         "800-letia moskvi", "11к4"),
        ("ulitsa Zelenodol'skaya, 12, Moscow, Russia, Москва", "улица", "zelenodolskaia", "12"),
        ("улица 65 лет Победы, д. 67, Сириус", "улица", "65 let pobedi", "67"),
        ("Загородный проспект 21-23, Санкт-Петербург", "проспект", "zagorodni", "21-23"),
    ],
)  # fmt: skip
def test_hotel_addresses(raw, street_type, street_key, house_key):
    a = parse_address(raw)
    assert (a.street_type, a.street_key, a.house_key) == (street_type, street_key, house_key)


def test_no_house_is_not_guessed():
    a = parse_address("Ул.Марксистская , Москва")
    assert a.street_type == "улица" and a.house is None and not a.complete
    assert parse_address("вл. 5, Ленинградское шоссе").possession


@pytest.mark.parametrize(
    ("latin", "cyrillic"),
    [
        ("Dorogomilovskaya", "Дорогомиловская"),
        ("Anohina", "Анохина"),
        ("Zelenodol'skaya", "Зеленодольская"),
        ("Kutuzovskiy", "Кутузовский"),
        ("Graivoronovskiy", "Грайвороновский"),
        ("Kartmazovskaja", "Картмазовская"),
        ("Trekhgorny", "Трёхгорный"),
    ],
)
def test_transliteration_skeleton(latin, cyrillic):
    assert skeleton(latin) == skeleton(cyrillic)


@pytest.mark.parametrize(
    ("house", "address", "exact"),
    [  # правила полного номера дома из v9 (qa/facades/test_address_gate_v92.py)
        ("1", "улица Бестужева, д.1 Б", False),
        ("1Б", "улица Бестужева, д.1 Б", True),
        ("2А", "улица Карла Маркса, д.2 А", True),
        ("19/21", "проспект Мира, зд.19/21, к.2", False),
        ("19/21 к2", "проспект Мира, зд.19/21, к.2", True),
        ("19/21 к.2", "проспект Мира, зд.19/21, корпус 2", True),
        ("19/21 к2", "проспект Мира, зд.19/21, корпус 3", False),
        ("19А", "улица Луначарского, д.19, лит.А", True),
        ("219/28", "ул.Ленина, д.219/28", True),
        ("219", "ул.Ленина, д.219/28", False),
    ],
)
def test_full_house_gate_from_v9(house, address, exact):
    hotel = parse_address(address)
    building = parse_address(f"{hotel.street_type} {hotel.street}, {house}")
    m = compare(hotel, building)
    assert (m == {"street": "same", "house": "exact"}) is exact


def test_osm_tags_and_second_address():
    tags = {
        "addr:street": "улица Земляной Вал",
        "addr:housenumber": "52/16 с3",
        "addr2:street": "Нижняя Сыромятническая улица",
        "addr2:housenumber": "1",
    }
    assert [a.house_key for a in osm_addresses(tags)] == ["52/16с3", "1"]
    assert osm_addresses({"building": "yes"}) == []


LAT, LON = 55.7700, 37.6100  # точка отеля


def square(id_, street, house, dlat=0.0, **tags):
    lat, lon, d = LAT + dlat, LON, 0.0002
    ring = [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d]]
    tags = {"building": "yes", "addr:street": street, "addr:housenumber": house, **tags}
    return Building.from_row({"id": id_, "geometry": {"type": "Polygon", "coordinates": [ring]},
                              "tags": tags})  # fmt: skip


def hotel(address):
    return {"id": "1", "city_id": "moscow", "address": address, "latitude": LAT, "longitude": LON}


def test_exact_address_near_point_is_high_confidence():
    index = BuildingIndex(
        [square("way/1", "улица Малая Дмитровка", "29 с3"),
         square("way/2", "улица Малая Дмитровка", "29 с2", dlat=0.0006),
         square("way/3", "Страстной бульвар", "4", dlat=-0.0006)]
    )  # fmt: skip
    item = propose(hotel("ул. Малая Дмитровка, 29, стр. 3, Москва"), index, v9_links=["way/1"])
    assert item["kind"] == "match_hotel_building" and item["subject_id"] == "1"
    assert item["proposal"]["building_id"] == "way/1"
    assert item["proposal"]["confidence"] == "high"
    building = next(e for e in item["evidence"] if e["kind"] == "building_address")
    assert building["distance_m"] == 0.0 and building["house_match"] == "exact"
    assert {"kind": "v9_link", "building_ids": ["way/1"]} in item["evidence"]
    # «29 с2» — тот же номер, другое строение: не кандидат при точном совпадении рядом
    assert "way/2" not in item["proposal"]["alternatives"]


def test_partial_ambiguous_far_and_none():
    index = BuildingIndex([square("way/1", "улица Покровка", "40")])
    item = propose(hotel("ул. Покровка, д. 40 стр. 2, Москва"), index)
    assert item["proposal"]["confidence"] == "medium"  # строение у здания не указано
    assert item["proposal"]["reason"] == "street_same+house_partial"

    two = BuildingIndex([square("way/1", "Смоленская улица", "10"),
                         square("way/2", "Смоленская площадь", "10", dlat=0.0005)])  # fmt: skip
    item = propose(hotel("Smolenskaya 10, Moscow Moscow, Москва"), two)
    assert item["proposal"]["confidence"] == "ambiguous"  # тип улицы не назван — две улицы
    assert item["proposal"]["alternatives"] == ["way/2"]

    typo = BuildingIndex([square("way/1", "Московская улица", "82")])
    item = propose(hotel("улица Московкая, 82, Москва"), typo)
    assert item["proposal"]["reason"] == "street_similar+house_exact"

    far = BuildingIndex([square("way/1", "улица Лесная", "20 с2", dlat=0.03)])  # ~3 км
    item = propose(hotel("Улица Лесная, д.20 стр.2, Москва"), far)
    assert item["proposal"]["confidence"] == "low" and "far" in item["proposal"]["reason"]

    item = propose(hotel("Ул.Марксистская , Москва"), index)
    assert item["proposal"]["building_id"] is None
    assert item["proposal"]["reason"] == "address_incomplete"
    nearest = next(e for e in item["evidence"] if e["kind"] == "nearest_buildings")
    assert nearest["items"][0]["building_id"] == "way/1"  # подсказка, не кандидат


def test_llm_only_for_incomplete_and_checked_against_raw():
    raw = "Mokhovaya street (near the Hermitage), house forty four"
    good = {"street_type": "улица", "street": "Моховая", "house": "", "corpus": "",
            "structure": "", "possession": "false"}  # fmt: skip
    addr, by, _ = normalize(raw, use_llm=True, gateway=Gateway([FakeLLM([good])]))
    assert by == "rules" and not addr.complete  # дома нет — модель не придумала, разбор правил

    raw = "Mokhovaya 44, Tsentralny"
    fake = FakeLLM([])
    addr, by, _ = normalize(raw, use_llm=True, gateway=Gateway([fake]))
    assert by == "rules" and addr.house_key == "44" and fake.calls == []  # правил хватило

    raw = "Здание на Моховой, сорок четыре"
    lie = {**good, "house": "44"}  # цифр «44» в строке нет — отклоняем
    addr, by, notes = normalize(raw, use_llm=True, gateway=Gateway([FakeLLM([lie])]))
    assert by == "rules" and any("цифр" in n for n in notes)

    raw = "Моховая, сорок четвёртый дом (44)"  # скобки правила отбрасывают — дома не видят
    ok = {**good, "house": "44"}
    addr, by, _ = normalize(raw, use_llm=True, gateway=Gateway([FakeLLM([ok])]))
    assert by == "llm" and addr.raw == raw
    assert (addr.street_type, addr.street_key, addr.house_key) == ("улица", "mohovaia", "44")
    assert STREET_TYPE_VALUES <= set(STREET_TYPES.values())


def test_cli_report_and_candidates(tmp_path, capsys):
    assert main(["--city", "moscow"]) == 0
    assert "у 49 из 50 отелей" in capsys.readouterr().out
    lat, lon = 55.7655, 37.6077  # Малая Дмитровка, 29 с3 (условная точка для теста)
    rows = [{"id": "way/1", "city_id": "moscow", "tags": {"addr:street": "улица Малая Дмитровка",
             "addr:housenumber": "29 с3"},
             "geometry": {"type": "Point", "coordinates": [lon, lat]}}]  # fmt: skip
    path = tmp_path / "buildings.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
    out = tmp_path / "candidates.jsonl"
    assert main(["--city", "moscow", "--buildings", str(path), "--out", str(out)]) == 0
    items = [json.loads(line) for line in out.read_text("utf-8").splitlines()]
    assert len(items) == 50
    che = next(i for i in items if i["subject_id"] == "6843354")  # «Отель Че»
    assert che["proposal"]["building_id"] == "way/1"
