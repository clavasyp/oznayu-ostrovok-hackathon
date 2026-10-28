"""API новой характеристики из текста (core §7.4) — для формы админа «новая характеристика текстом».

    POST /api/specs/draft  {text, use_llm}            → черновик (YAML), проблемы, замечания
    POST /api/specs/trial  {spec | yaml, city_id, n}  → покрытие пробного расчёта на n отелях
    POST /api/specs/activate {spec | yaml, cities}    → расчёт по всем отелям, факты в базе,
                                                         характеристика в каталоге
    GET  /api/specs                                   → включённые характеристики и покрытие

Черновик и пробный расчёт ничего не сохраняют. Включает человек (D33): тогда характеристика
считается для всех отелей городов и сразу доступна разбору и поиску — без правки кода.
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from agents.specgen import FeatureSpec, draft_spec, from_yaml, trial_run
from agents.specgen.activate import FOCUS_CITIES, activate
from agents.specgen.trial import TRIAL_N

router = APIRouter(prefix="/api/specs")


class DraftRequest(BaseModel):
    text: str = Field(min_length=3, max_length=2000)
    use_llm: bool = True


class TrialRequest(BaseModel):
    spec: FeatureSpec | None = None
    yaml: str | None = None
    city_id: str = "sochi"
    n: int = Field(default=TRIAL_N, ge=1, le=200)


class ActivateRequest(BaseModel):
    spec: FeatureSpec | None = None
    yaml: str | None = None
    cities: list[str] = Field(default_factory=lambda: list(FOCUS_CITIES), max_length=8)


def _spec(spec: FeatureSpec | None, text: str | None) -> FeatureSpec:
    if spec is None and text:
        try:
            spec = from_yaml(text)
        except (yaml.YAMLError, ValidationError) as e:
            raise HTTPException(422, f"YAML не читается как спецификация: {e}") from e
    if spec is None:
        raise HTTPException(422, "нужен spec или yaml")
    return spec


@router.get("")
def specs(request: Request) -> dict[str, Any]:
    return {"features": request.app.state.store.feature_specs()}


@router.post("/activate")
def activate_spec(body: ActivateRequest, request: Request) -> dict[str, Any]:
    result = activate(_spec(body.spec, body.yaml), request.app.state.store, tuple(body.cities))
    if not result["activated"]:
        raise HTTPException(422, result)
    return result


@router.post("/draft")
def draft(body: DraftRequest) -> dict[str, Any]:
    return {"status": "draft", "saved": False} | draft_spec(
        body.text, use_llm=body.use_llm
    ).as_dict()


@router.post("/trial")
def trial(body: TrialRequest, request: Request) -> dict[str, Any]:
    return trial_run(_spec(body.spec, body.yaml), request.app.state.store, body.city_id, n=body.n)
