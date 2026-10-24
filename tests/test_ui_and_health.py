from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert

from app.main import create_app
from core.db import PackLoad


def test_root_placeholder_without_ui_build(tmp_path):
    with TestClient(create_app(ui_dist=tmp_path)) as c:
        resp = c.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"] and "/docs" in resp.text


def test_ui_build_served_with_browser_routes(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    with TestClient(create_app(ui_dist=tmp_path)) as c:
        assert "id=root" in c.get("/").text
        assert c.get("/assets/app.js").text == "console.log(1)"
        # Маршрут на стороне браузера при обновлении страницы — тот же index.html.
        assert "id=root" in c.get("/hotel/13736470").text
        # API и /health совпадают раньше интерфейса; неизвестный /api — честный 404 в JSON.
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/api/cities").status_code == 200
        missing = c.get("/api/no-such-route")
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/json")


def test_db_mode_health_rechecks_until_init_loaded_packs(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'init.db'}"
    monkeypatch.setenv("DATA_MODE", "db")
    monkeypatch.setenv("DATABASE_URL", url)
    with TestClient(create_app(ui_dist=tmp_path)) as c:
        # Миграций ещё нет — ждём, а не падаем навсегда.
        resp = c.get("/health")
        assert resp.status_code == 503 and resp.json()["phase"] == "waiting_for_init"

        engine = create_engine(url)
        PackLoad.__table__.create(engine)
        assert c.get("/health").json()["detail"] == "пакеты городов ещё не загружены"

        with engine.begin() as conn:
            conn.execute(
                insert(PackLoad).values(city_id="sochi", pack_version="t", manifest_sha256="0")
            )
        assert c.get("/health").json() == {"status": "ok"}
        engine.dispose()
