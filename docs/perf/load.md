# Load tests (spec §11, §12)

Measured on 2026-11-21 against the **local stack**, not staging, with the final M8 code (the
first rows below are the last runs; the fixes that got there are further down). M8's done-criteria asks for
the §11 targets on staging; the scripts below are what to run there. Locally they show
whether the code meets the targets with enough processes, and how many processes each
target needs.

## Setup

- One machine runs both the stack and the load generator: 20 cores, 30 GB of RAM, Docker.
- **The stack:** `make load-up`.
  - Gateway rate limits off and 4-hour access tokens, so the test measures the system,
    not the limiter.
  - uvicorn workers stand in for replicas: 4 each for journey, social and chat, 3 for the
    gateway. Every other service runs one process.
  - Postgres 17 + PostGIS 3.5 with default settings, except `max_connections=300`.
- **Data:** `make load-seed`.
  - 200 users with profiles and avatars, 2 upcoming journeys each around Delhi NCR.
  - 80 connected pairs with a chat.
- **Load generator:** k6 1.3.0 in Docker on the same machine (`loadtest/*.js`). It takes some
  of the CPU, so the numbers are conservative.

## Results

| Test (spec §12) | Load | §11 target | Result |
|---|---|---|---|
| Feed scroll (`make load-feed`) | 200 iterations/s for 2 min (260 requests/s; a third scroll to page 2) | p95 < 300 ms | **p95 52 ms** first page, 55 ms next page; 0 errors in 31,207 requests |
| Journey creation with matching (`make load-journeys`) | 500 journeys/min for 3 min | banner filled p95 < 10 s | **p95 545 ms** from the POST to `matches.state = done`; create p95 30 ms; 1,500 of 1,500 filled |
| Sockets and chat (`make load-ws`) | 1,000 sockets, 50 messages/s for 2 min | delivery p95 < 500 ms | **p95 15 ms** socket to socket; 1,000 of 1,000 sockets, 0 errors; 30,051 deliveries for 6,000 messages (each user on 5 sockets) |
| Sockets, larger (`SOCKETS=3000`) | 3,000 sockets, 50 messages/s for 1 min | as above | **p95 16 ms**; 3,000 of 3,000, 0 errors |
| Soak (`make load-soak`) | every flow at once for 1 h (feed 30/s, 60 journeys/min, 300 sockets, 10 messages/s) | DLQs empty | see below |

10k sockets (§12) is left to staging. Each k6 virtual user costs memory: at 3,000 sockets k6
used 1.2 GB, so 10k would need about 4 GB on top of the stack. Run it on a separate load
machine.

## Capacity per process

From the CPU each container used during the runs (`docker stats`), divided by the request
rate. Use these to size the HPAs.

| Process | Work | CPU per request | One core serves |
|---|---|---|---|
| journey-api | `GET /v1/feed` (query, presenter, two relation calls) | 9 ms idle, 12 ms at saturation | about 80–110 feed pages/s |
| api-gateway | a proxied request (edge JWT check, streaming proxy) | 2–3 ms | about 350–450 requests/s |
| chat-api | `POST /internal/relations/connection-status` | 5–6 ms | about 180 calls/s |
| social-api | `POST /internal/relations/follow-status` | 3 ms | about 300 calls/s |
| Postgres | all queries behind one feed page (journey, social, chat) | about 5 ms (10 ms before the query fixes) | about 200 pages/s |
| chat-ws | 1,000 sockets pinging every 25 s, 50 messages/s fanned out | 0.3 cores in total | — |

So §12's feed scroll needs about 3.2 cores of journey-api, 1.5 of chat-api, 0.8 of social-api,
0.7 of gateway and 1.3 of Postgres. `values-staging.yaml` sizes journey-api for it (500m
requests, 2–8 replicas). The feed query's cost also grows with the number of candidate
journeys (`feed.md`): at the 420 here the feed query takes about 2 ms (`plans.md`).

## What the load tests found and fixed

The first feed run failed: p95 11.4 s and 73% errors at 200 rps. The causes, in order:

1. **The feed's Postgres time.** Tag similarity ran two set-operation subqueries per
   candidate, which was 80% of the ranking cost. It now uses `intarray` (migration 0005).
   - The query went from 6.2 ms to 2.8 ms at 400 journeys; `feed.md` has the larger sizes.
   - Later rounds: a hashed block check, inlinable SQL functions (0008), JIT off (0007), and
     extended statistics for the planner (0006).
2. **journey-api CPU** was the actual limit. One process was saturated at about 70 pages/s,
   and the pool timeouts were a symptom: queued requests each held a connection.
   - httpcore failed to import `sniffio` about 8 times a request, each failure searching
     `sys.path`.
   - Column aliases were rebuilt per request.
   - httpx logged two INFO lines per page.
   - After the fixes: about 110 pages/s per core, and the test passes with 4 workers.
3. **The single gateway process** saturated at about 240 requests/s. Several workers are
   only possible now that the second worker no longer dies on the metrics port. In
   Kubernetes it is replicas anyway.

The socket test found four more faults:
- the containers' 1024 open-file limit;
- a WebSocket proxy error path;
- a listen-backlog overflow on connect bursts;
- a Redis pool that failed instead of waiting under 1,000 synchronised pings.

It also found **access tokens in the logs** (uvicorn's handshake lines; spec §6.3 puts the
token in the query string). All are fixed, with regression tests (RECON §6).

## Soak

`make load-soak` runs the four flows together for an hour, then `make dlq`.

**First run (06:38–07:39 UTC):** all thresholds passed and every DLQ was empty: 192,262
requests with no failures; feed p95 31 ms; banner p95 553 ms. But only 2,009 messages reached
a socket, for about 36,000 sent. The script's listeners reused `sockets.js`'s hold time,
which defaulted to 2 minutes, so every socket closed after about 3 minutes and the senders'
messages went to offline users as pushes for the rest of the hour. The HTTP flows, matching
and the push path were soaked; the sockets weren't.
- Fixed: `make load-soak` exports `DURATION`, so every flow uses the same one.
- New threshold: with every user on a socket, `messages_received` must reach 95% of the
  messages sent. It would have failed this run, and it guards `make load-ws` as well.

**Second run:** see the results table once it finishes.

## Running it

```bash
make load-up && make load-seed
make load-feed                     # RATE=200 DURATION=2m
make load-journeys                 # RATE=500 (per minute) DURATION=3m
make load-ws                       # SOCKETS=1000 RATE=50 DURATION=2m
make load-soak                     # DURATION=1h, then the DLQ check
make up                            # normal limits again
```

On staging, set `BASE_URL` to the staging gateway (k6) and `GATEWAY_URL` (seed). For the
load window the stack needs the 4-hour tokens and no gateway rate limits
(`ACCESS_TOKEN_TTL_SECONDS`, `RATE_LIMIT_ENABLED`).

`seed.py` signs users in by reading their codes from Mailpit (`MAILPIT_URL`). On staging,
email goes out through SES, so for the seed either point staging's SMTP at a Mailpit
reachable from the load machine for the window, or sign the 200 users in some other way
and write `loadtest/.seed.json` in the same shape.
