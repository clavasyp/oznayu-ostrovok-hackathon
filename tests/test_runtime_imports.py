"""Образ жюри ставит только `api` и `llm` (Dockerfile): модуль приложения, который на верхнем
уровне импортирует что-то ещё, роняет запуск (так было с httpx в core/live.py: SDK моделей тянут
свой httpx2). Тяжёлые библиотеки сборки — только внутри функций калькуляторов."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = {"app", "core", "agents", "calculators", "connectors", "pipeline", "migrations", "tests"}
# Пакеты образа: api и llm из pyproject.toml и то, что они ставят сами.
IMAGE = {
    "fastapi", "starlette", "uvicorn", "sqlalchemy", "psycopg", "alembic", "geoalchemy2",
    "pydantic", "pydantic_settings", "opening_hours", "yaml", "anthropic", "openai",
}  # fmt: skip


def top_level_imports(path: Path) -> set[str]:
    out = set()
    for node in ast.parse(path.read_text("utf-8")).body:
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def runtime_modules() -> set[Path]:
    """Модули, которые грузит приложение: app, core, agents и калькуляторы, на которые они
    ссылаются на верхнем уровне (по цепочке)."""
    todo = [p for d in ("app", "core", "agents") for p in (ROOT / d).rglob("*.py")]
    seen: set[Path] = set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in ast.parse(path.read_text("utf-8")).body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("calculators."):
                todo.append(ROOT / (node.module.replace(".", "/") + ".py"))
    return seen


def test_app_imports_only_what_the_image_installs():
    bad = {}
    for path in runtime_modules():
        extra = top_level_imports(path) - LOCAL - IMAGE - set(sys.stdlib_module_names)
        extra.discard("__future__")
        if extra:
            bad[str(path.relative_to(ROOT))] = sorted(extra)
    assert not bad, f"импорты вне образа (api, llm): {bad}"
