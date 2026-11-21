# CoGo Helm chart

Deploys the CoGo backend to Kubernetes (EKS or GKE, India region: Mumbai, `ap-south-1` /
`asia-south1`). Postgres, Redis and RabbitMQ are external, managed services. The chart
creates no secret values: it references Secrets you create beforehand (see [Secrets](#secrets)).

`docker-compose.yml` is the source of truth for what each service needs. This chart passes
the same variables, under the same names, plus the role flags below.

## What's deployed

| Deployment | Image | Role | Flags | Scaling |
|---|---|---|---|---|
| api-gateway | cogo/api-gateway | public, Ingress | `METRICS_PORT=9100` | HPA (CPU) |
| auth-api / auth-worker | cogo/auth-service | api / worker | `CONSUMERS_ENABLED` false / true | HPA / KEDA `auth-service` |
| user-api / user-worker | cogo/user-service | api / worker | `CONSUMERS_ENABLED`, `JOBS_ENABLED` false / true | HPA / KEDA `user-service` |
| journey-api / journey-worker / journey-scheduler | cogo/journey-service | api / worker (matching) / scheduler | api: both false; worker: `CONSUMERS_ENABLED=true`; scheduler: `SCHEDULER_ENABLED=true` | HPA / KEDA `journey-service` / 1 replica |
| social-api / social-worker | cogo/social-service | api / worker | `CONSUMERS_ENABLED` false / true | HPA / KEDA `social-service` |
| chat-api / chat-worker | cogo/chat-service | api / worker | `CONSUMERS_ENABLED` false / true | HPA / KEDA `chat-service` |
| chat-ws | cogo/chat-service (`app.ws.main`) | WebSockets | | HPA (CPU) |
| notification-api / notification-worker | cogo/notification-service | api / worker | `CONSUMERS_ENABLED` false / true | HPA / KEDA `notification-service` + `notification.email` |
| admin-api | cogo/admin-service | api, console at `/admin/console/` | | HPA (CPU) |

- **`MAINTENANCE_ENABLED`** (the inbox/outbox purge) is true on every `*-worker` and on
  admin-api, which has no worker. It is false on the other APIs and on the scheduler.
- **Migrations.** A `pre-install,pre-upgrade` hook Job per service (`<release>-<svc>-migrate`)
  runs `alembic upgrade head` with only `DATABASE_URL`, straight to Postgres. Old pods keep
  running against the new schema, so migrations must be expand/contract (docs/SETUP.md §7).
- **Every pod:**
  - Port 8000, probes `/healthz` (startup, liveness) and `/readyz` (readiness).
  - uid 10001, read-only root filesystem with an emptyDir at `/tmp`, all capabilities dropped,
    seccomp `RuntimeDefault`, no service-account token.
  - `WEB_CONCURRENCY` from values. Graceful shutdown: 30 s grace period plus a 5 s `preStop`
    sleep, so endpoints drain before uvicorn's 20 s graceful stop.
  - Spread across zones and nodes.
- **Services.** The gateway, APIs and chat-ws get a ClusterIP Service. Workers and the
  scheduler get a headless one only when ServiceMonitors are on.
- **Optional:**
  - PgBouncer (`pgbouncer.enabled`).
  - ServiceMonitors (`metrics.serviceMonitor.enabled`).
  - `prometheus.io/*` pod annotations (`metrics.podAnnotations`).

Files: `values.yaml` holds the defaults, which lean towards production (`ENV=production`,
real providers). `values-staging.yaml` holds staging. Every Deployment comes from one
template, `cogo.deployment` in `templates/_pod.tpl`. It is fed by `services.<name>`, which
holds the wiring shared by one image, merged with `roles.<role>` defaults and the
deployable's own keys.

## Prerequisites

- **Cluster.** EKS in `ap-south-1` or GKE in `asia-south1`, with:
  - ingress-nginx;
  - cert-manager, if `ingress.tls.clusterIssuer` is set;
  - KEDA 2.x, if `keda.enabled`;
  - the Prometheus Operator, if ServiceMonitors are on;
  - a CNI that enforces NetworkPolicy (VPC CNI with network policies enabled, Calico,
    Cilium, or GKE Dataplane V2).
- **Postgres 17 with PostGIS.** RDS or Cloud SQL, in the same VPC and region.
  - Create the seven databases and roles. Run `infra/postgres/initdb/20-service-databases.sh`
    against it as the admin user, with `PGHOST`, `POSTGRES_USER`, `PGPASSWORD` and the
    `*_DB_PASSWORD` variables set.
  - It also runs `CREATE EXTENSION postgis` in `journeydb`, which needs `rds_superuser` or
    `cloudsqlsuperuser`. The journey migration only re-checks it.
- **Redis.**
  - Non-cluster mode: the session Lua scripts assume it.
  - Separate instances for sessions (auth, admin: persistent, `noeviction`) and for
    cache and counters.
- **RabbitMQ** with quorum queues (compose runs 4.x): Amazon MQ, or CloudAMQP in Mumbai.
  Use 3 nodes for HA.
- **Images** pushed to your registry (`image.registry`), tagged with the build tag (`image.tag`).

## Secrets

Generate **new** values and keys for each environment. Never reuse `secrets/` or
`.env.secrets` from development. Values are hex (`openssl rand -hex 24`), as `make secrets`
generates them. Passwords go into URLs, so they must be URL-safe.

| Secret (values key) | Keys | Used by |
|---|---|---|
| `cogo-auth` (`services.auth.secretName`) | `AUTH_DB_PASSWORD`, `OTP_HMAC_KEY` | auth-* |
| `cogo-user` | `USER_DB_PASSWORD`, `KYC_OTP_HMAC_KEY`, `KYC_CASHFREE_CLIENT_ID`, `KYC_CASHFREE_CLIENT_SECRET`, `GOOGLE_VISION_API_KEY`, optional `MEDIA_ACCESS_KEY`, `MEDIA_SECRET_KEY` (omit with IRSA / Workload Identity) | user-* |
| `cogo-journey` | `JOURNEY_DB_PASSWORD`, `CURSOR_SECRET`, `GOOGLE_PLACES_API_KEY` | journey-* |
| `cogo-social` | `SOCIAL_DB_PASSWORD`, `CURSOR_SECRET` | social-* |
| `cogo-chat` | `CHAT_DB_PASSWORD`, `CURSOR_SECRET` | chat-api, chat-worker |
| `cogo-notification` | `NOTIFICATION_DB_PASSWORD`, `SMTP_USERNAME`, `SMTP_PASSWORD` | notification-* |
| `cogo-admin` | `ADMIN_DB_PASSWORD`, `ADMIN_TOTP_ENCRYPTION_KEY` | admin-api |
| `cogo-rabbitmq` (`rabbitmq.existingSecret`) | `RABBITMQ_URL` (`amqps://user:pass@host:5671/vhost`) | every service with a broker, and KEDA's TriggerAuthentication |
| `cogo-redis` (`redis.existingSecret`, optional) | `auth`, `notification`, `gateway`, `admin`, `journey`, `realtime`, `config`: full URLs, e.g. `rediss://:<token>@host:6379/0` | everyone (`REDIS_URL`, `REALTIME_REDIS_URL`, `CONFIG_REDIS_URL`) |
| `cogo-sentry` (`sentry.secretName`, optional) | `SENTRY_DSN` | every pod |
| `cogo-jwt-private-key` | `jwt_private_key` (Ed25519 PEM) | auth-*, at `/run/secrets/jwt_private_key` |
| `cogo-service-key-<SERVICE_NAME>`, one per service: auth-service, user-service, journey-service, social-service, chat-service, chat-ws, notification-service, admin-service | `service_key` (Ed25519 PEM) | that service, at `/run/secrets/service_key` |
| `cogo-service-public-keys` | `<SERVICE_NAME>.pem` for all eight | everything except the gateway and chat-ws, at `/run/service-keys/` |
| `cogo-fcm` | `fcm-service-account.json` | notification-*, `FCM_CREDENTIALS_FILE=/run/secrets/fcm-service-account.json` |
| `cogo-pgbouncer-userlist` (only with PgBouncer) | `userlist.txt`: `"auth_svc" "<password>"`, one line per role | pgbouncer |

The CURSOR_SECRET may differ per service. Each signs only its own cursors.

```bash
NS=cogo-staging
kubectl create namespace $NS
rnd() { openssl rand -hex 24; }

# Database passwords: the same values you used to create the roles in Postgres.
AUTH_DB_PASSWORD=$(rnd) USER_DB_PASSWORD=$(rnd) JOURNEY_DB_PASSWORD=$(rnd) SOCIAL_DB_PASSWORD=$(rnd)
CHAT_DB_PASSWORD=$(rnd) NOTIFICATION_DB_PASSWORD=$(rnd) ADMIN_DB_PASSWORD=$(rnd)

kubectl -n $NS create secret generic cogo-auth \
  --from-literal=AUTH_DB_PASSWORD=$AUTH_DB_PASSWORD --from-literal=OTP_HMAC_KEY=$(rnd)
kubectl -n $NS create secret generic cogo-user \
  --from-literal=USER_DB_PASSWORD=$USER_DB_PASSWORD --from-literal=KYC_OTP_HMAC_KEY=$(rnd) \
  --from-literal=KYC_CASHFREE_CLIENT_ID='<cashfree client id>' \
  --from-literal=KYC_CASHFREE_CLIENT_SECRET='<cashfree client secret>' \
  --from-literal=GOOGLE_VISION_API_KEY='<key restricted to the Vision API>'
kubectl -n $NS create secret generic cogo-journey \
  --from-literal=JOURNEY_DB_PASSWORD=$JOURNEY_DB_PASSWORD --from-literal=CURSOR_SECRET=$(rnd) \
  --from-literal=GOOGLE_PLACES_API_KEY='<key restricted to Places API (New)>'
kubectl -n $NS create secret generic cogo-social \
  --from-literal=SOCIAL_DB_PASSWORD=$SOCIAL_DB_PASSWORD --from-literal=CURSOR_SECRET=$(rnd)
kubectl -n $NS create secret generic cogo-chat \
  --from-literal=CHAT_DB_PASSWORD=$CHAT_DB_PASSWORD --from-literal=CURSOR_SECRET=$(rnd)
kubectl -n $NS create secret generic cogo-notification \
  --from-literal=NOTIFICATION_DB_PASSWORD=$NOTIFICATION_DB_PASSWORD \
  --from-literal=SMTP_USERNAME='<SES SMTP user>' --from-literal=SMTP_PASSWORD='<SES SMTP password>'
kubectl -n $NS create secret generic cogo-admin \
  --from-literal=ADMIN_DB_PASSWORD=$ADMIN_DB_PASSWORD --from-literal=ADMIN_TOTP_ENCRYPTION_KEY=$(rnd)
kubectl -n $NS create secret generic cogo-rabbitmq \
  --from-literal=RABBITMQ_URL='amqps://cogo:<password>@<broker host>:5671/'
kubectl -n $NS create secret generic cogo-redis \
  --from-literal=auth='rediss://:<token>@<sessions host>:6379/0' \
  --from-literal=admin='rediss://:<token>@<sessions host>:6379/3' \
  --from-literal=notification='rediss://:<token>@<cache host>:6379/1' \
  --from-literal=gateway='rediss://:<token>@<cache host>:6379/2' \
  --from-literal=journey='rediss://:<token>@<cache host>:6379/4' \
  --from-literal=realtime='rediss://:<token>@<cache host>:6379/6' \
  --from-literal=config='rediss://:<token>@<cache host>:6379/7'
kubectl -n $NS create secret generic cogo-sentry --from-literal=SENTRY_DSN='<dsn>'
kubectl -n $NS create secret generic cogo-fcm \
  --from-file=fcm-service-account.json=./firebase-staging-adminsdk.json

# Ed25519 keys (as scripts/init-secrets.sh makes them), in a throwaway directory.
KEYS=$(mktemp -d) && mkdir -p $KEYS/public
openssl genpkey -algorithm ed25519 -out $KEYS/jwt_private_key.pem
kubectl -n $NS create secret generic cogo-jwt-private-key --from-file=jwt_private_key=$KEYS/jwt_private_key.pem
for svc in auth-service user-service journey-service social-service chat-service chat-ws \
           notification-service admin-service; do
  openssl genpkey -algorithm ed25519 -out $KEYS/$svc.pem
  openssl pkey -in $KEYS/$svc.pem -pubout -out $KEYS/public/$svc.pem
  kubectl -n $NS create secret generic cogo-service-key-$svc --from-file=service_key=$KEYS/$svc.pem
done
kubectl -n $NS create secret generic cogo-service-public-keys --from-file=$KEYS/public/
rm -rf $KEYS
```

Notes:

- **Storing secrets.** In practice, keep these in AWS Secrets Manager or GCP Secret
  Manager and sync them with External Secrets Operator, under the same names and keys.
- **Restarts.** Pods read Secrets at start. After changing one, run
  `kubectl -n $NS rollout restart deploy -l app.kubernetes.io/instance=cogo`.
- **Not for staging.** `ADMIN_BOOTSTRAP_*` is development only, and the chart fails to
  render if you set it. `KYC_MOCK_WEBHOOK_SECRET` is only for the mock KYC provider, which
  refuses to start outside development.

## Install, upgrade, roll back

```bash
helm upgrade --install cogo deploy/helm/cogo -n cogo-staging \
  -f deploy/helm/cogo/values-staging.yaml --set image.tag=<git sha> --wait --timeout 15m
helm -n cogo-staging history cogo
helm -n cogo-staging rollback cogo <revision>   # the schema stays migrated forward
helm -n cogo-staging uninstall cogo             # keeps the Secrets and every database
```

- **Migrations.** Each upgrade runs the migration Jobs first and stops if one fails. Read
  the logs with `kubectl -n cogo-staging logs job/cogo-<svc>-migrate`; failed Jobs are kept
  for a day.
- **First admin.** Create one after the first install (`ADMIN_ALLOWED_CIDRS` must include
  your network to open the console):

```bash
kubectl -n cogo-staging exec -it deploy/cogo-admin-api -- \
  python -m app.cli create-admin --email ops@cogo.app --role admin
```

## Scaling

- **APIs, gateway, chat-ws: CPU HPA.**
  - Target `roles.<role>.autoscaling.targetCPUUtilizationPercentage` (default 70), with
    min/max from the role or the deployable.
  - When an HPA (or KEDA) owns a Deployment, the Deployment has no `replicas` field, so
    upgrades don't reset it.
- **Workers: a KEDA `ScaledObject` per worker.**
  - `rabbitmq` trigger, `mode: QueueLength`, target `roles.worker.keda.queueLength`
    ready messages per replica (default 50).
  - notification-worker has two triggers: `notification-service` and `notification.email`
    (target 10, because OTP emails are latency-sensitive).
  - The shared `TriggerAuthentication` reads `host` from `cogo-rabbitmq/RABBITMQ_URL`.
  - The minimum is 1, not 0: user-worker also runs the periodic jobs.
- **Scheduler:** one replica. Its jobs take a Redis lock, so the brief overlap during a
  rollout is safe.
- **PDBs** (`maxUnavailable: 1`) for every deployable whose minimum is above 1.
- **Database connections.** Each replica opens up to `DB_POOL_SIZE + DB_MAX_OVERFLOW`
  connections (10 + 10 by default; staging sets 5 + 5).
  - Keep `sum(max replicas) x (pool + overflow)` under the instance's `max_connections`.
  - Staging at full scale is about 45 replicas x 10 = 450.
  - Above that, lower the pool sizes or turn on PgBouncer.
- **`WEB_CONCURRENCY`:** keep it at 1 and scale with replicas.
  - Metrics aren't aggregated across uvicorn workers.
  - The gateway's `METRICS_PORT` is bound by the first worker only; the others log
    `metrics_port_in_use` and their metrics aren't exported.

### PgBouncer

`pgbouncer.enabled: true` deploys PgBouncer 1.26 (`edoburu/pgbouncer`, run with a mounted
config) in **transaction** mode. The apps' `DATABASE_URL` then points at it; migrations
still go direct. Before you turn it on:

- **What asyncpg needs.** asyncpg (through SQLAlchemy) prepares every statement and caches
  them per connection. In transaction mode a later transaction can land on a different
  server connection. The chart sets `max_prepared_statements = 200`: PgBouncer 1.21+ then
  tracks protocol-level prepared statements itself, which is what keeps asyncpg working.
- **Without prepared-statement support** (`maxPreparedStatements: 0`, or another pooler
  such as RDS Proxy): the chart then sets `DB_PREPARED_STATEMENTS=false` on every service.
  `cogo_common.db.Database` turns off asyncpg's and SQLAlchemy's statement caches and names
  each statement uniquely, so clients can't collide on a shared server connection. Slower
  (every query is parsed again), so prefer leaving `max_prepared_statements` on.
- **Authentication.** PgBouncer authenticates with `scram-sha-256` from
  `cogo-pgbouncer-userlist`. Managed Postgres doesn't expose SCRAM verifiers, so the file
  holds plain passwords.
- **TLS.** PgBouncer uses `server_tls_sslmode = require` to Postgres. App to PgBouncer is
  plain TCP inside the cluster, limited by a NetworkPolicy.
- **Session state.** The services hold no session-level state (no advisory locks, LISTEN or
  `SET`), so transaction pooling is safe.

## Networking

- **Ingress** (ingress-nginx), gateway only, on `ingress.hosts`:
  - TLS from `ingress.tls.secretName`, issued by cert-manager when `clusterIssuer` is set.
  - Body limit 2m, above the gateway's own 1 MiB cap, so clients get the gateway's
    envelope instead of nginx's 413.
  - 60 s read and send timeouts.
  - `/v1/ws` has its own Ingress (`Exact`) with 3600 s timeouts and buffering off. Clients
    ping every 25 s and chat-ws closes idle sockets after 120 s.
- **Client IPs.** Rate limits and `ADMIN_ALLOWED_CIDRS` use the client IP.
  - The gateway takes it from `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS`. Set that
    to the ingress-nginx pod CIDR.
  - ingress-nginx must see the real IP itself: NLB with proxy protocol, or
    `externalTrafficPolicy: Local`, plus `use-forwarded-headers` if there is an L7 LB in front.
- **Admin console.** `/admin/*` is served only to `ADMIN_ALLOWED_CIDRS`. Staging ships `[]`,
  meaning closed. Add the office or VPN egress ranges.
- **NetworkPolicies** (ingress only; egress stays open for managed services and provider APIs):
  - default-deny for the whole namespace;
  - ingress controller to the gateway;
  - gateway to the APIs and chat-ws;
  - any CoGo pod to the APIs on 8000 (JWKS, `/internal/*`, deletion tracker, admin actions);
  - Prometheus to every pod (8000, and the gateway's 9100);
  - CoGo pods to PgBouncer;
  - ingress controller to cert-manager HTTP-01 solvers.

  Selectors for the ingress controller and Prometheus are in `networkPolicy.*`.

## Observability

- **Metrics.** `/metrics` on 8000, except the gateway, which serves it on 9100 only (never
  public). ServiceMonitors cover every deployable when `metrics.serviceMonitor.enabled`
  (staging labels them `release: kube-prometheus-stack`).
- **Logs** are JSON on stdout (`LOG_JSON=true`).
- **Traces** go to `commonEnv.OTEL_EXPORTER_OTLP_ENDPOINT`.
- **Errors** go to Sentry with `sentry.secretName`.
- **Alerts:** `metrics.prometheusRule.enabled` (on in staging) installs the 13 rules of
  `infra/prometheus/rules/cogo.yml` as a `PrometheusRule`, from the copy in
  `files/alerts.yml` (`make chart` fails if the two differ). They group by `service`, which
  the Operator sets from the Kubernetes Service name. The queue alerts (DLQ depth, consumers,
  backlog) need RabbitMQ's `/metrics/detailed` scraped too; with a managed broker, point a
  scrape job at it. Dashboards: import `infra/grafana/dashboards/*.json`.

## Validation

`make chart` (from the repo root) runs all of this. `helm` and `kubeconform` run from pinned
images:

```bash
docker run --rm -v "$PWD":/w -w /w alpine/helm:4.3.1 lint deploy/helm/cogo -f deploy/helm/cogo/values-staging.yaml
docker run --rm -v "$PWD":/w -w /w alpine/helm:4.3.1 template cogo deploy/helm/cogo -f deploy/helm/cogo/values-staging.yaml \
  | docker run --rm -i ghcr.io/yannh/kubeconform:v0.8.0 -strict -summary -kubernetes-version 1.34.0 \
      -schema-location default \
      -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

Run it for the defaults (drop `-f`) and with `--set pgbouncer.enabled=true` too. The chart
also renders with Helm 3.22.

## Before production

- **`values-production.yaml`:**
  - `ENV=production` (HSTS on, the test-push endpoint off).
  - Production Cashfree (`KYC_CASHFREE_BASE_URL=https://api.cashfree.com/verification`, plus
    egress IP allow-listing or `KYC_CASHFREE_PUBLIC_KEY_FILE`).
  - `APP_CHECK_MODE=enforce` once clients send tokens.
  - Restricted `CORS_ALLOW_ORIGINS`; real `ADMIN_ALLOWED_CIDRS`; a narrow `FORWARDED_ALLOW_IPS`.
  - Higher minimum replicas.
- **Ingress controller.** ingress-nginx went out of maintenance in March 2026. Plan a move to
  the Gateway API (for example Envoy Gateway, the GKE Gateway controller, or the AWS Load
  Balancer Controller). Only `templates/ingress.yaml` and the ingress NetworkPolicy selector
  change.
- **Secrets** from a secrets manager (External Secrets) instead of `kubectl create secret`.
- **Key rotation** (docs/SETUP.md §12). Publish the old JWT public key through
  `JWT_ADDITIONAL_PUBLIC_KEY_FILES` before switching the key. That needs an extra file mount,
  via `services.auth.files` and `extraEnv`.
- **Least privilege:**
  - A RabbitMQ user per service, which means a `RABBITMQ_URL` per service instead of the
    shared secret.
  - The media bucket through IRSA or Workload Identity instead of static keys.
- **Datastores:**
  - Postgres with Multi-AZ, PITR backups and tuned `max_connections` (or PgBouncer, with the
    service change above).
  - RabbitMQ with 3 nodes, and DLQ alerts.
  - Separate Redis instances for sessions and cache.
- **Email:** an SES production-access request, and SPF, DKIM and DMARC on the sending domain.
- **Images:** pin by digest. The chart renders `repository:tag` today, so this needs a
  small `cogo.image` change. Add image signing and admission policy.
- **Load test** the HPA and KEDA thresholds (`queueLength`, CPU target) and set real
  resource requests from the measurements.

### Service changes (done in M8)

- notification-service registers its consumers and runs the like flusher only with
  `CONSUMERS_ENABLED=true`, so notification-api pods consume nothing and KEDA alone sizes
  email and push throughput.
- `DB_PREPARED_STATEMENTS` (above) covers poolers without prepared-statement support.
- The gateway starts with `WEB_CONCURRENCY > 1`; only the first worker exports metrics.
