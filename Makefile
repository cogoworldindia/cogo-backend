# CoGo backend — local development entry points. `make help` lists targets.
COMPOSE  := docker compose --env-file .env --env-file .env.secrets
PY_PROJECTS := libs/common services/auth services/user services/journey services/social services/chat services/notification services/admin services/api-gateway

.DEFAULT_GOAL := help
.PHONY: help setup secrets check-docker build up down restart ps logs logs-% migrate obs-up \
        test lint smoke e2e contracts api-docs clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_%-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

setup: ## Init submodules and local secrets (first run)
	git submodule update --init --recursive
	@$(MAKE) --no-print-directory secrets

secrets: ## Create .env/.env.secrets/secrets/ if missing (keeps existing values)
	@./scripts/init-secrets.sh

check-docker:
	@docker info >/dev/null 2>&1 || { echo "Cannot reach the Docker daemon (is it running? are you in the docker group?)"; exit 1; }

build: check-docker secrets ## Build all images
	$(COMPOSE) build

up: check-docker secrets ## Build and start the stack; waits until healthy
	$(COMPOSE) up -d --build --wait --wait-timeout 180
	@printf '\nGateway   http://localhost:8000\nMailpit   http://localhost:8025\nRabbitMQ  http://localhost:15672\n'

down: ## Stop the stack (keeps data)
	$(COMPOSE) --profile observability down

restart: down up ## Restart the stack

ps: ## Show service status
	$(COMPOSE) ps -a

logs: ## Tail all logs
	$(COMPOSE) logs -f --tail=50

logs-%: ## Tail one service, e.g. make logs-auth-service
	$(COMPOSE) logs -f --tail=100 $*

migrate: ## Run DB migrations (also run automatically by `up`)
	$(COMPOSE) run --rm auth-migrate
	$(COMPOSE) run --rm user-migrate

obs-up: ## Start Jaeger (16686), Prometheus (9090), Grafana (3001)
	$(COMPOSE) --profile observability up -d

test: ## Run every test suite (needs Docker for testcontainers)
	@set -e; for p in $(PY_PROJECTS); do echo "== $$p"; (cd $$p && uv run --quiet pytest -q -p no:logging); done
	@echo "== services/admin/web (console)"; cd services/admin/web && { [ -d node_modules ] || npm ci --no-audit --no-fund --silent; } && npm test --silent

lint: ## ruff + mypy for every Python project, and the console's typecheck
	@set -e; for p in $(PY_PROJECTS); do echo "== $$p"; (cd $$p && uv run --quiet ruff check . && uv run --quiet ruff format --check . && uv run --quiet mypy $$( [ -d src ] && echo src || echo app )); done
	@echo "== services/admin/web (console)"; cd services/admin/web && { [ -d node_modules ] || npm ci --no-audit --no-fund --silent; } && npm run --silent typecheck

contracts: ## Regenerate contracts/ (event JSON Schemas + OpenAPI); CI fails if they drift
	@cd libs/common && uv run --quiet python ../../scripts/export_event_schemas.py ../../contracts/events
	@set -e; for s in auth user journey social chat notification admin; do (cd services/$$s && PYTHONPATH=. uv run --quiet python ../../scripts/export_openapi.py ../../contracts/openapi/$$s-service.json); done

api-docs: contracts ## Rebuild docs/api/CoGo-API-Reference.xlsx from the code (fails if api_content.py is stale)
	@cd libs/common && uv run --quiet --with openpyxl python ../../docs/api/build_api_xlsx.py ../../contracts/openapi ../../docs/api/CoGo-API-Reference.xlsx

smoke: ## Quick end-to-end check against the running stack (~1 min)
	@python3 scripts/smoke.py

e2e: ## Full end-to-end API test: 4 users, every public endpoint, coverage vs contracts (~2 min)
	@python3 scripts/e2e.py

clean: ## Stop and DELETE all local data volumes
	$(COMPOSE) --profile observability down -v --remove-orphans
