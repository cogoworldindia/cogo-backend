# CoGo backend

The microservice backend for CoGo, a social platform that connects people whose travel paths
overlap. This repo is the **integration repo**. Each service and the shared library is its own git
repository, included here as a submodule. This repo pins one known-good combination of their
versions and holds the local stack.

```
 Clients ──HTTPS──► api-gateway ─┬─► auth-service ──► Postgres authdb · Redis (OTP, sessions)
  (only public entry)            └─► user-service ──► Postgres userdb
                                        ▲
 auth-service ──outbox──► RabbitMQ (cogo.events) ──┼─► user-service          (user.registered)
                                                    └─► notification-service  (notification.email.requested → SMTP)
```

| Path | Repo | What it is |
|---|---|---|
| `libs/common` | `cogo-common` | Shared infrastructure: logging, error envelope, JWKS/JWT verification, DB/Redis helpers, outbox/inbox, RabbitMQ topology, telemetry |
| `services/api-gateway` | `api-gateway` | Public edge: route allowlist, edge JWT check, Redis rate limits, streaming proxy |
| `services/auth` | `auth-service` | Identity: email OTP, Google (Firebase) sign-in, account linking, EdDSA access tokens + JWKS, rotating refresh sessions |
| `services/user` | `user-service` | Profiles, tags, covers, media uploads; emits `user.*`; consumes `user.registered`, `social.*followed` |
| `services/journey` | `journey-service` | Journeys, home feed, engagement, share pages, Places proxy, matching worker + Matches list |
| `services/social` | `social-service` | Follows, blocks, reports, auto-hide moderation |
| `services/notification` | `notification-service` | Queue-driven email worker (no HTTP send API) |
| `services/mail` | `mail-service` | **Retired** (Java/Gmail). Not started by compose; kept only until its unpushed commit is dealt with |

## Documentation

Phase 1 progress and every difference from the build spec are tracked in [docs/RECON.md](docs/RECON.md).

| Document | For |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Setup, native hot reload, migrations, tests, the multi-repo workflow, adding a service, troubleshooting, production checklist |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, flows (sequence diagrams), data ownership, messaging, security, scalability, Phase 3 |
| [docs/architecture.svg](docs/architecture.svg) ([png](docs/architecture.png)) | Architecture diagram |
| [docs/api/CoGo-API-Reference.xlsx](docs/api/CoGo-API-Reference.xlsx) | API reference per service: routes, endpoints, schemas, events, error codes |
| [docs/perf/matching.md](docs/perf/matching.md) | Matching load check at 100k journeys, with the candidate query plan |
| `contracts/openapi/`, `contracts/events/` | Generated API and event contracts for every service (`make contracts`) |

## Quick start

Prerequisites: Docker (with Compose v2.24+), `make`, `openssl`, and [uv](https://docs.astral.sh/uv/)
for running tests.

```bash
make setup     # submodules + .env/.env.secrets + Ed25519 signing key (idempotent)
make up        # build, migrate, start, wait until healthy
make smoke     # end-to-end check through the gateway
```

| URL | What |
|---|---|
| http://localhost:8000 | API gateway (the only API endpoint clients use) |
| http://localhost:8025 | Mailpit: every email the stack sends (including OTP codes) lands here |
| http://localhost:15672 | RabbitMQ management (user `cogo`, password in `.env.secrets`) |
| http://localhost:9000 | Media store (RustFS, S3-compatible): presigned uploads and served files |
| `localhost:5433` | Postgres (superuser `postgres`; one role per service DB) |

`make help` lists everything. The most used targets are `logs-<service>`, `test`, `lint`, `migrate`
and `clean` (which **deletes local data**). `make obs-up` adds Jaeger (16686), Prometheus (9090)
and Grafana (3001); to get traces, set `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` in `.env`.

## Public API (v1)

All responses use the envelope `{status, code, message, data, error}`. Errors carry
`error.type` (stable, machine-readable) and `error.request_id` (echoed in `X-Request-Id`).

| Method | Path | Auth | |
|---|---|---|---|
| POST | `/v1/auth/email/send` | – | `{email}` → emails a 6-digit code |
| POST | `/v1/auth/email/verify` | – | `{email, code}` → `{access_token, refresh_token, expires_in, user_id, is_new_user}` |
| POST | `/v1/auth/google` | – | `{id_token}` (Firebase Auth, Google provider) → same token pair |
| POST | `/v1/auth/token/refresh` | – | `{refresh_token}` → new pair (the old refresh token becomes invalid) |
| POST | `/v1/auth/logout` · `/v1/auth/logout-all` | Bearer | Revoke this session / every session |
| GET | `/v1/users/me` | Bearer | Your user and profile |
| PATCH | `/v1/users/me/profile` | Bearer | Partial update (`null` clears a field) |
| GET | `/v1/users/{id}` | Bearer | Another user's **public** profile |

> **Breaking changes for existing clients**
> - The `/v1/api/...` prefix is gone; use the paths above.
> - Email and code are now sent in a JSON body, not query parameters.
> - Access tokens expire after 15 minutes, so clients must refresh.
> - The `Authorization` header must be `Bearer <token>`.

## Configuration and secrets

- `.env` holds non-secret settings (template: `.env.example`).
- `.env.secrets` holds passwords and the OTP HMAC key, generated by `make secrets`. It is
  gitignored and never committed.
- `secrets/jwt_private_key.pem` is the Ed25519 key that signs access tokens. It is mounted
  **only** into auth-service as a compose secret. Other services verify tokens through
  auth-service's JWKS.
- Each service receives only the variables it needs (see `docker-compose.yml`), and each
  database has its own login role.

To rotate the signing key:
1. Generate a new key.
2. Mount the old public key into auth-service via `JWT_ADDITIONAL_PUBLIC_KEY_FILES` and swap
   in the new private key.
3. After one access-token lifetime (15 min), remove the old public key.

## Design rules

1. **Database per service.** No service reads another's tables. Cross-service state changes go
   through events.
2. **Events are reliable.** Producers write events in the same transaction as their data
   (transactional outbox). Consumers are idempotent (inbox table or idempotency key). Queues are
   quorum queues with a DLQ.
3. **The gateway authenticates; every service authorises** and re-verifies the token. Identity
   comes only from the verified token `sub`, never from a client-supplied id.
4. **Stateless services.** Horizontal scale = more replicas. Shared state lives in
   Postgres/Redis/RabbitMQ; rate limits are in Redis.
5. **No internal details in responses.** Errors use the shared envelope; logs are structured JSON
   with `request_id`/`user_id` and masked emails.

## Working across repos

Each submodule is its own repo with its own CI (`.github/workflows/ci.yml`: ruff, mypy, pytest
with testcontainers, image build, Trivy, gitleaks). To change a service:

1. Commit inside its submodule.
2. Update the pointer here: `git add services/<name>`, then commit.

Services consume `cogo-common` from `../../libs/common`: through a uv path source locally, and
through a named Docker build context in compose (`additional_contexts: common`).

## Roadmap

Phase 1 follows the build spec's milestones (M0–M8). M0–M4 are done: platform foundations,
profiles and follows, journeys, feed and safety, and matching. Next are chat and push
notifications (M5), eKYC (M6), admin and moderation (M7) and hardening (M8). The plan and its
status are in [docs/RECON.md](docs/RECON.md) §5.
