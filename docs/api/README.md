# API reference workbook

`CoGo-API-Reference.xlsx` documents every public endpoint, the real-time protocol, every event
and every error type. Its sheets:

| Sheet | Contents |
|---|---|
| **Overview** | Basics (base URL, auth, WebSocket, push, envelope, lists, formats), breaking changes, and an index with live counts |
| **Gateway Routes** | The public route table in match order, with edge auth and rate limits, the `/v1/ws` WebSocket, and edge behaviour |
| **Auth / User / Journey / Social / Chat / Notification Service** | One row per endpoint: parameters, request and response schemas, examples, errors, rate limits and notes. Then internal endpoints, health, and the events each service consumes |
| **Chat WebSocket** | Every client and server frame with examples, the close codes and the error codes |
| **Events** | Every routing key: producer, consumers, queues, dead-letter queues, payload, idempotency |
| **Error Codes** | Every `error.type`, its HTTP status, who raises it and what the client should do |
| **Schemas** | Every request, response and event field: type, required, nullable, constraints, description, and which endpoints use it |

## Where the content comes from

Most of the workbook is generated from the code, so it can't disagree with it:

| From the code | Source |
|---|---|
| Endpoints, parameters, request/response schemas, success status | `contracts/openapi/*.json` (`make contracts`) |
| Edge auth and rate limit per endpoint; the route table | `services/api-gateway/app/routes.py` |
| Events, queues, consumers, payload schemas | `cogo_common.events` topology and contracts; `scripts/export_event_schemas.py` |
| The set of error types | every `error_type` raised in `services/*/app` and `libs/common` |

What code can't say (summaries, examples, error lists per endpoint, notes, field descriptions,
the WebSocket frames) is curated in `api_content.py`.

**The build refuses to run if `api_content.py` is out of date.** It names every endpoint in an
OpenAPI spec that isn't documented, every documented endpoint that no longer exists, every
consumed routing key without a description, every event without producer info, and every
error type raised in code but missing from the error table.

## Regenerate after an API change

```bash
make api-docs     # runs `make contracts`, then builds the workbook
```

If it fails, add or fix the entries it lists in `api_content.py`, then run it again. The
Overview counts are Excel formulas; Excel, Google Sheets and LibreOffice compute them on open.
