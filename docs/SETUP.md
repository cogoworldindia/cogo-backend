# CoGo backend: project setup

This guide gets you from zero to a running stack, then covers day-to-day work: running a
service with hot reload, migrations, tests, the multi-repo workflow, adding a service, and
troubleshooting. For how the system fits together, see [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Prerequisites

| Tool | Version | Why |
|---|---|---|
| Docker Engine + Compose plugin | Engine 24+, Compose 2.24+ | Runs the whole stack; tests use testcontainers |
| git | 2.30+ | Submodules |
| make, openssl | any | Entry points; secret generation |
| [uv](https://docs.astral.sh/uv/) | 0.11+ | Python toolchain. It installs Python 3.13 itself |
| python3 | 3.10+ on the host | Runs `scripts/smoke.py` (stdlib only) |

You also need SSH access to the `cogoworldindia` GitHub org: read access for the submodules,
write access to push. Check it with `ssh -T git@github.com`.

Your user must be able to run Docker without `sudo`:
`sudo usermod -aG docker $USER`, then log out and back in.

## 2. First run

```bash
git clone --recursive git@github.com:cogoworldindia/cogo-backend.git
cd cogo-backend
make setup     # submodules + .env + .env.secrets + secrets/jwt_private_key.pem
make up        # build images, run migrations, start everything, wait until healthy
make smoke     # 21 end-to-end checks through the gateway
```

`make setup` is idempotent: it never overwrites a value you already have. On the first `make up`,
expect a few minutes while the images build. Later runs reuse the cache.

If you already cloned without `--recursive`, run `git submodule update --init --recursive`
(`make setup` does this for you).

## 3. What's running

| Service | URL on your machine | Notes |
|---|---|---|
| api-gateway | http://localhost:8000 | The **only** API endpoint clients use |
| Mailpit | http://localhost:8025 | Every email the stack sends lands here (OTP codes too). Nothing reaches real inboxes |
| RabbitMQ UI | http://localhost:15672 | User `cogo`, password `RABBITMQ_PASSWORD` in `.env.secrets` |
| Postgres | `localhost:5433` | Superuser `postgres` / `POSTGRES_PASSWORD`. Port 5433 avoids clashing with a host Postgres on 5432 |
| Redis | `localhost:6380` | For running services natively |
| Media store (RustFS) | http://localhost:9000 | S3-compatible; presigned uploads go here in dev |
| RabbitMQ AMQP | `localhost:5672` | For running services natively |
| auth-, user-, journey-, social-, notification-service | not published | Reach them through the gateway, or with `docker compose exec` |

All ports bind to `127.0.0.1` only.

Try the API by hand:

```bash
curl -s localhost:8000/v1/auth/email/send -H 'content-type: application/json' \
     -d '{"email":"me@example.com"}'
# Read the 6-digit code at http://localhost:8025, then:
curl -s localhost:8000/v1/auth/email/verify -H 'content-type: application/json' \
     -d '{"email":"me@example.com","code":"123456"}'
# Use data.access_token as:  -H "Authorization: Bearer <token>"
curl -s localhost:8000/v1/users/me -H "Authorization: Bearer $TOKEN"
```

## 4. Configuration

| File | Committed? | Contents |
|---|---|---|
| `.env` | no (template `.env.example`) | Non-secret settings: log level, DB names, token lifetimes, `FIREBASE_PROJECT_ID`, CORS origins, SMTP host, tracing endpoint |
| `.env.secrets` | **never** (template `.env.secrets.example`) | Postgres, per-service DB and RabbitMQ passwords, `OTP_HMAC_KEY`, SMTP credentials |
| `secrets/jwt_private_key.pem` | **never** | Ed25519 key that signs access tokens. Mounted only into auth-service |

The Makefile passes both env files to Compose
(`docker compose --env-file .env --env-file .env.secrets`). If you call `docker compose` yourself,
pass both files too, or use the Make targets.

Settings you are most likely to change:

| Variable | Default | Effect |
|---|---|---|
| `FIREBASE_PROJECT_ID` | `cogo-d1d73` | Enables Google sign-in. Leave it empty to disable it (the endpoint then returns 503 `provider_disabled`) |
| `CORS_ALLOW_ORIGINS` | `["http://localhost:3000"]` | Browser origins allowed to call the API (JSON list) |
| `ACCESS_TOKEN_TTL_SECONDS` / `REFRESH_TOKEN_TTL_DAYS` | 900 / 30 | Token lifetimes |
| `LOG_JSON` | `true` | Set `false` for human-readable local logs |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | empty | Set `http://jaeger:4318` and run `make obs-up` to collect traces |
| `PLACES_PROVIDER` | `mock` | `google` uses Places API (New); set `GOOGLE_PLACES_API_KEY` in `.env.secrets` |
| `PUSH_PROVIDER` | `mock` | `mock` records pushes (read them at `GET /v1/me/devices/test-pushes`). For `fcm`, mount the Firebase service-account JSON into notification-service and set `FCM_CREDENTIALS_FILE` to its path |

Each service receives only the variables it needs. The `environment:` block of each service in
`docker-compose.yml` is the definitive list.

## 5. Everyday commands

```bash
make help                         # everything below, with descriptions
make ps                           # status (the *-migrate containers show "Exited (0)": that's success)
make logs                         # all logs;  make logs-auth-service  for one
make up                           # after code changes: rebuilds only what changed
docker compose --env-file .env --env-file .env.secrets up -d --build user-service   # one service
make down                         # stop, keep data
make clean                        # stop and DELETE all local data (fresh databases next time)
make obs-up                       # Jaeger :16686, Prometheus :9090, Grafana :3001
make contracts                    # regenerate contracts/ (OpenAPI + event JSON Schemas) after an API change
make api-docs                     # ...and the API workbook (docs/api/CoGo-API-Reference.xlsx)
```

## 6. Run one service natively (hot reload)

Keep the infrastructure in Docker and run the service you're working on from source. For
auth-service:

```bash
make up                                   # infra + everything else
cd services/auth
uv sync                                   # first time: creates .venv with Python 3.13
sec() { grep -E "^$1=" ../../.env.secrets | cut -d= -f2-; }
export ENV=development LOG_JSON=false \
  DATABASE_URL="postgresql+asyncpg://auth_svc:$(sec AUTH_DB_PASSWORD)@localhost:5433/authdb" \
  REDIS_URL="redis://localhost:6380/0" \
  RABBITMQ_URL="amqp://cogo:$(sec RABBITMQ_PASSWORD)@localhost:5672/" \
  JWT_PRIVATE_KEY_FILE=../../secrets/jwt_private_key.pem \
  OTP_HMAC_KEY="$(sec OTP_HMAC_KEY)" FIREBASE_PROJECT_ID=cogo-d1d73
uv run uvicorn app.main:create_app --factory --reload --port 8001
# Interactive docs: http://localhost:8001/docs  (only when ENV=development)
```

For **user-service**, use `user_svc` / `USER_DB_PASSWORD` / `userdb`, `RABBITMQ_URL` as above,
and `AUTH_JWKS_URL=http://localhost:8001/.well-known/jwks.json`. That URL needs auth running
natively, because the compose auth-service isn't published. Stop the compose copy of the
service first (`docker compose ... stop user-service`) so two consumers don't share the queue.

## 7. Database migrations

Each service owns its own Alembic history in `services/<svc>/migrations`. `make up` applies
pending migrations automatically through the one-shot `auth-migrate` / `user-migrate`
containers, and `make migrate` runs them again on demand.

To create a migration after changing `app/models.py`:

```bash
cd services/user
export DATABASE_URL="postgresql+asyncpg://user_svc:$(grep ^USER_DB_PASSWORD= ../../.env.secrets | cut -d= -f2-)@localhost:5433/userdb"
uv run alembic revision --autogenerate -m "add home_city to profiles"
# Review the generated file (autogenerate misses enum drops, data moves and renames), then:
uv run alembic upgrade head
uv run alembic check          # "No new upgrade operations detected" = models and DB agree
```

Rules:
- Never edit a migration that has been merged.
- Make every `downgrade()` work. Check it locally with
  `uv run alembic downgrade -1 && uv run alembic upgrade head`.
- Prefer expand/contract for live tables: add a column, backfill, then drop the old one.

## 8. Tests and lint

```bash
make test     # every project; needs Docker (testcontainers starts Postgres/Redis/RabbitMQ/Mailpit)
make lint     # ruff check + ruff format --check + mypy --strict, per project
cd services/auth && uv run pytest -q -k refresh    # one project, filtered
```

| Project | What the tests cover |
|---|---|
| libs/common | Error envelope, request ids, token verification, JWKS caching/rotation, outbox relay against real Postgres + RabbitMQ (including unroutable messages and concurrent relays) |
| services/auth | OTP limits/lockout/concurrency, account linking both ways, refresh rotation + reuse detection, logout(-all), Firebase token rules, migrations |
| services/user | IDOR impossibility, public projection, PATCH semantics/validation, idempotent event consumption, DLQ for poison messages |
| services/notification | Template escaping, dedupe, expiry, retry vs dead-letter, real SMTP delivery to Mailpit |
| services/api-gateway | Route allowlist, raw-path traversal, per-IP/per-user limits, body caps, upstream failure mapping, CORS, header hygiene |
| services/journey | Journey rules and lifecycle, feed ranking and filters, engagement, share pages, Places; matching end to end (scores, mutual eligibility, blocks, hides, moderation, push cap, paging, concurrent recomputes, the sweeper) |
| services/social | Follows, blocks, reports and auto-hide |
| services/chat | Masking (Indian phone formats, emails, links), every request rule and race, accept as one transaction, unread counts under concurrent send/read, receipts, mute, clear, block and journey consumers; chat-ws on a real server (auth, presence, chat both ways, typing, receipts, idle close) |
| services/notification | Email delivery; devices and preferences; every push path (prefs, presence then and now, mute, dedupe, dead tokens, inactive users, like batching) |

The first run downloads container images, so it takes longer.

**Matching load check.** This starts a throwaway PostGIS container, seeds 100k journeys, times
recomputes (serial and with 8 workers) and writes the report with the candidate query plan:

```bash
cd services/journey && PYTHONPATH=. uv run python scripts/perf_matching.py --journeys 100000 --out ../../docs/perf
```

## 9. Working across the repos

`cogo-backend` is the integration repo. Each service and `libs/common` is its own GitHub repo,
included as a git submodule, and each has its own CI. To change a service:

```bash
cd services/user
git switch -c feature/home-city           # branch inside the submodule
# ... edit, test, commit ...
git push -u origin feature/home-city      # open a PR in user-service

cd ../..                                  # back in cogo-backend
git add services/user                     # record the new submodule commit
git commit -m "Bump user-service: home city"
git push                                  # only AFTER the submodule commit is on GitHub
```

- **Push order:** submodules first, then `cogo-backend`. Otherwise the root points at a commit
  nobody else can fetch.
- `git submodule update --remote --merge` pulls the latest branch of every submodule.
- **Changing `cogo-common`:**
  1. Commit and push it like any submodule.
  2. Bump its pointer here.
  3. Run `uv lock` in each service that should pick it up.
  Services consume it from `../../libs/common`: a uv path source locally, and the `common`
  build context in Docker.
- **CI secrets:**
  - Each service repo needs `COGO_COMMON_TOKEN`, a token that can read `cogo-common`.
  - `cogo-backend` needs `SUBMODULES_TOKEN` to check out every submodule for the integration
    run (`make up && make smoke`).

## 10. Adding a new service

Copy `services/user` (it already has a FastAPI app, the shared error envelope, the token
verifier, a database, Alembic, an event consumer and tests). Then:

1. Create the GitHub repo and add it here: `git submodule add git@github.com:cogoworldindia/<name>.git services/<name>`.
2. `pyproject.toml`: rename it; keep `cogo-common = { path = "../../libs/common" }`.
3. In `infra/postgres/initdb/20-service-databases.sh`, add `create_service_db <db> <role> <password>`.
   The script runs only on an **empty** volume, so `make clean` (or create the role and
   database by hand), and add the password to `.env.secrets.example` and `scripts/init-secrets.sh`.
4. `docker-compose.yml`: add a `<name>-migrate` + `<name>` pair modeled on user-service, with
   only the environment variables it needs.
5. Gateway: add the public route(s) to `services/api-gateway/app/routes.py`, with a rate limit.
6. Events: add a `QueueSpec` to `libs/common/src/cogo_common/events/topology.py` and its
   payload contract to `events/contracts.py`. Publish through the outbox; consume through
   `claim_event`.
7. Add the service to `PY_PROJECTS` in the Makefile, extend `scripts/smoke.py`, and regenerate
   the API workbook (`docs/api/README.md`).

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Cannot reach the Docker daemon` | Start Docker; add yourself to the `docker` group, then log out and back in |
| `run make secrets` error from compose | `.env.secrets` is missing a value. Run `make secrets` |
| A `*-migrate` container exited non-zero | `make logs-auth-migrate`. Usually a bad migration, or the DB volume predates a new role: `make clean` (dev data only) |
| `make up` times out waiting for health | `make ps`, then `make logs-<service>`. Services wait for Postgres, Redis and RabbitMQ health first; RabbitMQ can take ~20 s on a cold start |
| Port already allocated | Something on the host uses 8000/8025/5433/6380/5672/15672. Stop it, or change the left side of that `ports:` mapping |
| 429 while testing by hand | Gateway limits are real (5 OTP sends/min per IP). Wait, or clear the dev counters: `docker compose --env-file .env --env-file .env.secrets exec redis sh -c 'redis-cli -n 2 --scan --pattern "gw:rl*" \| xargs -r redis-cli -n 2 del'` |
| `otp_cooldown` | One code per email per 60 s, by design. Use another address or wait |
| No email in Mailpit | `make logs-notification-service`; check the `notification.email.dlq` depth in the RabbitMQ UI |
| `401 invalid_refresh_token` after `make clean` | Sessions lived in Redis, which was wiped (the signing key in `secrets/` is kept). Sign in again |
| `503 provider_disabled` on `/v1/auth/google` | `FIREBASE_PROJECT_ID` is empty in `.env` |
| Tests fail with Docker errors | testcontainers needs the Docker socket. Run `docker info`. On rootless or remote Docker, set `DOCKER_HOST` |
| A socket closes with `4401` | The access token in `?token=` is invalid or expired (15 min). Refresh, then reconnect |
| A socket closes with `1013` or `1012` | chat-ws is unreachable or restarting: reconnect with backoff, then `GET /v1/conversations?since=` |
| No pushes in dev | `PUSH_PROVIDER=mock` records them instead: `GET /v1/me/devices/test-pushes` (after `POST /v1/me/devices`) |
| A Matches banner stays "Finding matches…" | The scheduler sweep re-runs `pending` journeys after a minute. If it persists: `make logs-journey-service` and check the `journey-service.dlq` depth |
| Start completely fresh | `make clean && make up` (**deletes** local Postgres/Redis/RabbitMQ data) |

## 12. Before production

- Terminate TLS at the load balancer. Set `GATEWAY_FORWARDED_ALLOW_IPS` to its address/CIDR,
  otherwise rate limiting sees one IP for everyone (or trusts spoofed headers).
- Store secrets in a secrets manager (not `.env` files) and mount the JWT key as a file.
- Plan signing-key rotation:
  1. Publish the old public key via `JWT_ADDITIONAL_PUBLIC_KEY_FILES`.
  2. Switch the private key.
  3. Drop the old public key after 15 minutes.
- Use managed Postgres (backups, PITR, read replicas) behind PgBouncer
  (`statement_cache_size=0`). Size pools so `replicas × (DB_POOL_SIZE + DB_MAX_OVERFLOW)` fits.
- Run separate Redis instances for sessions (persistent, `noeviction`, non-cluster) and for
  counters/cache.
- Use a managed RabbitMQ cluster (quorum queues need 3 nodes for HA), and alert on DLQ depth.
- Point the SMTP settings at a real relay (SES/SendGrid/Postmark) with SPF, DKIM and DMARC on the
  sending domain.
- Set `ENV=production` (disables docs, enables HSTS) and restrict `CORS_ALLOW_ORIGINS`.
- Run several replicas per service. Scrape `/metrics` (gateway on `:9100`, which must not be
  public) and ship traces through `OTEL_EXPORTER_OTLP_ENDPOINT`.
