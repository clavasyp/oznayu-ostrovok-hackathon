import pytest

from agents.llm import Gateway
from agents.llm.fake import FakeLLM
from agents.specgen import check, draft_spec, from_yaml, to_yaml
from agents.specgen.draft import VEGETATION, rules_draft
from agents.specgen.registry import WORLDCOVER_CLASSES, listing
from core import plugins

OFFLINE = Gateway([])
BUILT_UP = "Доля застройки в 300 м"


@pytest.fixture
def no_landcover(monkeypatch):
    """Калькулятора Леры ещё нет — и тест не сломается, когда он появится."""
    monkeypatch.delitem(plugins.CALCULATORS, "env.landcover_share", raising=False)


def test_rules_draft_built_up_share(no_landcover):
    d = draft_spec(BUILT_UP, gateway=OFFLINE)
    assert d.by == "rules" and d.problems == []
    s = d.spec
    assert (s.id, s.subject_level, s.status) == ("built_up_share_300m", "hotel", "draft")
    assert s.value["share_pct"].unit == "%" and s.search.default_field == "share_pct"
    (r,) = s.recipes
    assert r.calculator == "env.landcover_share@1"
    assert r.params == {"classes": [50], "radius_m": 300}
    assert r.requires.layers == ["worldcover"]
    assert from_yaml(d.yaml) == s  # YAML читается обратно в ту же спецификацию
    # Калькулятор Леры ещё не в реестре — черновик есть, пробного расчёта нет.
    assert not d.trial_ready and any("ещё не зарегистрирован" in w for w in d.warnings)
    assert rules_draft("доля леса в радиусе 1,5 км").recipes[0].params == {
        "classes": [10],
        "radius_m": 1500,
    }
    assert set(VEGETATION) <= set(WORLDCOVER_CLASSES)


def test_existing_feature_is_not_created_again():
    d = draft_spec("доля зелени в радиусе 300 м", gateway=OFFLINE)
    assert any("уже есть — E03" in p for p in d.problems)


def _broken(**changes):
    spec = rules_draft(BUILT_UP)
    data = spec.model_dump()
    recipe = data["recipes"][0]
    for key, value in changes.items():
        if key.startswith("recipe."):
            recipe[key.removeprefix("recipe.")] = value
        else:
            data[key] = value
    return type(spec).model_validate(data)


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"id": "Built Up!"}, "латиница"),
        ({"id": "e03"}, "уже занят"),
        ({"recipe.calculator": "env.magic@1"}, "калькулятора «env.magic» нет"),
        ({"recipe.calculator": "env.landcover_share@7"}, "версия 1"),
        ({"recipe.requires": {"layers": ["satellite_magic"]}}, "слоя «satellite_magic» нет"),
        ({"recipe.params": {"classes": [55], "radius_m": 300}}, "кодов WorldCover"),
        ({"recipe.params": {"classes": [50], "radius_m": 5000}}, "от 100 до 2000"),
        ({"recipe.params": {"classes": [50], "depth": 3}}, "нет параметров ['depth']"),
        ({"value": {"share_pct": {"type": "number", "unit": "m"}}}, "даёт «%»"),
        ({"subject_level": "room_type"}, "считает уровень hotel"),
        ({"search": {"default_field": "nope", "operators": ["lte"]}}, "нет такого поля"),
        ({"recipes": []}, "нет ни одного рецепта"),
    ],
)
def test_validator_problems(changes, problem):
    found = check(_broken(**changes)).problems
    assert any(problem in p for p in found), found


def test_hotelier_recipe_needs_question_and_fields():
    spec = _broken(**{"recipe.kind": "hotelier", "recipe.calculator": None,
                      "recipe.question": "", "recipe.parse_to": ["nope"]})  # fmt: skip
    found = " ".join(check(spec).problems)
    assert "нет текста" in found and "parse_to" in found


COMPACT = {
    "understood": "true",
    "reason": "",
    "id": "Entrance Steps",
    "title": "Ступеньки у входа в отель",
    "family": "accessibility",
    "subject_level": "hotel",
    "value_fields": [{"name": "steps", "type": "integer", "unit": "count"}],
    "default_field": "steps",
    "operators": ["lte", "eq"],
    "synonyms": ["без ступенек у входа", "вход без лестницы"],
    "recipes": [{"kind": "hotelier", "calculator": "", "layers": [], "params": [],
                 "question": "Сколько ступенек от тротуара до двери отеля?",
                 "parse_to": ["steps"]}],
    "related": ["A02"],
}  # fmt: skip


def test_model_path_only_known_calculators_and_layers():
    fake = FakeLLM([COMPACT])
    d = draft_spec("сколько ступенек у входа в отель", gateway=Gateway([fake]))
    assert d.by == "llm" and d.problems == [] and d.spec.id == "entrance_steps"
    assert d.spec.recipes[0].id == "entrance_steps-R1"  # ID рецептов ставит код
    assert "env.landcover_share@1" in fake.calls[0]["system"]  # модель видит реестр
    assert not d.trial_ready  # только вопрос отельеру — считать нечего

    invented = {"kind": "computed", "calculator": "osm.steps_counter@1", "layers": ["lidar"]}
    made_up = COMPACT | {"recipes": [COMPACT["recipes"][0] | invented]}
    d = draft_spec("ступеньки", gateway=Gateway([FakeLLM([made_up])]))
    found = " ".join(d.problems)
    assert "калькулятора «osm.steps_counter» нет" in found and "слоя «lidar» нет" in found

    no = COMPACT | {"understood": "false", "reason": "уже есть A02"}
    d = draft_spec("ширина входа", gateway=Gateway([FakeLLM([no])]))
    assert d.spec is None and "уже есть A02" in d.notes[0]
    assert draft_spec("ширина входа", gateway=OFFLINE).notes  # без ключа — честная пометка


def test_listing_names_layers_and_calculators(no_landcover):
    text = listing()
    assert "worldcover" in text and "50 застройка" in text
    assert "env.landcover_share@1 (ещё не зарегистрирован)" in text
    assert to_yaml(rules_draft(BUILT_UP)).count("parse_to") == 0  # пустые поля не пишем
