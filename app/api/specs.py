"""API новой характеристики из текста (core §7.4) — для формы админа «новая характеристика текстом».

    POST /api/specs/draft  {text, use_llm}            → черновик (YAML), проблемы, замечания
    POST /api/specs/trial  {spec | yaml, city_id, n}  → покрытие пробного расчёта на n отелях

Ничего не сохраняется и не включается: черновик правит админ, включает человек (статус `active`
и файл в `specs/` — у лида).
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from agents.specgen import FeatureSpec, draft_spec, from_yaml, trial_run
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


@router.post("/draft")
def draft(body: DraftRequest) -> dict[str, Any]:
    return {"status": "draft", "saved": False} | draft_spec(
        body.text, use_llm=body.use_llm
    ).as_dict()


@router.post("/trial")
def trial(body: TrialRequest, request: Request) -> dict[str, Any]:
    spec = body.spec
    if spec is None and body.yaml:
        try:
            spec = from_yaml(body.yaml)
        except (yaml.YAMLError, ValidationError) as e:
            raise HTTPException(422, f"YAML не читается как спецификация: {e}") from e
    if spec is None:
        raise HTTPException(422, "нужен spec или yaml")
    return trial_run(spec, request.app.state.store, body.city_id, n=body.n)
