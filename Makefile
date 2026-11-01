# Короткие команды. Рецепты — POSIX sh; то же самое можно запустить напрямую через docker compose.
COMPOSE ?= docker compose
DEV = $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml
URL ?= http://localhost:8080

.PHONY: up down seed logs ps dev db test test-db test-packs audit lint fmt lock clean

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

TEST_DB = postgresql+psycopg://oznayu:oznayu@localhost:55432/oznayu_test

test-db: .env ## все тесты, включая тесты на настоящем PostGIS: база oznayu_test в контейнере make db
	$(DEV) up -d --wait db
	$(DEV) exec -T db sh -c 'psql -U "$$POSTGRES_USER" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = '"'"'oznayu_test'"'"'" | grep -q 1 || createdb -U "$$POSTGRES_USER" oznayu_test'
	TEST_DATABASE_URL=$(TEST_DB) uv run pytest -q

test-packs: .env ## поставляемые пакеты packs/ в PostGIS, как при обычном запуске (несколько минут)
	$(DEV) up -d --wait db
	$(DEV) exec -T db sh -c 'psql -U "$$POSTGRES_USER" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname = '"'"'oznayu_test'"'"'" | grep -q 1 || createdb -U "$$POSTGRES_USER" oznayu_test'
	TEST_DATABASE_URL=$(TEST_DB) SHIPPED_PACKS=1 uv run pytest -q -s tests/test_shipped_packs_db.py

audit: ## офлайн-аудит всех пакетов → pipeline/reports/seva_pack_quality_audit.json (после пересборки)
	uv run python -m pipeline.audit_packs --packs packs --report pipeline/reports/seva_pack_quality_audit.json

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

# Публичный ключ организаторов (docs.ostrovok.tech, «Шифрование секретов (ключей)»).
