import pytest

from agents.address.normalize import osm_addresses, parse_address, skeleton


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


def test_osm_tags_and_second_address():
    tags = {
        "addr:street": "улица Земляной Вал",
        "addr:housenumber": "52/16 с3",
        "addr2:street": "Нижняя Сыромятническая улица",
        "addr2:housenumber": "1",
    }
    assert [a.house_key for a in osm_addresses(tags)] == ["52/16с3", "1"]
    assert osm_addresses({"building": "yes"}) == []
