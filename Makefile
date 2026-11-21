# CoGo backend — local development entry points. `make help` lists targets.
COMPOSE  := docker compose --env-file .env --env-file .env.secrets
PY_PROJECTS := libs/common services/auth services/user services/journey services/social services/chat services/notification services/admin services/api-gateway

.DEFAULT_GOAL := help
.PHONY: help setup secrets check-docker build up down restart ps logs logs-% migrate obs-up \
        test lint smoke e2e fuzz seed load-up load-seed load-feed load-journeys load-ws load-soak dlq \
        chart explain contracts api-docs clean

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

seed: ## Seed data through the API (spec §12): 50 users, 200 journeys, chats
	@python3 scripts/seed.py --users 50 --journeys 4

# ---------------------------------------------------------------- load tests (spec §12)
# K6_MEMORY caps the load generator so an oversized run kills k6, not the machine
K6_MEMORY ?= 3g
K6 := docker run --rm -i --network host --memory $(K6_MEMORY) --memory-swap $(K6_MEMORY) \
      -v $(CURDIR)/loadtest:/scripts -w /scripts \
      -e BASE_URL -e RATE -e DURATION -e SOCKETS -e FEED_RATE -e JOURNEY_RATE grafana/k6:1.3.0

load-up: ## Stack for load tests: no rate limits, 4 h tokens, extra workers standing in for replicas
	GATEWAY_RATE_LIMIT_ENABLED=false ACCESS_TOKEN_TTL_SECONDS=14400 API_WEB_CONCURRENCY=4 \
	  GATEWAY_WEB_CONCURRENCY=3 $(MAKE) up

load-seed: ## 200 load-test users (2 journeys each) through the API
	@python3 scripts/seed.py --users 200 --journeys 2

load-feed: ## k6: feed scroll at RATE req/s (default 200) for DURATION (2m)
	$(K6) run feed.js

load-journeys: ## k6: RATE journeys/min (default 500), time to a filled match banner
	$(K6) run journeys.js

load-ws: ## k6: SOCKETS open sockets (default 1000), RATE msg/s (50), delivery latency
	$(K6) run sockets.js

load-soak: export DURATION ?= 1h
load-soak: ## k6: every flow at once for DURATION (1h), then the DLQ check (spec §13 M8)
	$(K6) run soak.js
	@python3 scripts/dlq_check.py

dlq: ## Dead-letter queue depths; fails if any holds a message
	@python3 scripts/dlq_check.py

fuzz: ## Schemathesis on every public route through the gateway; fails on any 5xx (after make load-up)
	@python3 scripts/fuzz.py

HELM := docker run --rm -v $(CURDIR):/w -w /w alpine/helm:4.3.1
KUBECONFORM := docker run --rm -i ghcr.io/yannh/kubeconform:v0.8.0 -strict -summary \
  -kubernetes-version 1.34.0 -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

chart: ## Lint the staging Helm chart and validate its manifests (defaults, staging, + PgBouncer)
	@cmp -s infra/prometheus/rules/cogo.yml deploy/helm/cogo/files/alerts.yml || { echo "deploy/helm/cogo/files/alerts.yml differs from infra/prometheus/rules/cogo.yml: copy it over"; exit 1; }
	$(HELM) lint deploy/helm/cogo
	$(HELM) lint deploy/helm/cogo -f deploy/helm/cogo/values-staging.yaml
	@set -e; for args in "" "-f deploy/helm/cogo/values-staging.yaml" \
	    "-f deploy/helm/cogo/values-staging.yaml --set pgbouncer.enabled=true"; do \
	  echo "== template $$args"; $(HELM) template cogo deploy/helm/cogo $$args | $(KUBECONFORM); done

explain: ## EXPLAIN ANALYZE the feed and matches queries on the current data (spec §11)
	@$(COMPOSE) cp scripts/explain_queries.py journey-service:/tmp/explain_queries.py
	@$(COMPOSE) exec -T -w /app -e PYTHONPATH=/app journey-service python /tmp/explain_queries.py

clean: ## Stop and DELETE all local data volumes
	$(COMPOSE) --profile observability down -v --remove-orphans
