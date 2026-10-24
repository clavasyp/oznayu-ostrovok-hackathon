"""Приложение: API, /health и раздача интерфейса. Запуск: `uvicorn app.main:app --port 8080`.

Режим данных (`DATA_MODE`):
- `fixtures` (по умолчанию, пока нет базы) — заглушки из настоящих фактов v9;
- `db` — база после `python -m pipeline.init` (миграции + пакеты городов).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.state import AppState, Phase
from app.stub_store import StubStore


def _wait_for_init(state: AppState) -> None:
    from sqlalchemy import create_engine, func, select

    from core.db import PackLoad

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        loaded = conn.execute(select(func.count()).select_from(PackLoad)).scalar_one()
    if loaded:
        state.phase = Phase.ready
    else:
        state.phase, state.detail = Phase.waiting_for_init, "пакеты городов ещё не загружены"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = app.state.health
    state.data_mode = os.environ.get("DATA_MODE", "fixtures")
    try:
        state.phase = Phase.loading
        app.state.store = StubStore.load()  # заглушки нужны и в режиме db, пока API не на базе
        if state.data_mode == "db":
            _wait_for_init(state)
        else:
            state.phase = Phase.ready
    except Exception as exc:  # noqa: BLE001 — показать причину в /health
        state.phase, state.detail = Phase.failed, f"{type(exc).__name__}: {exc}"
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="О!знаю", version="0.1.0", lifespan=lifespan)
    app.state.health = AppState()
    # Для разработки интерфейса на отдельном dev-сервере (Vite по умолчанию на 5173).
    origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    app.add_middleware(
        CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    def health() -> JSONResponse:
        state: AppState = app.state.health
        if state.ready:
            return JSONResponse({"status": "ok"})
        body: dict[str, Any] = {"status": "starting", "phase": state.phase}
        if state.detail:
            body["detail"] = state.detail
        return JSONResponse(body, status_code=503)

    @app.get("/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        return health()

    app.include_router(router)
    return app


app = create_app()
