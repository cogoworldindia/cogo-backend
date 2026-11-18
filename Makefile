# Auto-detect Docker Compose: prefer V2 plugin, fall back to standalone binary.
# For V2, pass both env files so secrets are available for compose-file variable
# substitution (e.g. ${POSTGRES_PASSWORD} in environment: blocks).
ifeq ($(shell docker compose version > /dev/null 2>&1 && echo yes || echo no),yes)
  COMPOSE = docker compose \
    $(if $(wildcard .env),--env-file .env) \
    $(if $(wildcard .env.secrets),--env-file .env.secrets)
else
  COMPOSE = docker-compose
endif

# --- PRE-FLIGHT CHECKS ---
.PHONY: check-docker check-env
check-docker:
	@if ! docker info > /dev/null 2>&1; then \
		echo ""; \
		echo "ERROR: Cannot connect to the Docker daemon."; \
		echo ""; \
		echo "Fix: add your user to the docker group, then log out and back in:"; \
		echo "  sudo usermod -aG docker $$USER"; \
		echo "  newgrp docker   (or log out/in)"; \
		echo ""; \
		exit 1; \
	fi

check-env:
	@if [ ! -f .env ]; then \
		echo "ERROR: .env file not found."; \
		echo "Run: cp .env.example .env && cp .env.secrets.example .env.secrets"; \
		echo "Then fill in .env.secrets with real credentials."; \
		exit 1; \
	fi
	@if [ ! -f .env.secrets ]; then \
		echo "ERROR: .env.secrets file not found."; \
		echo "Run: cp .env.secrets.example .env.secrets"; \
		echo "Then fill in the credentials."; \
		exit 1; \
	fi

# --- SMART DB CHECK ---
.PHONY: dbcheck
dbcheck:
	@echo "🔍 Checking local PostgreSQL availability..."
	@./scripts/check_db.sh || { \
		echo "🐳 Starting fallback Docker PostgreSQL..."; \
		$(COMPOSE) --profile fallback up -d db; \
		echo "Waiting for PostgreSQL to be ready..."; \
		sleep 10; \
	}

# --- SMART REDIS CHECK ---
.PHONY: redischeck
redischeck:
	@echo "🔍 Checking local Redis availability..."
	@./scripts/check_redis.sh || { \
		echo "🐳 Starting fallback Docker Redis..."; \
		$(COMPOSE) --profile fallback up -d redis; \
		echo "Waiting for Redis to be ready..."; \
		sleep 10; \
	}

# --- GIT ---
.PHONY: setup
setup:
	@echo "🧩 Initializing git submodules..."
	git submodule update --init --recursive
	@echo "✅ Submodules initialized."

.PHONY: update
update:
	@echo "🔄 Updating submodules..."
	git submodule update --remote --merge
	@echo "✅ Submodules updated."

# --- MIGRATIONS ---
# Run these once after 'make up' or whenever schema changes are deployed.
.PHONY: migrate-auth
migrate-auth:
	@echo "Running auth service migrations..."
	$(COMPOSE) exec auth-service alembic upgrade head
	@echo "Auth migrations complete."

.PHONY: migrate-user
migrate-user:
	@echo "Running user service migrations..."
	$(COMPOSE) exec user-service alembic upgrade head
	@echo "User migrations complete."

.PHONY: migrate
migrate: migrate-auth migrate-user

# --- DOCKER ---
.PHONY: build
build:
	@echo "🏗️ Building all services..."
	$(COMPOSE) build
	@echo "✅ Build complete."

.PHONY: up
up: check-docker check-env dbcheck redischeck
	@echo "🚀 Starting all containers..."
	$(COMPOSE) up -d
	@echo "✅ All services running."

.PHONY: down
down:
	@echo "🛑 Stopping containers..."
	$(COMPOSE) down
	@echo "✅ Containers stopped."

.PHONY: logs
logs:
	$(COMPOSE) logs -f --tail=50

.PHONY: logs-auth
logs-auth:
	$(COMPOSE) logs -f --tail=100 auth-service

.PHONY: logs-user
logs-user:
	$(COMPOSE) logs -f --tail=100 user-service

.PHONY: logs-gateway
logs-gateway:
	$(COMPOSE) logs -f --tail=100 api-gateway

.PHONY: logs-mail
logs-mail:
	$(COMPOSE) logs -f --tail=100 mail-service

.PHONY: clean
clean:
	@echo "🧹 Cleaning up..."
	$(COMPOSE) down -v --remove-orphans
	@echo "✅ Cleanup done."
