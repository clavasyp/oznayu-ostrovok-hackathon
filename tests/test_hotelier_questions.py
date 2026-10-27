"""Вопросы отельеру для полного каталога: шаблоны v9, предзаполнение из заявлений Островка."""

from agents.hotelier.questions import enrich_question
from connectors.ostrovok_site import parse_page
from pipeline.hotelier_questions import build_questions, templates
from tests.test_ostrovok_import import snapshot as snapshot

BAN = "Размещение с домашними животными не допускается"
ADMISSION = "Размещение с домашними животными"


def page(snapshot, *, pets=None, hid="101"):
    root, add = snapshot

    def mutate(hotel):
        hotel["roomGroups"][0]["nameStruct"]["beddingType"] = "двуспальная кровать"
        if pets:
            hotel["amenityGroups"] = [{"amenities": [{"name": pets}]}]

    entry, _ = add(hid=hid, mutate=mutate)
    return parse_page(root, entry)


def raw_facts(catalog):
    return [f.model_dump(mode="json", exclude_none=True) for f in catalog.facts]


def shown(row):
    """Предзаполнение в кабинете: хранилище отдаёт колонку `prefill` как `claims`."""
    return enrich_question(row | {"claims": row["prefill"]})["prefill"]


def test_new_hotel_gets_template_questions_with_ostrovok_prefill(snapshot):
    catalog = page(snapshot, pets=ADMISSION)
    hotels = [h.model_dump() for h in catalog.hotels]
    rows, stats = build_questions(hotels, raw_facts(catalog), [])
    assert stats["new"] == len(templates()) == len(rows)  # «можно, вид не назван» — спросим
    by_feature = {r["feature_id"]: r for r in rows}
    assert {r["subject_id"] for r in rows} == {"hotel:101"}
    assert by_feature["S04"]["text"] == templates()["S04"]["free_text_question"]
    assert by_feature["S04"]["fields"] == templates()["S04"]["parsed_fields"].split(",")
    pet = by_feature["P01"]["prefill"]
    assert [c["quote"] for c in pet] == [ADMISSION]
    assert pet[0]["json_pointer"].endswith("/amenities/0/name")
    assert pet[0]["source_url"].startswith("https://ostrovok.ru/hotel/")
    assert shown(by_feature["P01"]).startswith(
        f"На странице отеля на Островке указано: «{ADMISSION}»"
    )
    # Метка категории — одна цитата на вопрос, все категории с ней — списком.
    (air,) = by_feature["S04"]["prefill"]
    assert air["quote"] == "air-conditioning" and air["source_subject_ids"] == ["101:rg1"]
    assert shown(by_feature["M01"]).startswith(
        "На странице отеля на Островке указано: «двуспальная кровать»"
    )
    again, _ = build_questions(hotels, raw_facts(catalog), [])
    assert again == rows  # детерминированно: те же id и порядок


def test_explicit_ban_closes_the_pet_question(snapshot):
    catalog = page(snapshot, pets=BAN)
    rows, stats = build_questions([h.model_dump() for h in catalog.hotels], raw_facts(catalog), [])
    assert "P01" not in {r["feature_id"] for r in rows}
    assert stats["closed_by_fact"] == 1


def test_cohort_questions_keep_v9_prefill_and_gain_new_quotes(snapshot):
    catalog = page(snapshot, pets=BAN)
    hotels = [h.model_dump() for h in catalog.hotels]
    v9 = [
        {
            "id": 1,
            "hotel_id": "101",
            "feature_id": fid,
            "prefill": [{"quote": f'"{BAN}"', "subject_type": "hotel"}] if fid == "P01" else [],
        }
        for fid in templates()
    ]
    rows, stats = build_questions(hotels, raw_facts(catalog), v9)
    assert len(rows) == len(v9) and stats["new"] == 0  # у отеля когорты свой набор v9
    by_feature = {r["feature_id"]: r for r in rows}
    assert by_feature["P01"]["prefill"] == v9[[*templates()].index("P01")]["prefill"]
    assert [c["quote"] for c in by_feature["M01"]["prefill"]] == ["двуспальная кровать"]
