# RECON: current state vs the Phase 1 build spec

Written before Phase 1 work started (2026-11-19), as required by spec §0.1. It records what exists,
where it differs from spec §1A, and how each difference is resolved. Decisions follow the spec's
rule: **match what exists** unless the spec's version is needed for a feature.

## 1. What exists

| Area | Current state |
|---|---|
| Repo layout | Integration repo `cogo-backend` + one GitHub repo per service as **git submodules**: `services/{auth,user,api-gateway,notification}`, `libs/common` (`cogo-common`). The retired Java `services/mail` is still pinned but not run. |
| Language / tooling | Python **3.13**, **uv** (pyproject + uv.lock per project), ruff, mypy `--strict`, pytest + pytest-asyncio + testcontainers |
| Framework / DB | FastAPI 0.164 (Starlette 1.x, native OTel), Pydantic v2, SQLAlchemy 2.1 async + asyncpg, Alembic per service with an async `env.py` |
| Tokens | **JWT**: EdDSA (Ed25519) access tokens, 15 min, claims `sub, sid, iss=cogo-auth, aud=cogo-api, jti`, JWKS at auth `/.well-known/jwks.json`. Opaque rotating refresh tokens with reuse detection. **No service tokens yet.** |
| Login | auth-service: **email OTP** + **Google (Firebase ID token)**, with accounts linked by verified email. **No phone OTP.** |
| Inter-service | RabbitMQ (topic `cogo.events`, DLX `cogo.dlx`, quorum queues + DLQ) via FastStream; transactional outbox + relay; inbox `processed_events`. One event today: `user.registered`. No synchronous internal calls. |
| Gateway | Custom FastAPI gateway (`services/api-gateway`): explicit route allowlist, edge JWT check against JWKS, Redis rate limits, streaming proxy. No WebSocket support. |
| user-service | `users(id, status)` + `user_profiles(user_id PK, full_name, date_of_birth, gender, bio, profile_photo_url)`. API: `GET /v1/users/me`, `PATCH /v1/users/me/profile`, `GET /v1/users/{id}`. |
| notification-service | Email-only worker (Jinja2 + SMTP/Mailpit). No device tokens, prefs or FCM. |
| Local dev | `docker-compose.yml` + Makefile: PostGIS 17-3.5, Redis 7, RabbitMQ 4, Mailpit; migrate containers; `make up/test/lint/smoke` |
| Error / list format | Envelope `{status, code, message, data, error:{type, request_id, details?}}`; `error.type` lower_snake. No list convention yet. |
| Mobile (`../CoGo/frontend-app`) | React Native 0.82, React Navigation (a **single stack**; bottom tabs installed but unused), zustand, axios, Keychain tokens, react-i18next (en, hi), Firebase Analytics + Messaging. Built: Onboarding, email Login → OTP → ProfileSetup, a placeholder Home, a local-only Notifications list. Most of it is **uncommitted work in progress**. Theme tokens in `src/theme/Theme.js` (brand `#1C84C8`, primary `#007AFF`, system fonts). It already calls the refactored API (`/v1/auth/*`, `/v1/users/me`, `/v1/users/me/profile`) and handles token refresh and rotation. |

## 2. Differences from spec §1A, and decisions

| # | Spec says | Here | Decision |
|---|---|---|---|
| D1 | Monorepo `cogo/services/*-service` | Submodules, `services/<name>` | **Keep submodules.** New services become new repos/submodules: `services/journey`, `services/social`, `services/chat`, `services/admin`. Shared `contracts/` lives in the integration repo. |
| D2 | Python 3.12 | 3.13 | Keep 3.13 (already in images and CI). |
| D3 | Phone OTP login exists | Email OTP + Google | **No phone OTP in this pass.** It needs an SMS provider (MSG91/Twilio) and DLT registration in India. To add later behind an `SmsSender` interface, with the identity model unchanged (a new `phone` provider). The spec's `users.phone` is not added until then. **Open question for product.** |
| D4 | Error body `{error:{code,message,details}}` and lists `{items,next_cursor}` | Envelope with `error.type` | **Keep the envelope.** It's the existing convention and the app depends on it. Spec codes map to lower-case `error.type` (`PENDING_EXISTS` → `pending_exists`). Lists are `data: {items, next_cursor}` (`cogo_common.responses.Page`). |
| D5 | Event envelope `event_id`, `trace` fields | `id`; trace context in AMQP headers (`traceparent`, `x-request-id`) | **Keep.** It's semantically the same, and the outbox already propagates headers. JSON Schemas are generated into `contracts/events/`. |
| D6 | Kong or Traefik gateway | Custom FastAPI gateway | **Keep the existing gateway** (spec allows "use an existing gateway"). Extend its route table per §6, and add WebSocket passthrough for `/v1/ws` in M5. It forwards `Authorization` instead of `X-User-Id`; services re-verify (the spec's defence in depth). |
| D7 | Service JWT for `/internal/*` | none | **Added in M0:** a per-service Ed25519 key, `iss=<service>`, `aud=<target>`, 5 min (`cogo_common.auth.service_tokens`). Keys come from `make secrets`. |
| D8 | `/healthz`, `/readyz` | `/health/live`, `/health/ready` | Both are served (aliases added in M0). |
| D9 | Single `users` table with profile columns | `users` + `user_profiles` | **M1 migrates to the spec's single-table shape** (users gains username, name, dob, gender, bio, avatar_url, city, tags, counters, kyc, version…), moving data from `user_profiles`. |
| D10 | `/v1/me` endpoints | `/v1/users/me`, `/v1/users/me/profile` (used by the app) | **Add the spec's `/v1/me` surface** and keep the old two endpoints as **deprecated aliases** until the app migrates (tracked in §4). |
| D11 | OpenAPI as `contracts/openapi/<service>.yaml` | — | Generated as `.json` (no YAML dependency); CI diffs it. |
| D12 | APScheduler + Redis lock for journey-scheduler | — | As spec: one process, with a Redis lock per job. |
| D13 | Sentry | — | Deferred to M8 (hardening). Structured logs and OTel are already in place. |
| D14 | `deploy/docker-compose.dev.yml` with hot reload | Root `docker-compose.yml`, image-based | Keep the root compose file. Hot reload is documented as running one service natively (`docs/SETUP.md` §6). |
| D15 | Admin web (React) | — | M7. |
| D16 | `users.city_location geography` in user-service | — | user-service stores `city_lat`/`city_lng` floats. It never runs spatial queries, and PostGIS isn't a trusted extension (it needs superuser). journey-service projects the city into a geography column. |
| D17 | Separate `user-worker` / `*-worker` deployables | Consumers run inside each API process | Consumers live in the API process (same image) for now. Splitting them into worker deployables is a deployment change (same code, different command), planned with KEDA in M8. |
| D18 | S3/GCS + MinIO for dev media | MinIO stopped publishing images (2025) | Dev uses **RustFS** (S3-compatible, Apache-2.0) at `localhost:9000`. The code is plain S3 (boto3 presigned PUT), so production uses S3/GCS + CDN unchanged. |
| D20 | `block_projection (blocker, blocked, source_version)` | — | Adds `active`: an unblock keeps a **tombstone** with its version, so a stale block event can't resurrect a lifted block. |
| D21 | Vibe Match "computed in SQL" | — | SQL functions `cogo_vibe`, `cogo_tag_sim`, `cogo_falloff` (journey-service migration 0003) are used for feed ranking. A test proves they equal `cogo_common.scoring`, so ranking and the displayed numbers agree. |
| D19 | Event `trace` field | — | Trace context and the request id travel as AMQP headers set by the outbox (see D5). |
| D22 | Matching takes `pg_advisory_xact_lock(hashtext(journey_id))` | — | **Row locks on every affected journey, taken in id order.** Matching A also rewrites partner B's rows and banner counter. A per-journey advisory lock lets two replicas refreshing A and C overwrite B's counter with stale counts (a regression test reproduces the lost update). Ordered row locks serialise exactly the journeys whose rows change, and they can't deadlock. The worker plans once to find the set, locks it, then plans again under the lock. |
| D23 | `matching-worker` is its own deployable | Consumers run in the API process (D17) | The worker runs in journey-service's consumer, on the same queue as its projections. The inbox (`processed_events`) is keyed by event id, so two queues in one database would collide; one queue also keeps a block's projection update and its match purge in a single transaction. A scheduler sweep re-runs any journey still `pending` after a minute (the safety net for a dead-lettered event). Splitting the worker out later is the D17 change plus a composite inbox key. |
| D24 | `journey_matches` rows for valid pairs | — | Rows are **directional**: (A, B) exists only if A's host may see B now (B not moderation-hidden, not full, not hidden by A's host). The banner count then equals what the Matches list shows. The list still re-applies these filters at read time, so a row the worker hasn't caught up with yet can't leak. |
| D25 | Emit `journey.match_found` "for each B whose owner should be told"; 3 per journey per day | — | A new row (X, Y) notifies X's host only when Y is the newcomer: Y was just posted or re-routed (`pending`) and X wasn't, or both were and X is older. The result doesn't depend on which refresh runs first, and unblocks or eligibility changes notify nobody. The daily cap is enforced by the producer (Redis counter per journey per IST day), so the bus doesn't carry pushes that would be dropped. It fails open if Redis is down. |
| D26 | "Scoring is pure Python over at most a few hundred rows" | — | New config `matching.max_candidates` (default 1000) caps the rows scored per journey, nearest first. New `matching.page_size` (20) for the Matches list. Measured at 100k journeys: [perf/matching.md](perf/matching.md). |

## 3. Open questions (spec §14): defaults implemented

| Question | Decision |
|---|---|
| Token format | JWT + JWKS (already the case) |
| Monorepo vs a repo per service | A repo per service (existing) |
| Gateway | The existing custom gateway |
| Broker | RabbitMQ behind the `cogo-common` event API |
| chat-ws in Go | No. Python first, with the protocol and internal API kept stable |
| Nav items | 5 as in Figma (Home, Journey, Post, Chat, Profile) |
| Activities tabs | Upcoming / Completed with "View all" |
| Feed types | Journeys only; typed items |
| Comments | Yes, behind `features.comments` |
| Match formulas | §5.4 defaults, min 50 (config `matching`) |
| Who can join | anyone, women_only, men_only, verified_only |
| KYC gates | All open; prompt cool-down 3 days |
| Official email | Personal domains blocked (config) |
| Full KYC | Gov ID alone = full; email alone = partial |
| Follow vs connection | Independent |
| Rejected request visible to sender | "Not accepted" |
| Seats | +1 per accepted request (via `chat.request_accepted`) |
| Phone OTP (new, D3) | Not in this pass; email + Google only |

## 4. Mobile app: migration notes for the app team

- Keep calling `/v1/users/me` + `/v1/users/me/profile` until moving to `GET/PATCH /v1/me` (the spec DTO). The old endpoints stay until then.
- Logout correctly sends only the bearer token: the session id comes from the token's `sid`.
- The 5-tab shell, Home feed, Filter, Post, Journey, Matches, Chat and Profile screens (spec §8) are **not built**. The backend for them lands milestone by milestone, and the API contracts are committed in `contracts/openapi/`.
- Theme: centralise the spec §2 colours (Journey Match green, Vibe Match blue, who-can-join slate, group-size orange) in `Theme.js`. Confirm the font against Figma (the spec suggests Outfit; the app uses the system font, with `Instrument Sans` in one component).
- **Screens not yet designed** (to be built from existing components, then reviewed by design): Create Journey, Journey Detail, Chat home, Chat thread, Comments sheet, Report/Block sheets, eKYC popup, coach marks, empty states, Settings.

## 5. Milestone plan for this codebase

Follows spec §13, in order. Each milestone ends with migrations, OpenAPI and event contracts, tests (unit + testcontainers integration), compose wiring, gateway routes and a commit per repo.

| # | Scope here |
|---|---|
| M0 | `cogo-common`: service tokens, internal client + breaker, app config, cursors, scoring, rate limits, idempotent consumer, versioned projections, per-service queues + dispatcher, broker start with retries. `contracts/` + generators (`make contracts`). Service keys in `make secrets`. **Done.** |
| M1 | user-service single-table migration, `/v1/me`, personality tags, covers, media upload (RustFS in dev), `user.*` events. social-service follows → counters. Places proxy (journey-service, mock + Google providers). **Done.** |
| M2 | journey-service: journeys CRUD, `journey.*` events, projections, `/me/journeys`, scheduler. user-service `journeys_count`. **Done.** |
| M3 | Feed (ranked, filtered, stable cursors) + likes/comments/shares/hides + `/j/{id}` preview and app links; social blocks, reports, auto-hide; block and moderation consumers in journey- and user-service. **Done.** Deferred: cover/avatar moderation targets in user-service (M7, with image moderation); `connection_status` in DTOs (needs chat-service, M5). |
| M4 | Matching worker in journey-service: `journey_matches` (directional), candidate retrieval on both GiST indexes, Python scoring, ordered row locks, banners, `journey.match_found` with a daily cap, a scheduler sweep, `GET /v1/journeys/{id}/matches` with filters and keyset paging, and a load check (`docs/perf/matching.md`). **Done.** Deferred: `GET /internal/journeys/{id}/join-check` (M5, its first caller is chat-service); `seats_taken` from `chat.request_accepted` (M5); the Matches screen itself (mobile, see §4). |
| M5 | chat-service + chat-ws; notification-service device tokens, prefs, FCM (mock) |
| M6 | eKYC |
| M7 | admin-service, moderation, account deletion |
| M8 | Hardening, load tests, Helm |

## 6. Incidents found during the build

- **Worker running without consumers (fixed in M0).** After a host restart, notification-service
  started before RabbitMQ. FastStream's ASGI wrapper swallowed the connect error, and the worker
  then ran "healthy" with **zero consumers**: OTP emails queued and were never sent.
  - Fix: `cogo_common.events.broker.start_broker` retries, then exits so the orchestrator
    restarts the process.
  - `/readyz` now fails unless every subscriber is consuming.
  - notification-service moved onto the shared FastAPI lifespan.
  - There's a regression test for each part.

- **Events without consumers stuck in the outbox (found and fixed in M4).** The relay published
  every event as `mandatory`, so RabbitMQ returned events that no queue binds yet. In dev,
  `journey.liked`, `journey.commented` and `journey.updated` (which had no consumer before M4)
  were retried until `max_attempts` and never marked published. In production, this would have
  grown an ever-retrying backlog.
  - Fix: only routing keys bound in the declared topology are `mandatory`
    (`cogo_common.events.topology.has_consumers`). A lost binding for a consumed event still
    fails loudly. Events published ahead of their consumer go out without `mandatory`.
  - The four stuck dev rows were reset and flushed.
  - A regression test covers it.
