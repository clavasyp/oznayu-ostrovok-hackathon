# Образ О!знаю: API + интерфейс. Один образ для сервисов init и app (docker-compose.yml).
# Три стадии: интерфейс (Node) → зависимости Python (uv по uv.lock) → итоговый образ без инструментов сборки.

# 1. Интерфейс (ui/, Андрей). Есть ui/package.json → npm ci и npm run build → ui/dist.
#    Нет — пустая dist, и приложение отдаёт на / временную страницу.
FROM node:24.22.1-alpine3.24 AS ui
WORKDIR /ui
COPY ui/ ./
RUN if [ -f package.json ]; then \
        if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi \
        && npm run build; \
    fi \
    && mkdir -p dist

# 2. Зависимости Python: только api и llm. Тяжёлые библиотеки сборки пакетов (builder) жюри не нужны.
FROM python:3.13.17-slim-trixie AS deps
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra api --extra llm

# 3. Итоговый образ: venv, код, пакеты городов и собранный интерфейс. Процесс не от root.
FROM python:3.13.17-slim-trixie AS app
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY . .
COPY --from=ui /ui/dist ./ui/dist
USER app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
