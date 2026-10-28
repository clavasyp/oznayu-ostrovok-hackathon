"""Приложение: API, /health и раздача интерфейса. Запуск: `uvicorn app.main:app --port 8080`.

Режим данных (`DATA_MODE`):
- `fixtures` (по умолчанию, пока нет базы) — заглушки из настоящих фактов v9;
- `db` — база после `python -m pipeline.init` (миграции + пакеты городов).

Интерфейс — сборка `ui/dist` (Dockerfile, стадия `ui`); пока её нет, на `/` временная страница.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.api.geo_routes import router as geo_router
from app.api.placement_routes import router as placement_router
from app.api.routes import router
from app.api.specs import router as specs_router
from app.state import AppState, Phase
from app.stub_store import StubStore
from core.store_db import DbStore

UI_DIST = Path(__file__).resolve().parents[1] / "ui" / "dist"

PLACEHOLDER = """<!doctype html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>О!знаю</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto">
<h1>О!знаю</h1>
<p>Интерфейс ещё не собран (<code>ui/</code>). API работает:</p>
<ul>
<li><a href="/docs">/docs</a> — все маршруты API</li>
<li><a href="/health">/health</a> — готовность</li>
<li><a href="/api/cities">/api/cities</a> — города</li>
</ul>
</body>
</html>
"""


class SPAStaticFiles(StaticFiles):
    """Сборка интерфейса. Путь вне /api без своего файла получает index.html — маршруты браузера."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            # Под Windows путь приходит с «\»: api\no-such-route — это тоже /api.
            api = path.replace("\\", "/")
            if exc.status_code != 404 or api == "api" or api.startswith("api/"):
                raise
            response = await super().get_response("index.html", scope)
        # Файлы сборки с хешем в имени не меняются — кешируем надолго; страницу — каждый раз
        # сверяем с сервером (etag), иначе после обновления браузер покажет старый интерфейс.
        immutable = path.replace("\\", "/").startswith("assets/")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if immutable else "no-cache"
        )
        return response


# Запросы показа из README: прогреваем кеш базы в фоне, чтобы первый поиск не ждал диска (база
# в Docker на Apple Silicon — в эмуляции amd64). Включается `WARMUP=1` (compose), тесты — без него.
WARMUP_QUERIES = (
    ("тихий номер с видом на закат недалеко от моря, 15 ноября", "sochi"),
    ("хочу отель у моря рядом с парками и хорошими ресторанами", "sochi"),
    ("отель рядом с парком", "moscow"),
)


def _warm_up(engine: Any) -> None:
    from agents.search import parse_query
    from core.search import search

    store = DbStore(engine)
    for text, city in WARMUP_QUERIES:
        try:
            search(store, parse_query(text, city, mode="rules").ast)
        except Exception:  # noqa: BLE001 — прогрев не должен ронять приложение
            return


def _check_init(app: FastAPI) -> None:
    """Режим db: готово, когда init загрузил пакеты городов. Пока нет — /health проверяет снова."""
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.exc import SQLAlchemyError

    from core.db import PackLoad

    state: AppState = app.state.health
    if app.state.engine is None:
        app.state.engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    try:
        with app.state.engine.connect() as conn:
            loaded = conn.execute(select(func.count()).select_from(PackLoad)).scalar_one()
    except SQLAlchemyError as exc:
        state.phase, state.detail = Phase.waiting_for_init, f"база не готова: {type(exc).__name__}"
        return
    if loaded:
        state.phase, state.detail = Phase.ready, None
        # Характеристики, включённые по описанию текстом (задача 4), — в каталог разбора и поиска.
        from agents.catalog import register_features

        try:
            register_features(DbStore(app.state.engine).feature_specs())
        except SQLAlchemyError:
            pass  # база без таблицы feature_spec (миграция ещё не прошла) — каталог из файла
        if os.environ.get("WARMUP") == "1" and not getattr(app.state, "warmed", False):
            import threading

            app.state.warmed = True
            threading.Thread(target=_warm_up, args=(app.state.engine,), daemon=True).start()
    else:
        state.phase, state.detail = Phase.waiting_for_init, "пакеты городов ещё не загружены"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = app.state.health
    state.data_mode = os.environ.get("DATA_MODE", "fixtures")
    try:
        state.phase = Phase.loading
        if state.data_mode == "db":
            _check_init(app)
            app.state.store = DbStore(app.state.engine)
        else:
            app.state.store = StubStore.load()
            state.phase = Phase.ready
    except Exception as exc:  # noqa: BLE001 — показать причину в /health
        state.phase, state.detail = Phase.failed, f"{type(exc).__name__}: {exc}"
    yield
    if app.state.engine is not None:
        app.state.engine.dispose()


def create_app(ui_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="О!знаю", version="0.1.0", lifespan=lifespan)
    app.state.health = AppState()
    app.state.engine = None  # режим db: создаётся при первой проверке готовности
    # Для разработки интерфейса на отдельном dev-сервере (Vite по умолчанию на 5173).
    origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    app.add_middleware(
        CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    def health() -> JSONResponse:
        state: AppState = app.state.health
        if state.phase == Phase.waiting_for_init:
            _check_init(app)  # init мог закончить уже после старта приложения
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
    app.include_router(specs_router)
    app.include_router(geo_router)
    app.include_router(placement_router)
    _mount_ui(app, ui_dist or Path(os.environ.get("UI_DIST", UI_DIST)))
    return app


def _mount_ui(app: FastAPI, dist: Path) -> None:
    """Интерфейс подключается последним: /api, /health и /docs совпадают раньше."""
    if (dist / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="ui")
        return

    @app.get("/", include_in_schema=False)
    def placeholder() -> HTMLResponse:
        return HTMLResponse(PLACEHOLDER)


app = create_app()
