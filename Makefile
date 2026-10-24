# Короткие команды. Рецепты — POSIX sh; то же самое можно запустить напрямую через docker compose.
COMPOSE ?= docker compose
DEV = $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml
URL ?= http://localhost:8080

.PHONY: up down seed logs ps dev db test lint fmt lock clean

# Свой .env из примера — только если его ещё нет.
.env:
	cp .env.example .env

up: .env ## собрать и поднять db → init → app, дождаться /health
	$(COMPOSE) up --build -d
	@i=0; until curl -fsS $(URL)/health >/dev/null 2>&1; do \
	  i=$$((i + 1)); [ $$i -gt 300 ] && { echo "нет ответа от $(URL)/health за 10 минут"; exit 1; }; sleep 2; \
	done; echo "Готово: $(URL)"

down: ## остановить (база сохраняется)
	$(COMPOSE) down

seed: .env ## заново: миграции и загрузка пакетов городов (то же делает init при каждом запуске)
	$(COMPOSE) run --rm init

logs: ## логи всех сервисов
	$(COMPOSE) logs -f

ps: ## состояние сервисов
	$(COMPOSE) ps -a

dev: .env ## полный стек с автоперезагрузкой кода; база на 127.0.0.1:55432
	$(DEV) up --build

db: .env ## только база на 127.0.0.1:55432 — для uv run на своей машине
	$(DEV) up -d db

test: ## тесты
	uv run pytest -q

lint: ## стиль и актуальность uv.lock
	uv lock --check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## поправить стиль
	uv run ruff format .
	uv run ruff check --fix .

lock: ## обновить uv.lock после правки pyproject.toml
	uv lock

clean: ## остановить и удалить базу (данные init загрузит заново)
	$(COMPOSE) down -v
