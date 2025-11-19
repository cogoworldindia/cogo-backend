COMPOSE=docker compose

# --- SMART DB CHECK ---
.PHONY: dbcheck
dbcheck:
	@echo "🔍 Checking local PostgreSQL availability..."
	@if ./scripts/check_db.sh; then \
		echo "🌐 Using local PostgreSQL."; \
		export POSTGRES_HOST=host.docker.internal; \
	else \
		echo "🐳 Starting fallback Docker PostgreSQL..."; \
		$(COMPOSE) --profile fallback up -d db; \
		sleep 10; \
		export POSTGRES_HOST=db; \
	fi
# 	@echo "🧠 Ensuring database exists..."
# 	@./scripts/ensure_db.sh || echo "⚠️ Could not create DB automatically."

# --- SMART REDIS CHECK ---
.PHONY: redischeck
redischeck:
	@echo "🔍 Checking local Redis availability..."
	@if ./scripts/check_redis.sh; then \
		echo "🌐 Using local Redis."; \
		export REDIS_HOST=host.docker.internal; \
	else \
		echo "🐳 Starting fallback Docker Redis..."; \
		$(COMPOSE) --profile fallback up -d redis; \
		sleep 10; \
		export REDIS_HOST=redis; \
	fi

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

# --- DOCKER ---
.PHONY: build
build:
	@echo "🏗️ Building all services..."
	$(COMPOSE) build
	@echo "✅ Build complete."

.PHONY: up
up: dbcheck redischeck
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

.PHONY: clean
clean:
	@echo "🧹 Cleaning up..."
	$(COMPOSE) down -v --remove-orphans
	@echo "✅ Cleanup done."
