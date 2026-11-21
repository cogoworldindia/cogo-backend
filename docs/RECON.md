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
| D27 | Gateway does WebSocket upgrade passthrough for `/v1/ws` | Custom gateway (D6) | The gateway proxies `/v1/ws` to chat-ws: it rejects a bad token in the URL at the edge, limits connects (30/min per user, per IP before auth), relays frames and passes close codes through. In production an L7 load balancer can route `/v1/ws` straight to chat-ws. chat-ws verifies the token again and owns the 120 s idle timeout. |
| D28 | Push `chat.message_created` only if the recipient has no presence key | — | Presence is also **snapshotted at send time** (`recipient_online` in the event). The worker sees the event only after the outbox relay, so someone who read a message on the socket and closed the app would otherwise get a push for it (found by the smoke test). Suppressed if online then *or* now. |
| D29 | FCM through the Firebase Admin SDK | — | FCM **HTTP v1 called directly**: the OAuth token is minted from the service-account key with PyJWT. It's the same API the SDK wraps, and stays async (no thread pool, no extra dependency). |
| D30 | chat-ws calls chat-api with a service token | Service identity per service (D7) | chat-ws has its **own key** (`iss=chat-ws`). chat-api's `/internal/messages`, `/internal/receipts` and `/internal/conversations/{id}/members` accept only it. `make secrets` generates it. |
| D31 | Unread = messages after `last_read_message_id` | — | Kept as `conversation_members.unread_count`: +1 in the send transaction, recounted on read after locking the member row, 0 on clear. The badge is one indexed SUM. A test races sends against reads, and fails without the lock. |
| D32 | A block "deletes ... any pending requests in the pair" | — | They're marked `expired` (with `chat.request_expired`) instead of deleted, which keeps the history for admins. The rest is as specified: the connection is deleted and the chat becomes read-only (`403 blocked`). An unblock doesn't reconnect anyone. |
| D33 | `/internal/messages` falls back to social `/internal/blocks/check` while the projection catches up | — | Not implemented: chat-service's block projection is authoritative (lag < 2 s). A send in that window still fails as soon as the event lands, because the connection is deleted. Revisit if product wants the stricter rule. |
| D34 | "Batches likes in a 10-minute window" | — | The first like on a trip pushes at once. Later likes in the window become one "X and N others also liked your trip" when it closes (a background flusher; one replica claims each window via `ZREM`). Config `notifications.like_window_minutes`. |
| D35 | Realtime state in Redis | One dev Redis, logical DBs | Pub/sub, presence and message rate limits use a **realtime** Redis (db 6 in dev), shared by chat-api, chat-ws and the notification worker. In production use a separate instance. Pub/sub channels aren't per-DB, so only presence needs the shared DB. |
| D36 | `user.kyc_level_changed` carries `id, kyc_level, version` | — | It carries the **full user snapshot** (the `user.updated` schema) plus `previous_kyc_level`. Consumers apply it like `user.updated`. A thin event bumping `version` would make a late `user.updated` (older version, other fields) be skipped, losing that change. Still only the level is KYC data on the bus (spec §5.9). journey, chat and social bind it; notification doesn't need it. |
| D37 | Config via `cogo_common.config`, falling back to Redis | Each service read `config:*` from its own Redis DB | **One shared config Redis** (`CONFIG_REDIS_URL`, db 7 in dev) for every service, so admin-service (M7) writes a key once. Overrides merge into the defaults key by key, nested objects included, so `{"gates": {"create_journey": "full"}}` changes one gate. Dev re-reads every 5 s (`APP_CONFIG_TTL_SECONDS`); production keeps 60 s until `config.updated` lands in M7. |
| D38 | "One aggregator implementation (configurable)" | — | **Cashfree Secure ID (DigiLocker)**: the only candidate whose public docs cover session, webhook and identity fetch (Setu documents no webhook; Digio's schemas aren't readable without login). `app/kyc/cashfree.py` follows the API reference ([DigiLocker API](https://www.cashfree.com/docs/api-reference/vrs/v2/digilocker/create-digilocker-url), [webhooks](https://www.cashfree.com/docs/api-reference/vrs/v2/digilocker/webhooks-digilocker), [signature](https://www.cashfree.com/docs/api-reference/vrs/webhook-signature-verification)) and is tested against a stub. **Before go-live, check against a sandbox delivery**: the docs don't state the redirect's query parameter, the retry schedule or the DOB format (we accept DD-MM-YYYY and ISO), and one page puts the signature in the body rather than the header. Production needs an https `KYC_RETURN_URL` (an app link; `cogo://` is refused) and Cashfree's 2FA (IP allow-list, or `KYC_CASHFREE_PUBLIC_KEY_FILE` for `x-cf-signature`). Aadhaar comes back masked; PAN and DL numbers are cut to the last 4 in memory. |
| D39 | The webhook "enqueues work for user-worker" | — | The result is **applied inside the webhook request**: one short transaction, idempotent per session. The payload (or Cashfree's document fetch) holds the holder's name and DOB, which must not be stored or queued (spec §5.9), so they're compared in memory and dropped. Cashfree's consent lasts an hour, so the fetch can't wait for a queue. A failed fetch returns 503 and the provider redelivers. |
| D40 | `EmailSender` interface in user-service (SES / SendGrid) | Email goes through notification-service | The official-email code goes through **notification-service** (template `kyc_email_otp`), like sign-in codes: one SMTP integration. It's published directly, not via the outbox, so the plaintext code is never written to a table. A failed publish deletes the attempt, so it doesn't count toward 3 sends/hour. Codes are stored as HMACs (`KYC_OTP_HMAC_KEY`). |
| D41 | Prompt shows if level ≠ full and not snoozed; name/DOB read-only after a verified ID | — | Both also apply **while an ID awaits review**: the prompt is hidden (nothing more for the user to do) and name/DOB are locked, so the profile can't be edited to fit the ID mid-review. Admin approve/reject is M7. |
| D42 | — | — | **One account per verified official email** (partial unique index). `email_in_use` is returned only at verify time, after the user has proved they own the address, so it can't be used to probe which emails are registered. |
| D43 | Mock KYC provider: "auto-verify / mismatch / fail by test phone number" | No phone numbers (D3) | The mock's page offers the three outcomes as buttons. It then calls the real webhook through the gateway with a signed payload. It's mounted only when `KYC_PROVIDER=mock`, and refused in staging and production. An unknown gate value in config fails closed (treated as `full`). |
| D44 | `UserSummary` carries `kyc_level` everywhere | social's lists had no level | social-service's projection gains `kyc_level` (migration 0003) so follower, following and block lists show the verified badge. Existing rows start at `none` and catch up on the user's next event. |
| D45 | `config.updated` → "all services refresh config cache" | One durable queue per service (competing consumers) | A queue gives each message to **one replica**, so it can't invalidate every replica's cache. admin-service publishes each change to the shared config Redis and on its `config:updated` **pub/sub channel**; every replica's `AppConfig` listens and drops the key at once (the TTL still covers a missed message). `config.updated` also goes on the bus (outbox) for the record. |
| D46 | Admin auth: email + password + TOTP | — | argon2id passwords; TOTP secrets encrypted at rest (`ADMIN_TOTP_ENCRYPTION_KEY`); each code accepted once; 5 failures lock the email for 15 min. Sessions are **opaque tokens in Redis** (8 h, revocable on logout), not JWTs: admins are few and revocation matters more than statelessness. The gateway allows `/admin/*` only from `ADMIN_ALLOWED_CIDRS` (404 otherwise) and doesn't check admin tokens (admin-service does). Accounts come from `python -m app.cli create-admin`; dev creates a bootstrap admin from `.env.secrets`. |
| D47 | Roles `moderator` and `admin` | — | moderator: reports (dismiss, remove, warn, suspend), users (view, suspend, reinstate, warn), KYC reviews, journeys. admin: all of that, plus **ban**, config and the audit log. |
| D48 | Report actions: dismiss · remove · warn · suspend · ban | — | admin-service acts on the target through its owner first, then social-service closes **every open report on that target** (grouped in the queue). dismiss restores an auto-hidden target; remove keeps it hidden and the owner deletes or hides it; warn, suspend and ban leave its moderation state as it is. If a report didn't name the owner (`target_owner_id` is optional, spec §5.10), admin-service asks the target's service. |
| D49 | "warn (system notification)" | — | A push of type `account_warning` through notification-service's `/internal/admin/warnings`: logged, and **not** switchable off by preferences (it's about the account). |
| D50 | `user.status_changed {id, account_status, suspended_until}` | — | The full snapshot plus `previous_status` and `suspended_until` (as D36). auth-service also consumes it: a suspension or ban refuses sign-in and ends every session (auth maps both to `suspended`). user-service lifts suspensions when they run out (a job every minute; SKIP LOCKED, safe on several replicas). Access tokens already issued live out their ≤ 15 min, but the user's content is hidden at once. |
| D51 | `DELETE /me` "requires an OTP re-confirm via auth-service" | — | `POST /v1/auth/reauth/email/send` + `/verify` (codes kept apart from sign-in codes) issue a 5-minute token with its own audience (`cogo-reauth`, `purpose: delete_account`), sent as `X-Reauth-Token`. user-service anonymises its row ("Deleted user"; media, bio, tags and KYC cleared; photos deleted from storage) and emits `user.deleted`. |
| D52 | "Each service emits nothing further" on `user.deleted` | — | Two exceptions keep other users' data right: social-service emits `social.unfollowed` for each removed follow (their follower counts), and chat-service's expired requests emit `chat.request_expired` (analytics). Projections write the deleted user with version 2^62 (`DELETED_USER_VERSION`), so a stale `user.updated` can't bring them back. auth anonymises the account and drops its identities, so the email can sign up again as a **new** account. |
| D53 | Deletion tracking: "polling `/internal/admin/deletion-status/{user_id}` on each service, or alerts after 24 h" | — | user-service's job polls auth, journey, social, chat and notification until all report done, then sets `deletion_completed_at`; past 24 h it logs `account_deletion_incomplete` (error, hourly per user) for alerting. |
| D54 | Cloud Vision SafeSearch in `user-worker` | Consumers run in the API process (D17) | Confirm marks the upload usable at once and queues `media.uploaded` to user-service itself. The consumer runs SafeSearch (REST `images:annotate`, API key; a mock flags test images) and, at adult or violence LIKELY or above, takes it down (avatar cleared so the app shows its default, cover removed) and emits `media.flagged`; social-service opens a **system report** (reporter null). A provider outage retries and then dead-letters (alert); the photo stays up meanwhile. The flagged file is kept for the admin's review. |
| D55 | admin-web (React + Vite + TS) | — | Built in a Node stage of admin-service's image and served by admin-service at `/admin/console/` (one origin, no CORS) under a strict CSP. Sessions live in `sessionStorage`. Source in `services/admin/web/` (49 component and client tests). |
| D56 | Database per service | Databases were created only on an empty volume | The init script is idempotent and also runs as a one-shot `db-provision` container before every migration, so a new service (admindb) gets its database on an existing volume. |
| D57 | App Check on `/v1/places/*`, `/v1/me/kyc/*`, `/v1/connection-requests` and the OTP endpoints, verified in cogo-common (§9) | The spec's table says gateway + services | Verified by cogo-common's `AppCheckVerifier`, **at the gateway only**: clients can't reach a service except through it (the chart's NetworkPolicies), so a second check per service adds nothing. Per route (`app_check` in the route table; also re-auth, which sends codes): Firebase's RS256 tokens in `X-Firebase-AppCheck`, keys cached 6 h. `APP_CHECK_MODE` off / log / enforce: staging starts in **log** (counts `gateway_app_check_total{result}` and an alert while clients roll out), then enforce, which refuses with 401 `app_check_failed`. If Firebase's keys can't be fetched the request passes, counted as `unavailable`: an anti-abuse signal mustn't take sign-in down. |
| D58 | Nightly maintenance in every service (§7) | — | **Hourly**, in-process (`cogo_common.maintenance`, `MAINTENANCE_ENABLED`), each step batched (5000 rows) and replica-safe: inbox rows past 14 days, published outbox rows past 7, notification log past 30; counters recounted where a service keeps them (journey likes/comments/shares, chat unread) with `FOR UPDATE SKIP LOCKED` so a concurrent write waits for a correct count. Also feeds the `outbox_pending` / `outbox_oldest_pending` gauges every 30 s. In the chart, workers run it and APIs only keep the gauges. |
| D59 | Sentry (§1A.2) | — | Optional, on when `SENTRY_DSN` is set: no default PII, no request bodies, no frame locals, traces sampled at `SENTRY_TRACES_SAMPLE_RATE` (0 by default). |
| D60 | Dashboards and alerts (§11, M8) | Prometheus + Grafana in compose's `observability` profile | OTel metrics on each service's `/metrics` (gateway on `:9100`). 13 alert rules in `infra/prometheus/rules/cogo.yml` (service down, gateway 5xx rate, read p95, DLQ above 0, a queue without consumers, backlog, stuck outbox, projection lag, handler failures, push failures, overdue deletions, Places spend, App Check failures) and three Grafana dashboards generated by `infra/grafana/dashboards.py` (services, events, product). |
| D61 | Helm charts for staging; KEDA for workers (M8) | — | `deploy/helm/cogo`: one chart, one template per role (gateway, api, worker, ws, scheduler). APIs scale on CPU (HPA), workers on RabbitMQ queue length (KEDA), chat-ws on CPU with a long drain. PDBs, NetworkPolicies, migrations as hook Jobs straight to Postgres, ServiceMonitors. `make chart` lints it and validates every manifest with kubeconform (defaults, staging, staging + PgBouncer). Placeholders in `values-staging.yaml` are marked `REPLACE-ME` and the chart refuses unsafe ones. |
| D62 | PgBouncer "if needed" (M8) | — | **Not needed at staging scale**: about 45 replicas × (5 + 5) stays under a managed instance's `max_connections`. The chart can deploy PgBouncer 1.26 in transaction mode with `max_prepared_statements`, which keeps asyncpg's prepared statements working; with it at 0 (or another pooler without them) services get `DB_PREPARED_STATEMENTS=false` (no statement caches, unique statement names). |
| D63 | Load tests to the §11 targets on staging (§12) | No staging cluster yet | k6 scripts in `loadtest/` and `make load-*` targets, run here against the local stack (`make load-up`: no rate limits, 4 h tokens, extra uvicorn workers standing in for replicas). Results and per-process capacity in `docs/perf/load.md`; the staging run with these scripts is the remaining part of M8's done-criteria. |
| D64 | Feed: one query ranking every candidate (§5.5, §11) | — | Kept, and made cheaper per candidate (native tag similarity, inlinable SQL functions, one hashed block check, JIT off, extended statistics; `docs/perf/feed.md`). The cost still grows linearly with candidates (query p95: 103 ms at 10k upcoming journeys in the 90-day horizon, 220 ms at 30k, 742 ms at 100k), so the 300 ms budget runs out at about 35–40k candidates. **Open for product** before that size: bound the candidate set (a shorter horizon or a radius for the unfiltered feed, or two-stage retrieval). |
| D65 | Accessibility pass (M8) | The mobile screens aren't built here (§4) | The admin console: WCAG 2.1 AA checks (axe-core on every page, a contrast test over the stylesheet's tokens), focus management on navigation and confirm steps, live regions, labelled errors, 320 px reflow. The mobile app's pass belongs with its screens. |
| D66 | Logs never carry credentials (§0.7) | — | Every log line goes through a redaction step for query-string credentials (`token`, `access_token`, `code`, …), tracebacks have no frame locals, and httpx's per-request INFO lines are off. Tests in cogo-common. |
| D67 | Process model | — | One uvicorn process per pod; scale with replicas (`WEB_CONCURRENCY=1` in the chart). Each process lifts its open-file soft limit to the hard one at start (Docker's default 1024 capped a socket server at about 1000 clients), and uvicorn's listen backlog is 4096 where sockets connect in bursts (gateway, chat-ws). |

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
| M3 | Feed (ranked, filtered, stable cursors) + likes/comments/shares/hides + `/j/{id}` preview and app links; social blocks, reports, auto-hide; block and moderation consumers in journey- and user-service. **Done.** Deferred: cover/avatar moderation targets in user-service (M7, with image moderation). (`connection_status` in DTOs landed in M5.) |
| M4 | Matching worker in journey-service: `journey_matches` (directional), candidate retrieval on both GiST indexes, Python scoring, ordered row locks, banners, `journey.match_found` with a daily cap, a scheduler sweep, `GET /v1/journeys/{id}/matches` with filters and keyset paging, and a load check (`docs/perf/matching.md`). **Done.** Deferred: `GET /internal/journeys/{id}/join-check` (M5, its first caller is chat-service); `seats_taken` from `chat.request_accepted` (M5); the Matches screen itself (mobile, see §4). |
| M5 | **chat-service** (new repo): requests with join-check, snapshots and masking; connections; conversations, messages, receipts, mute, clear, badge; internal relations and socket endpoints; consumers (projections, blocks, journey lifecycle, moderation). **chat-ws**: the §6.3 protocol, Redis pub/sub fan-out, presence, idle timeout, graceful close. **notification-service**: database, device tokens, preferences, push worker (prefs, presence, mute, dedupe, like batching), mock + FCM HTTP v1 providers. journey-service: `/internal/journeys/{id}/join-check`, seats from `chat.request_accepted`, `connection_status` on cards (and in user-service profiles). Gateway: chat and notification routes, `/v1/ws` passthrough. Spec §12 E2E flows 1–2 pass in `make smoke`. **Done.** Deferred: the mobile screens (§4); `/internal/journeys/{id}/summary` (its first caller is admin, M7); the block-check fallback (D33); App Check (M8). |
| M6 | eKYC in user-service: official-email OTP (blocked personal domains, 3 sends/hour, 5 attempts, 10 min) and government ID through the `KycProvider` interface (mock + Cashfree DigiLocker) with a signed webhook and name + DOB matching (verified, or review); the derived `kyc_level` and `user.kyc_level_changed`, consumed by journey (create gate, `verified_only`, matching), chat (request and chat gates) and social (badge); the `/me` prompt, snooze, `/v1/me/kyc` status for polling, and the name/DOB lock. Shared config Redis, so a gate change applies without a deploy. Gateway routes for `/v1/me/kyc/*`, `/v1/webhooks/kyc/*` and the mock page. `make e2e` covers the whole flow (190 checks, 67/67 endpoints). **Done.** Deferred: EkycSheet, EkycFlow and CoachMarks (mobile, §4; the API is ready, and coach marks use `POST /me/guide/seen` from M1); admin approve/reject of reviews (M7); App Check on `/v1/me/kyc/*` (M8); a Cashfree sandbox run (D38). |
| M7 | **admin-service** (new repo): admin sign-in (password + TOTP, opaque sessions, lockout), roles, app config as the source of truth (validated, published to the shared config Redis with a broadcast, `config.updated`, public `GET /v1/config` with ETag), the console's API (reports with previews and resolve, users, KYC reviews, journeys, audit log) through the owners' `/internal/admin/*`, and the React console. Owners: user (status changes and expiry, KYC review, photo takedown, `DELETE /v1/me` + deletion tracker, image moderation), journey (summary, remove journey/comment, deletion), social (queue, resolve, system reports, deletion), chat (reported messages, deletion), notification (warnings, deletion), auth (re-confirm codes, suspensions and deletion). Gateway: `/v1/config`, `/admin/*` behind an IP allow-list. Spec §12 flows 3 (block) and 4 (deletion) pass in `make e2e` (233 checks, 90/90 endpoints). **Done.** Deferred: a Cloud Vision run with a real key; admin user management in the console (CLI for now); App Check (M8). |
| M8 | Hardening: App Check at the gateway (D57); hourly maintenance and counter reconciliation (D58); Sentry (D59); domain metrics, 13 alert rules and three dashboards (D60); the staging Helm chart with HPA and KEDA, and optional PgBouncer (D61, D62); k6 load tests and a 1-hour soak, run locally: §11 latency targets met, DLQs empty after the soak (D63, `docs/perf/load.md`; 0.09% errors in one minute when the laptop ran out of memory); the feed's ranking made about 6× cheaper per candidate (D64, `docs/perf/feed.md`, EXPLAIN plans in `docs/perf/plans.md`); the console's accessibility pass (D65); log redaction (D66); Schemathesis over every public route (`make fuzz`: 3,909 generated requests, no 5xx after two fixes). Fixed along the way: see §6. **Done here; the staging run remains** (the chart and the load scripts are ready). Open: D64's candidate bound before about 35k upcoming journeys. |

## 6. Incidents found during the build

- **Access tokens in the logs (found in M8 by the socket load test).** uvicorn logs each
  WebSocket handshake at INFO with the full path, and spec §6.3 puts the access token in the
  query string (`/v1/ws?token=…`). The gateway and chat-ws wrote one token per connection.
  Nothing is deployed yet, so only local dev logs held them.
  - Fix: a redaction step on every log line (D66); uvicorn's per-socket "connection
    open/closed" lines dropped.
  - A test logs a handshake, a URL with `?code=` and an exception carrying a token, and checks
    none survive.
- **Stack-frame locals in exception logs (M8).** structlog's JSON tracebacks include each
  frame's local variables by default, which can hold tokens, codes or ID data. Turned off,
  with a test.
- **The feed fell over at 200 rps (M8 load test).** p95 11 s and 73% errors: journey-service's
  pool timed out, because one process was out of CPU and every queued request held a
  connection. The pool wasn't the cause. Per request:
  - httpcore imported `sniffio` (no longer installed with anyio) on every lock it created and
    failed, searching `sys.path` about 8 times a request. Now a cogo-common dependency.
  - Tag similarity ran two set-operation subqueries per candidate (80% of the ranking);
    now intarray (D64). Aliases and the visibility predicate were rebuilt per request.
  - httpx logged two INFO lines per feed page (the relation calls).
  - Then it passed with workers standing in for replicas (`docs/perf/load.md`).
- **WebSockets stopped at about 1000 (M8 load test).** Four separate faults:
  - The containers' soft open-file limit was 1024: chat-ws couldn't accept the rest. Each
    process now raises it at start (D67).
  - The gateway's WebSocket proxy didn't catch an upstream that closed without an HTTP
    answer (`InvalidMessage`): the client got a broken handshake instead of close 1013.
  - Connect bursts overflowed uvicorn's accept queue (backlog 2048): dropped connects retry
    only after 1, 3, 7 s. Backlog 4096.
  - 1000 sockets pinging together emptied chat-ws's Redis pool; redis-py fails at once
    instead of waiting, and the failed presence write closed the socket. `create_redis`
    now waits for a connection (blocking pool), and a presence failure no longer drops a
    socket. Regression tests for each.
- **Service tests ran an old cogo-common (M8).** Services install it as a non-editable path
  dependency, and uv only rebuilds those when `pyproject.toml` changes, so a source-only change
  wasn't tested in the services until a lockfile change happened to rebuild it. The
  library's source files are now uv cache keys.
- **The feed's plan degraded after deletions (M8).** Deleted journeys are always cancelled,
  but Postgres multiplied `status = 'upcoming'` and `deleted_at IS NULL` as if independent.
  With deletions piling up it expected 1 candidate instead of 330 and chose a nested loop
  that rescanned the users for every row (8 ms instead of 3). Extended statistics
  (migration 0006).
- **notification-api pods consumed email (found reviewing the Helm chart).** The email
  consumer was registered whatever `CONSUMERS_ENABLED` said, and the like flusher ran in
  every replica. Both now run on workers only.
- **Two 500s from fuzzing (M8, `make fuzz`).**
  - A NUL character (`\u0000`) in a device token reached Postgres, which can't store it. Any
    text field, query or path can carry one, so cogo-common's error handlers now answer
    Postgres's invalid-text errors (SQLSTATE 22021, 22P05) with 400 `invalid_text`; other
    database errors stay a logged 500.
  - `hmac.compare_digest` raises on a `str` with non-ASCII characters, so a crafted signature
    on the KYC webhooks or the mock KYC page was a 500. The comparisons use bytes.
- **The first soak didn't soak the sockets (M8).** Its listeners took their hold time from
  `sockets.js`'s 2-minute default and closed after about 3 minutes; the rest of the hour's
  messages went out as pushes. Everything passed, which is the problem: `make load-soak`
  now exports `DURATION`, and a delivery threshold (95% of sent messages must arrive)
  fails a run like that.
- **e2e feed checks read only the first page (M8).** With the load-test seed in the dev
  database, Asha's trip ranked past page one for some viewers and "unhide brings it back"
  failed; "...it leaves his feed" could have passed for the same wrong reason. The checks
  now walk every page.
- **A load test took the host down (M8).** A 10k-socket k6 run parsed the seed file once per
  virtual user and ran the machine out of memory. The scripts now share one copy
  (`SharedArray`), k6 runs with a memory cap (`K6_MEMORY`, 3g), and 10k sockets is left to
  staging (3000 passed locally).


- **Admin lists could never fill a page (found in M7 by the e2e).** admin-service asks the
  owners for `limit + 1` items to know if there's a next page, but their internal endpoints
  capped `limit` at 50, so the console's largest page (50) failed. The unit tests used fake
  owners without the cap.
  - Fix: internal list endpoints accept up to 100 (their only caller is admin-service).
  - The owners' tests now call with `limit=51`.
- **`make test` and `make lint` skipped chat-service (M5 to M6).** It wasn't in the Makefile's
  project list. Added (and the console's tests and typecheck too).

- **Config changes would have reached no service (found in M6).** Every service read
  `config:*` from its own Redis DB (journey db4, chat db6, notification db1, social db5), and
  user-service had no Redis at all. An admin change written once would have changed nothing,
  so M6's "a gate change takes effect without a deploy" couldn't hold.
  - Fix: one shared config Redis (D37), and nested overrides merge into the defaults.
  - The e2e test now changes the gates in Redis and checks both journey- and chat-service
    enforce them, then reopen.

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

- **A push for a message already read on the socket (found in M5 by the smoke test).** The worker
  checked presence when it processed `chat.message_created`, which is after the outbox relay.
  A user who read the message live and closed the app got a push for it anyway.
  - Fix: chat-api snapshots presence at send time (D28), and the worker suppresses the push if
    the user was online then or is now.
  - The smoke test and unit tests both cover it.
- **A hide could fail with a 500 under matching load (seen once in tests).** Hide and unhide
  recomputed the viewer's matches inside the request transaction, so lock contention with the
  matching worker could fail the user's request.
  - Fix: the recompute runs in its own transaction after the hide commits, with retries.
  - The Matches list filters hides at read time, so only the banner count can lag.
- **A feed test failed between 18:30 and 24:00 UTC.** It built its date filter from the UTC date,
  while filters are interpreted in IST (spec §11). The test now uses the IST date; the service
  was already right.
