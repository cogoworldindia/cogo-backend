"""Builds docs/api/CoGo-API-Reference.xlsx from the services' OpenAPI specs + curated notes."""

import json
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

SCRATCH, OUT = Path(sys.argv[1]), Path(sys.argv[2])
SPECS = {s: json.loads((SCRATCH / f"{s}-openapi.json").read_text()) for s in ("auth", "user")}

FONT = "Arial"
NAVY, SLATE, LIGHT, ZEBRA = "1F3A5F", "44546A", "DCE6F1", "F5F8FC"
METHOD_FILL = {"GET": "DFF1E1", "POST": "DCE9F7", "PATCH": "FCE8D5", "PUT": "FCE8D5", "DELETE": "F8D9D9", "CONSUME": "EDE3F6", "ANY": "EEEEEE"}
thin = Side(style="thin", color="C9D3E0")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
WRAP_TOP = Alignment(wrap_text=True, vertical="top")
MONO_HINT = Font(name=FONT, size=9, color="333333")

wb = Workbook()


def sheet(title: str, heading: str, subtitle: str, headers: list[str], widths: list[int], rows: list[list], method_col: int | None = None, mono_cols: tuple[int, ...] = ()):
    ws = wb.create_sheet(title)
    ws["A1"] = heading
    ws["A1"].font = Font(name=FONT, size=15, bold=True, color=NAVY)
    ws["A2"] = subtitle
    ws["A2"].font = Font(name=FONT, size=10, italic=True, color=SLATE)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[2].height = 30
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=4, column=c, value=h)
        cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = BORDER
    for r, row in enumerate(rows, 5):
        section = len(row) == 1  # a single-value row is a section divider
        for c in range(1, len(headers) + 1):
            value = row[c - 1] if c - 1 < len(row) else None
            cell = ws.cell(row=r, column=c, value=value)
            cell.border = BORDER
            cell.alignment = WRAP_TOP
            cell.font = Font(name=FONT, size=10)
            if section:
                cell.fill = PatternFill("solid", fgColor=LIGHT)
                cell.font = Font(name=FONT, size=10, bold=True, color=NAVY)
            elif c in mono_cols:
                cell.font = MONO_HINT
            if not section and r % 2 == 0:
                cell.fill = PatternFill("solid", fgColor=ZEBRA)
        if not section and method_col:
            m = ws.cell(row=r, column=method_col)
            m.fill = PatternFill("solid", fgColor=METHOD_FILL.get(str(m.value).split()[0], "EEEEEE"))
            m.font = Font(name=FONT, size=10, bold=True)
        if section:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(headers))
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B5"
    ws.auto_filter.ref = f"A4:{get_column_letter(len(headers))}{4 + len(rows)}"
    ws.sheet_view.showGridLines = False
    return ws


def j(obj) -> str:
    return json.dumps(obj, indent=2)


TOKEN_PAIR_EX = {"status": "success", "code": 200, "message": "Signed in", "data": {
    "access_token": "eyJhbGciOiJFZERTQSIsImtpZCI6Ii4uLiIsInR5cCI6ImF0K2p3dCJ9...", "refresh_token": "q8Jx3...43 chars",
    "token_type": "bearer", "expires_in": 900, "user_id": "01a1efb0-044d-76c9-abed-4549c122f6e1", "is_new_user": True}, "error": None}
ME_EX = {"status": "success", "code": 200, "message": "OK", "data": {
    "id": "01a1efb0-044d-76c9-abed-4549c122f6e1", "status": "active", "created_at": "2026-11-18T17:24:12Z",
    "profile": {"full_name": "Asha Rao", "date_of_birth": "1996-04-02", "gender": "female", "bio": "Delhi to Goa by train",
                "profile_photo_url": "https://cdn.cogo.app/u/asha.jpg", "updated_at": "2026-11-18T17:25:03Z"}}, "error": None}
AUTH_ERRS = "401 missing_token / invalid_token / token_expired\n429 rate_limited (gateway)"

# --------------------------------------------------------------------------- Auth
auth_rows = [
    ["Public endpoints (through the gateway, base URL http://localhost:8000 in dev)"],
    ["AUTH-01", "POST", "/v1/auth/email/send", "Email a 6-digit sign-in code", "None",
     "EmailSendRequest", j({"email": "asha@example.com"}), "202 Accepted", "EmailSendResponse",
     j({"status": "success", "code": 202, "message": "If the address can receive email, a code is on its way", "data": {"expires_in": 600, "resend_after": 60}, "error": None}),
     "422 validation_error\n429 otp_cooldown (Retry-After)\n429 otp_daily_limit\n429 rate_limited (gateway)\n503 unavailable (email queue down; retry)",
     "Gateway: 5/min per IP (fails closed)\nService: 1 per 60 s per email, 10/day per email",
     "Same response whether or not an account exists (no user enumeration). A new code replaces the previous one. Codes expire after 10 min."],
    ["AUTH-02", "POST", "/v1/auth/email/verify", "Verify the code and sign in (creates the account on first sign-in)", "None",
     "EmailVerifyRequest", j({"email": "asha@example.com", "code": "482913"}), "200 OK", "TokenPair", j(TOKEN_PAIR_EX),
     "400 otp_invalid (wrong or expired)\n429 otp_locked (5th wrong attempt; request a new code)\n403 account_inactive\n409 conflict (rare race; retry)\n422 validation_error\n429 rate_limited (gateway)",
     "Gateway: 10/min per IP (fails closed)\nService: 5 attempts per code",
     "If a Google identity already uses this verified email, you get the same user_id (accounts are linked). is_new_user=true only when the account was just created."],
    ["AUTH-03", "POST", "/v1/auth/google", "Sign in with Google (Firebase Auth ID token)", "None",
     "GoogleSignInRequest", j({"id_token": "<Firebase ID token from the client SDK>"}), "200 OK", "TokenPair", j(TOKEN_PAIR_EX),
     "401 google_token_invalid\n401 google_token_expired\n401 google_email_unverified\n403 account_inactive\n503 provider_disabled (FIREBASE_PROJECT_ID not set)\n503 unavailable (Google keys unreachable)\n422 validation_error\n429 rate_limited (gateway)",
     "Gateway: 20/min per IP (fails closed)",
     "Token must come from Firebase Auth with the Google provider (sign_in_provider = google.com) and a verified email. Identity key is the Firebase uid; links to an existing account with the same verified email."],
    ["AUTH-04", "POST", "/v1/auth/token/refresh", "Exchange a refresh token for a new token pair (rotation)", "None",
     "RefreshRequest", j({"refresh_token": "q8Jx3...43 chars"}), "200 OK", "TokenPair",
     j({**TOKEN_PAIR_EX, "message": "Token refreshed", "data": {**TOKEN_PAIR_EX["data"], "is_new_user": False}}),
     "401 invalid_refresh_token (unknown/expired/revoked)\n401 refresh_token_reused (an old token was replayed: the whole session is revoked, sign in again)\n403 account_inactive\n422 validation_error\n429 rate_limited (gateway)",
     "Gateway: 30/min per IP (fails closed)",
     "Every refresh returns a NEW refresh token; the old one is dead. Store the new one atomically. Don't refresh the same token concurrently from two places — the second call is treated as theft. Refresh tokens live 30 days (sliding)."],
    ["AUTH-05", "POST", "/v1/auth/logout", "Sign out of the current session", "Bearer access token",
     "—", "(no body)", "200 OK", "null", j({"status": "success", "code": 200, "message": "Signed out", "data": None, "error": None}),
     AUTH_ERRS, "Gateway: 30/min per user",
     "Revokes the session's refresh token. The access token stays valid until it expires (≤15 min); discard it client-side."],
    ["AUTH-06", "POST", "/v1/auth/logout-all", "Sign out of every session/device", "Bearer access token",
     "—", "(no body)", "200 OK", "LogoutAllResponse",
     j({"status": "success", "code": 200, "message": "Signed out everywhere", "data": {"revoked_sessions": 3}, "error": None}),
     AUTH_ERRS, "Gateway: 10/min per user", "Use after a suspected compromise or from a 'sign out everywhere' setting."],
    ["Internal endpoints (NOT routed by the gateway; reachable only on the private network)"],
    ["AUTH-I1", "GET", "/.well-known/jwks.json", "Public keys (JWKS) used to verify access tokens", "Internal",
     "—", "—", "200 OK", "JWKS (RFC 7517), not enveloped",
     j({"keys": [{"kty": "OKP", "crv": "Ed25519", "x": "…", "kid": "…", "use": "sig", "alg": "EdDSA"}]}),
     "—", "—", "Consumed by the gateway and every service through cogo_common.JWKSClient (cached 5 min, refetched on unknown kid). Cache-Control: max-age=300."],
    ["AUTH-I2", "GET", "/health/live · /health/ready · /metrics", "Liveness, readiness (Postgres, Redis, RabbitMQ), Prometheus metrics", "Internal",
     "—", "—", "200 / 503", "Health JSON / Prometheus text", j({"status": "ok", "checks": {"postgres": "ok", "redis": "ok", "rabbitmq": "ok"}}),
     "503 when a dependency check fails", "—", "/health is an alias of /health/live for container health checks."],
]
sheet("Auth Service", "auth-service  —  identity, sign-in and sessions",
      "Owns accounts, sign-in identities (email, Google), access tokens (EdDSA JWT, 15 min) and refresh sessions. Clients call it only through the API gateway. Schemas are detailed on the Schemas sheet; error types on Error Codes.",
      ["ID", "Method", "Path", "Summary", "Auth", "Request body (schema)", "Request example", "Success", "Response data (schema)", "Response example", "Errors (status error.type)", "Rate limits", "Notes"],
      [9, 9, 26, 30, 14, 19, 30, 11, 20, 48, 40, 26, 46], auth_rows, method_col=2, mono_cols=(3, 7, 10))

# --------------------------------------------------------------------------- User
user_rows = [
    ["Public endpoints (through the gateway). Every route requires a Bearer access token."],
    ["USER-01", "GET", "/v1/users/me", "The caller's full user record and profile", "Bearer access token",
     "—", "(no body)", "200 OK", "MeOut", j(ME_EX),
     AUTH_ERRS + "\n404 not_found (account deleted)", "Gateway: 120/min per user (all /v1/users/*)",
     "Creates the user row on the first call if the user.registered event hasn't arrived yet. profile is null until the first PATCH."],
    ["USER-02", "PATCH", "/v1/users/me/profile", "Create or partially update the caller's profile", "Bearer access token",
     "ProfileUpdate", j({"full_name": "Asha Rao", "date_of_birth": "1996-04-02", "gender": "female", "bio": "Delhi to Goa by train", "profile_photo_url": "https://cdn.cogo.app/u/asha.jpg"}),
     "200 OK", "MeOut", j({**ME_EX, "message": "Profile updated"}),
     AUTH_ERRS + "\n400 empty_update (no fields sent)\n403 account_inactive\n413 payload_too_large\n422 validation_error",
     "Gateway: 120/min per user",
     "PATCH semantics: omitted fields are unchanged; an explicit null clears the field. Unknown fields (status, user_id, …) are rejected with 422. profile_photo_url must be https."],
    ["USER-03", "GET", "/v1/users/{user_id}", "Another user's public profile", "Bearer access token",
     "—", "Path param user_id: UUID", "200 OK", "PublicUserOut",
     j({"status": "success", "code": 200, "message": "OK", "data": {"id": "01a1efb0-0695-73e0-90d1-b00f9f582fa0", "full_name": "Ravi Kumar", "bio": "Weekend trekker", "profile_photo_url": None}, "error": None}),
     AUTH_ERRS + "\n404 not_found (unknown, suspended or deleted)\n422 validation_error (not a UUID)",
     "Gateway: 120/min per user",
     "Public projection only: never date of birth, gender or status. There is no endpoint that writes another user's data."],
    ["Internal endpoints (NOT routed by the gateway)"],
    ["USER-I1", "GET", "/health/live · /health/ready · /metrics", "Liveness, readiness (Postgres, RabbitMQ), Prometheus metrics", "Internal",
     "—", "—", "200 / 503", "Health JSON / Prometheus text", j({"status": "ok", "checks": {"postgres": "ok", "rabbitmq": "ok"}}),
     "503 when a dependency check fails", "—", ""],
    ["Consumed events (see Events sheet)"],
    ["USER-E1", "CONSUME", "user.registered", "Create the user row for a newly registered account", "Internal (RabbitMQ)",
     "UserRegistered", j({"user_id": "01a1efb0-044d-76c9-abed-4549c122f6e1", "provider": "email", "registered_at": "2026-11-18T17:24:12Z"}),
     "ack", "—", "—", "Malformed payload → rejected to DLQ. Transient failure → 3 retries with backoff, then nack (DLQ after 5 deliveries).",
     "—", "Queue user-service.user.registered (quorum). Idempotent via processed_events: duplicates are skipped."],
]
sheet("User Service", "user-service  —  users and profiles",
      "Owns user records and profiles. The user id is minted by auth-service. Identity always comes from the verified token (sub), never from a client-supplied id.",
      ["ID", "Method", "Path", "Summary", "Auth", "Request body (schema)", "Request example", "Success", "Response data (schema)", "Response example", "Errors (status error.type)", "Rate limits", "Notes"],
      [9, 10, 26, 30, 14, 19, 34, 11, 20, 50, 40, 24, 46], user_rows, method_col=2, mono_cols=(3, 7, 10))

# --------------------------------------------------------------------------- Notification
notif_rows = [
    ["Consumed commands — there is NO HTTP API for sending email (the old open relay is gone)"],
    ["NOTIF-E1", "CONSUME", "notification.email.requested", "Render a template and deliver an email", "Internal (RabbitMQ)",
     "EmailRequested", j({"template": "otp", "to": "asha@example.com", "params": {"code": "482913", "ttl_minutes": 10}, "locale": "en", "expires_at": "2026-11-18T17:34:12Z"}),
     "ack (sent / skipped)", "Requests past expires_at are dropped. Duplicate event ids are skipped (Redis, 24 h).",
     "Invalid payload or unknown template → DLQ immediately.\nSMTP 5xx (permanent) → DLQ.\nSMTP 4xx / connection error → 3 retries (1 s, 4 s, 10 s), then nack; DLQ after 5 deliveries.",
     "Queue notification.email (quorum, delivery-limit 5, DLQ notification.email.dlq). Producer today: auth-service (OTP)."],
    ["Templates (app/templates, Jinja2: HTML autoescaped, missing params are errors)"],
    ["NOTIF-T1", "—", "otp", "Sign-in code email (subject, text and HTML parts)", "—",
     "params", j({"code": "6-digit string", "ttl_minutes": "integer"}), "—", "Subject: 'Your CoGo sign-in code: 482913'", "—",
     "Add a template = add otp-like files + widen EmailRequested.template."],
    ["Internal HTTP (port 8000, not routed by the gateway)"],
    ["NOTIF-I1", "GET", "/health/live · /health/ready · /metrics", "Liveness, readiness (RabbitMQ, Redis), Prometheus metrics", "Internal",
     "—", "—", "200 / 503", "—", "—", ""],
]
sheet("Notification Service", "notification-service  —  event-driven email delivery",
      "A RabbitMQ worker. Other services request emails by publishing notification.email.requested; this service renders and delivers them through any SMTP relay (Mailpit in dev; SES / SendGrid / Postmark in prod). Logs mask recipients and never include template params.",
      ["ID", "Type", "Name / Path", "Purpose", "Auth", "Payload (schema)", "Payload example", "Outcome", "Behaviour", "Failure handling", "Notes"],
      [10, 10, 30, 34, 16, 16, 40, 16, 38, 44, 44], notif_rows, method_col=2, mono_cols=(3, 7))

# --------------------------------------------------------------------------- Gateway
gw_rows = [
    ["Routed (anything not listed returns 404 at the edge)"],
    ["GW-01", "POST", "/v1/auth/email/send", "exact", "auth-service", "No", "5 / minute", "Client IP", "Fail closed (503)"],
    ["GW-02", "POST", "/v1/auth/email/verify", "exact", "auth-service", "No", "10 / minute", "Client IP", "Fail closed (503)"],
    ["GW-03", "POST", "/v1/auth/google", "exact", "auth-service", "No", "20 / minute", "Client IP", "Fail closed (503)"],
    ["GW-04", "POST", "/v1/auth/token/refresh", "exact", "auth-service", "No", "30 / minute", "Client IP", "Fail closed (503)"],
    ["GW-05", "POST", "/v1/auth/logout", "exact", "auth-service", "Yes (Bearer)", "30 / minute", "User id", "Fail open"],
    ["GW-06", "POST", "/v1/auth/logout-all", "exact", "auth-service", "Yes (Bearer)", "10 / minute", "User id", "Fail open"],
    ["GW-07", "GET PATCH PUT POST DELETE", "/v1/users/*", "prefix", "user-service", "Yes (Bearer)", "120 / minute", "User id", "Fail open"],
    ["Served by the gateway itself"],
    ["GW-H1", "GET", "/health/live · /health/ready", "exact", "(gateway)", "No", "—", "—", "Ready checks Redis"],
    ["Never routed (examples) — all return 404"],
    ["GW-X1", "ANY", "/.well-known/jwks.json, /health*, /metrics of upstreams", "—", "—", "—", "—", "—", "Internal only"],
    ["GW-X2", "ANY", "/internal/*, /docs, /openapi.json", "—", "—", "—", "—", "—", "Internal only"],
    ["GW-X3", "ANY", "/v1/api/*  (old prefix, incl. the old /v1/api/emails relay)", "—", "—", "—", "—", "—", "Removed"],
]
ws = sheet("Gateway Routes", "api-gateway  —  public route table",
           "The single public entry point (dev: http://localhost:8000). Public paths equal upstream paths. Authenticated routes are verified at the edge (EdDSA via JWKS) and again by the service. Limits are sliding windows in Redis, shared by all gateway replicas. Metrics are on internal port 9100.",
           ["ID", "Method(s)", "Public path", "Match", "Upstream", "Auth at edge", "Rate limit", "Limited per", "If Redis is down"],
           [9, 24, 52, 9, 16, 14, 13, 13, 20], gw_rows, method_col=2, mono_cols=(3,))
base = 5 + len(gw_rows) + 1
notes = [
    ("Edge behaviour", ""),
    ("Path hygiene", "Rejects '..', '.', empty segments, backslashes and %2f / %5c / %2e with 400 bad_path before routing."),
    ("Body size", "MAX_BODY_BYTES (default 1 MiB), enforced while streaming (declared and chunked bodies) → 413 payload_too_large."),
    ("Headers in", "Strips hop-by-hop headers and any client X-Forwarded-*, X-Real-IP; sets X-Forwarded-For/Proto/Host and X-Request-Id; forwards Authorization unchanged."),
    ("Headers out", "Strips hop-by-hop and Server; adds X-Content-Type-Options, X-Frame-Options, Referrer-Policy, CORP (+ HSTS in production); echoes X-Request-Id."),
    ("429 response", "Retry-After (seconds) and X-RateLimit-Limit headers; error.type = rate_limited."),
    ("Upstream errors", "Connect failure 503 upstream_unavailable · timeout 504 upstream_timeout · protocol error 502 bad_gateway (no upstream detail leaked)."),
    ("CORS", "Allowlist from CORS_ALLOW_ORIGINS; allowed headers authorization, content-type, x-request-id; exposes x-request-id, retry-after."),
    ("Client IP", "Taken from X-Forwarded-For only when the connecting peer is in FORWARDED_ALLOW_IPS (set to the load balancer in production)."),
]
for i, (k, v) in enumerate(notes):
    a, b = ws.cell(row=base + i, column=1, value=k), ws.cell(row=base + i, column=3, value=v)
    ws.merge_cells(start_row=base + i, start_column=1, end_row=base + i, end_column=2)
    ws.merge_cells(start_row=base + i, start_column=3, end_row=base + i, end_column=9)
    a.font = Font(name=FONT, size=10, bold=True, color=NAVY if i == 0 else "000000", underline="single" if i == 0 else None)
    b.font = Font(name=FONT, size=10)
    a.alignment = b.alignment = WRAP_TOP
    ws.row_dimensions[base + i].height = 28 if i else 18

# --------------------------------------------------------------------------- Events
events_rows = [
    ["EVT-01", "user.registered", "Domain event", "auth-service", "Transactional outbox (same DB transaction as the new account) → relay", "user-service",
     "user-service.user.registered", "Quorum · delivery-limit 5", "user-service.user.registered.dlq", "EventEnvelope + UserRegistered",
     "Inbox table processed_events (event id)", "No PII on the bus (no email). Relay uses FOR UPDATE SKIP LOCKED; safe in every replica."],
    ["EVT-02", "notification.email.requested", "Command", "auth-service (OTP)", "Direct publish with confirms + mandatory (no DB state to keep consistent)", "notification-service",
     "notification.email", "Quorum · delivery-limit 5", "notification.email.dlq", "EventEnvelope + EmailRequested",
     "Redis key per event id (24 h)", "If publishing fails the API returns 503 and the OTP is cancelled so the user can retry at once."],
]
sheet("Events", "Event catalogue  —  RabbitMQ (exchange cogo.events, topic)",
      "State changes cross service boundaries only as messages. Delivery is at-least-once, so every consumer is idempotent. Dead letters go to exchange cogo.dlx and a per-queue .dlq. Topology is declared by every producer and consumer at startup (cogo_common.events.topology).",
      ["ID", "Routing key", "Kind", "Producer", "Published via", "Consumer(s)", "Queue", "Queue type", "Dead-letter queue", "Payload", "Idempotency", "Notes"],
      [8, 28, 13, 18, 34, 18, 30, 18, 32, 24, 24, 44], events_rows, mono_cols=(2, 7, 9))

# --------------------------------------------------------------------------- Errors
err = [
    ["Generic (any service / gateway)"],
    ["bad_request", 400, "any", "The request is malformed.", "Fix the request."],
    ["validation_error", 422, "any", "Body/path/query failed validation. error.details = [{loc, msg, type}] (inputs are never echoed).", "Show field errors from details[].loc."],
    ["unauthorized", 401, "any", "Authentication required.", "Sign in."],
    ["forbidden", 403, "any", "Authenticated but not allowed.", "—"],
    ["not_found", 404, "any", "Resource or route doesn't exist (unlisted gateway routes too).", "—"],
    ["method_not_allowed", 405, "gateway", "Path exists but not for this method. Allow header lists methods.", "Fix the method."],
    ["payload_too_large", 413, "gateway / services", "Body above the limit (gateway 1 MiB, services 64 KiB).", "Send a smaller body."],
    ["rate_limited", 429, "gateway", "Rate limit hit. Retry-After header gives seconds.", "Back off for Retry-After seconds."],
    ["internal_error", 500, "any", "Unexpected failure. Details are only in server logs (search by request_id).", "Retry later; report the request_id."],
    ["bad_gateway", 502, "gateway", "Upstream returned an invalid response.", "Retry with backoff."],
    ["unavailable", 503, "any", "A dependency is temporarily unavailable (queue, Redis, Google keys…).", "Retry with backoff."],
    ["upstream_unavailable", 503, "gateway", "Upstream service unreachable.", "Retry with backoff."],
    ["upstream_timeout", 504, "gateway", "Upstream didn't answer in time.", "Retry with backoff (idempotent calls only)."],
    ["bad_path", 400, "gateway", "Path contains traversal or encoded separators.", "Fix the client URL building."],
    ["Tokens"],
    ["missing_token", 401, "gateway / services", "No Authorization header.", "Send Authorization: Bearer <access_token>."],
    ["invalid_token", 401, "gateway / services", "Malformed, wrong signature/issuer/audience, or no 'Bearer ' prefix.", "Sign in again."],
    ["token_expired", 401, "gateway / services", "Access token expired (15 min lifetime).", "Call /v1/auth/token/refresh, then retry once."],
    ["invalid_refresh_token", 401, "auth", "Refresh token unknown, expired or revoked.", "Sign in again."],
    ["refresh_token_reused", 401, "auth", "A rotated (old) refresh token was replayed; the session was revoked as a precaution.", "Sign in again; check for concurrent refreshes in the client."],
    ["Sign-in"],
    ["otp_cooldown", 429, "auth", "A code was sent to this email less than 60 s ago.", "Wait Retry-After seconds."],
    ["otp_daily_limit", 429, "auth", "Too many codes for this email today.", "Try tomorrow / another method."],
    ["otp_invalid", 400, "auth", "Wrong or expired code.", "Let the user retry or request a new code."],
    ["otp_locked", 429, "auth", "5 wrong attempts; the code is burned.", "Request a new code."],
    ["google_token_invalid", 401, "auth", "Firebase ID token invalid, wrong project, or not a Google sign-in.", "Re-run Google sign-in."],
    ["google_token_expired", 401, "auth", "Firebase ID token expired.", "Get a fresh ID token (getIdToken(true))."],
    ["google_email_unverified", 401, "auth", "Google account email isn't verified.", "Ask the user to verify their Google email."],
    ["provider_disabled", 503, "auth", "Google sign-in not configured on this environment.", "Hide the Google button / use email."],
    ["account_inactive", 403, "auth / user", "Account is suspended or deleted.", "Show an account-status message."],
    ["conflict", 409, "auth", "Concurrent first sign-in race.", "Retry once."],
    ["Profile"],
    ["empty_update", 400, "user", "PATCH with no fields.", "Send at least one field."],
]
sheet("Error Codes", "Error codes  —  error.type catalogue",
      'Every error uses the envelope {"status":"error","code":<http>,"message":str,"data":null,"error":{"type":str,"request_id":str}}. Branch on error.type (stable), show message to humans, log request_id (also in the X-Request-Id header).',
      ["error.type", "HTTP", "Raised by", "Meaning", "Client action"], [26, 8, 18, 70, 46], err, mono_cols=(1,))

# --------------------------------------------------------------------------- Schemas
DESC = {
    ("EmailSendRequest", "email"): ("Address to send the code to; normalised to lower-case.", "asha@example.com"),
    ("EmailSendResponse", "expires_in"): ("Seconds until the code expires.", "600"),
    ("EmailSendResponse", "resend_after"): ("Seconds before another code can be requested for this email.", "60"),
    ("EmailVerifyRequest", "email"): ("Same address the code was sent to.", "asha@example.com"),
    ("EmailVerifyRequest", "code"): ("6-digit code from the email (string; keep leading zeros).", "048291"),
    ("GoogleSignInRequest", "id_token"): ("Firebase Auth ID token after Google sign-in in the client SDK.", "eyJhbGciOiJSUzI1NiIs…"),
    ("RefreshRequest", "refresh_token"): ("Latest refresh token. Single use.", "q8Jx3…"),
    ("TokenPair", "access_token"): ("EdDSA JWT. Send as Authorization: Bearer <token>. Lifetime expires_in.", "eyJhbGciOiJFZERTQSIs…"),
    ("TokenPair", "refresh_token"): ("Opaque, single-use, 30 days (sliding). Store in Keychain/Keystore.", "q8Jx3…"),
    ("TokenPair", "token_type"): ("Always 'bearer'.", "bearer"),
    ("TokenPair", "expires_in"): ("Access-token lifetime in seconds.", "900"),
    ("TokenPair", "user_id"): ("Platform-wide user id (UUIDv7).", "01a1efb0-044d-76c9-abed-4549c122f6e1"),
    ("TokenPair", "is_new_user"): ("True only on the sign-in that created the account (start onboarding).", "true"),
    ("LogoutAllResponse", "revoked_sessions"): ("Number of sessions revoked.", "3"),
    ("MeOut", "id"): ("User id.", "01a1efb0-…"),
    ("MeOut", "status"): ("active | suspended | deleted", "active"),
    ("MeOut", "created_at"): ("ISO-8601 UTC.", "2026-11-18T17:24:12Z"),
    ("MeOut", "profile"): ("ProfileOut, or null before the first profile update.", "{…}"),
    ("ProfileOut", "full_name"): ("Display name.", "Asha Rao"),
    ("ProfileOut", "date_of_birth"): ("YYYY-MM-DD. Private: never shown to other users.", "1996-04-02"),
    ("ProfileOut", "gender"): ("Gender enum. Private.", "female"),
    ("ProfileOut", "bio"): ("Short bio.", "Delhi to Goa by train"),
    ("ProfileOut", "profile_photo_url"): ("https URL.", "https://cdn.cogo.app/u/asha.jpg"),
    ("ProfileOut", "updated_at"): ("ISO-8601 UTC.", "2026-11-18T17:25:03Z"),
    ("ProfileUpdate", "full_name"): ("Trimmed, 1–100 chars. Omit = unchanged, null = clear.", "Asha Rao"),
    ("ProfileUpdate", "date_of_birth"): ("YYYY-MM-DD, in the past, year ≥ 1900.", "1996-04-02"),
    ("ProfileUpdate", "gender"): ("Gender enum.", "female"),
    ("ProfileUpdate", "bio"): ("Trimmed, ≤ 500 chars.", "Weekend trekker"),
    ("ProfileUpdate", "profile_photo_url"): ("https only, ≤ 2048 chars.", "https://cdn.cogo.app/u/asha.jpg"),
    ("PublicUserOut", "id"): ("User id.", "01a1efb0-…"),
    ("PublicUserOut", "full_name"): ("", "Ravi Kumar"),
    ("PublicUserOut", "bio"): ("", "Weekend trekker"),
    ("PublicUserOut", "profile_photo_url"): ("", "null"),
}
USED_BY = {"EmailSendRequest": "AUTH-01", "EmailSendResponse": "AUTH-01", "EmailVerifyRequest": "AUTH-02", "GoogleSignInRequest": "AUTH-03",
           "RefreshRequest": "AUTH-04", "TokenPair": "AUTH-02, AUTH-03, AUTH-04", "LogoutAllResponse": "AUTH-06",
           "MeOut": "USER-01, USER-02", "ProfileOut": "USER-01, USER-02", "ProfileUpdate": "USER-02", "PublicUserOut": "USER-03"}


def describe(prop: dict) -> tuple[str, bool, str]:
    variants = prop.get("anyOf", [prop])
    nullable = any(v.get("type") == "null" for v in variants)
    types, cons = [], []
    for v in variants:
        if v.get("type") == "null":
            continue
        types.append(v.get("type") or v.get("$ref", "").split("/")[-1])
        if "format" in v:
            types[-1] += f" ({v['format']})"
        for k, label in (("minLength", "min length"), ("maxLength", "max length"), ("pattern", "pattern")):
            if k in v:
                cons.append(f"{label} {v[k]}")
    if "default" in prop:
        cons.append(f"default {json.dumps(prop['default'])}")
    return " | ".join(types) or "any", nullable, "; ".join(cons)


schema_rows = []
for svc, label in (("auth", "auth-service"), ("user", "user-service")):
    schema_rows.append([f"{label} (generated from the service's OpenAPI spec)"])
    for name, sc in SPECS[svc]["components"]["schemas"].items():
        if name.startswith("Envelope") or name in ("HTTPValidationError", "ValidationError"):
            continue
        if "enum" in sc:
            schema_rows.append([name, "enum", "(values)", "string", "—", "No", " | ".join(sc["enum"]), "Enumeration.", sc["enum"][0], "see fields using it"])
            continue
        required = set(sc.get("required", []))
        for field, prop in sc.get("properties", {}).items():
            typ, nullable, cons = describe(prop)
            desc, ex = DESC.get((name, field), ("", ""))
            schema_rows.append([name, "request" if name.endswith(("Request", "Update")) else "response", field, typ,
                                "Yes" if field in required else "No", "Yes" if nullable else "No", cons, desc, ex, USED_BY.get(name, "")])
schema_rows += [
    ["Shared wire formats (cogo_common)"],
    ["Envelope (success)", "response", "status / code / message / data / error", "object", "Yes", "—", "status='success'; code mirrors HTTP status; error=null", "Wraps every JSON response body.", '{"status":"success","code":200,"message":"OK","data":{…},"error":null}', "all"],
    ["Envelope (error)", "response", "error.type / error.request_id / error.details", "object", "Yes", "—", "status='error'; data=null; details only for 422", "See Error Codes sheet.", '{"status":"error","code":401,"message":"Token expired","data":null,"error":{"type":"token_expired","request_id":"…"}}', "all"],
    ["AccessToken (JWT)", "token", "header: alg, kid, typ", "JOSE header", "Yes", "No", "alg=EdDSA (Ed25519); typ=at+jwt", "kid = RFC 7638 thumbprint of the signing key.", '{"alg":"EdDSA","kid":"…","typ":"at+jwt"}', "all Bearer routes"],
    ["AccessToken (JWT)", "token", "claims: sub, sid, iss, aud, iat, nbf, exp, jti", "JWT claims", "Yes", "No", "iss=cogo-auth; aud=cogo-api; exp = iat + 900", "sub = user id; sid = session id. Clients should treat the token as opaque.", '{"sub":"01a1…","sid":"…","iss":"cogo-auth","aud":"cogo-api"}', "all Bearer routes"],
    ["Event contracts (cogo_common.events.contracts)"],
    ["EventEnvelope", "event", "id", "string (uuid)", "Yes", "No", "UUIDv7", "Event id = AMQP message_id = idempotency key.", "01a1efb0-…", "EVT-01, EVT-02"],
    ["EventEnvelope", "event", "type", "string", "Yes", "No", "", "Routing key.", "user.registered", "EVT-01, EVT-02"],
    ["EventEnvelope", "event", "version", "integer", "Yes", "No", "default 1", "Bump on breaking payload change.", "1", "EVT-01, EVT-02"],
    ["EventEnvelope", "event", "occurred_at", "string (date-time)", "Yes", "No", "", "UTC.", "2026-11-18T17:24:12Z", "EVT-01, EVT-02"],
    ["EventEnvelope", "event", "producer", "string", "Yes", "No", "", "Producing service.", "auth-service", "EVT-01, EVT-02"],
    ["EventEnvelope", "event", "data", "object", "Yes", "No", "", "Contract below.", "{…}", "EVT-01, EVT-02"],
    ["UserRegistered", "event", "user_id", "string (uuid)", "Yes", "No", "", "New account id.", "01a1efb0-…", "EVT-01"],
    ["UserRegistered", "event", "provider", "string", "Yes", "No", "email | google", "First sign-in method.", "email", "EVT-01"],
    ["UserRegistered", "event", "registered_at", "string (date-time)", "Yes", "No", "", "UTC.", "2026-11-18T17:24:12Z", "EVT-01"],
    ["EmailRequested", "command", "template", "string", "Yes", "No", "otp", "Template name.", "otp", "EVT-02"],
    ["EmailRequested", "command", "to", "string (email)", "Yes", "No", "", "Recipient.", "asha@example.com", "EVT-02"],
    ["EmailRequested", "command", "params", "object", "Yes", "No", "template-specific", "otp: {code, ttl_minutes}. Never logged.", '{"code":"482913","ttl_minutes":10}', "EVT-02"],
    ["EmailRequested", "command", "locale", "string", "No", "No", "default en", "Reserved for localisation.", "en", "EVT-02"],
    ["EmailRequested", "command", "expires_at", "string (date-time)", "No", "Yes", "", "Dropped instead of sent after this time.", "2026-11-18T17:34:12Z", "EVT-02"],
]
sheet("Schemas", "Schemas  —  request, response, token and event fields",
      "Auth and user schemas are generated from each service's OpenAPI spec, so names, types and constraints match the code. Every JSON body below sits inside the envelope's data field.",
      ["Schema", "Kind", "Field", "Type", "Required", "Nullable", "Constraints", "Description", "Example", "Used by"],
      [22, 10, 30, 18, 9, 9, 30, 52, 36, 20], schema_rows, mono_cols=(3, 9))

# --------------------------------------------------------------------------- Overview (first sheet)
ov = wb.active
ov.title = "Overview"
ov.sheet_view.showGridLines = False
ov["A1"] = "CoGo Backend — API Reference (v1)"
ov["A1"].font = Font(name=FONT, size=18, bold=True, color=NAVY)
ov["A2"] = "Generated from the services' OpenAPI specs and code on branch refactor/foundations (2026-11-18). Regenerate when endpoints change."
ov["A2"].font = Font(name=FONT, size=10, italic=True, color=SLATE)

r = 4
def kv_block(title: str, pairs: list[tuple[str, str]]) -> None:
    global r
    ov.cell(row=r, column=1, value=title).font = Font(name=FONT, size=12, bold=True, color=NAVY)
    r += 1
    for k, v in pairs:
        a, b = ov.cell(row=r, column=1, value=k), ov.cell(row=r, column=2, value=v)
        a.font, b.font = Font(name=FONT, size=10, bold=True), Font(name=FONT, size=10)
        a.alignment = b.alignment = WRAP_TOP
        a.border = b.border = BORDER
        a.fill = PatternFill("solid", fgColor=ZEBRA)
        ov.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
        r += 1
    r += 1

kv_block("Basics", [
    ("Base URL", "Dev: http://localhost:8000 (api-gateway). Production: the public API host behind the load balancer. Clients only ever call the gateway."),
    ("Auth", "Authorization: Bearer <access_token> (EdDSA JWT, 15 min). Get tokens from /v1/auth/email/verify or /v1/auth/google; renew with /v1/auth/token/refresh."),
    ("Content type", "application/json for every request body and response (UTF-8)."),
    ("Envelope", '{"status":"success|error","code":<http status>,"message":str,"data":…,"error":null|{"type","request_id","details"?}}'),
    ("Errors", "Branch on error.type (stable). See the Error Codes sheet."),
    ("Tracing", "Every response has X-Request-Id; send your own (8–128 chars [A-Za-z0-9._:-]) to correlate client logs."),
    ("Rate limits", "429 + Retry-After seconds. Per IP on sign-in routes, per user on authenticated routes (Gateway Routes sheet)."),
    ("Formats", "Ids: UUID (v7, time-ordered). Timestamps: ISO-8601 UTC. Dates: YYYY-MM-DD."),
])
kv_block("Breaking changes from the pre-refactor API", [
    ("Prefix", "/v1/api/... is removed. Use /v1/auth/... and /v1/users/... ."),
    ("OTP", "email and code move from query parameters to a JSON body."),
    ("Tokens", "Access tokens expire after 15 minutes; clients must refresh. Refresh tokens rotate on every use."),
    ("Users", "/v1/users/user/{id} routes are gone; use /v1/users/me and /v1/users/me/profile. Others' data is read-only via /v1/users/{id}."),
    ("Email", "/v1/api/emails (open relay) is removed. Email is sent only by notification-service from internal events."),
])

ov.cell(row=r, column=1, value="Sheets").font = Font(name=FONT, size=12, bold=True, color=NAVY)
r += 1
hdr = ["Sheet", "What's in it", "Entries", "Counting rule"]
for c, h in enumerate(hdr, 1):
    cell = ov.cell(row=r, column=c, value=h)
    cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
    cell.fill = PatternFill("solid", fgColor=NAVY)
    cell.border = BORDER
r += 1
index = [
    ("Gateway Routes", "Public route table, auth, rate limits, edge behaviour", "=COUNTIF('Gateway Routes'!$A:$A,\"GW-0*\")", "Routed entries (GW-0n)"),
    ("Auth Service", "Sign-in, tokens and sessions", "=COUNTIF('Auth Service'!$A:$A,\"AUTH-0*\")", "Public endpoints (AUTH-0n)"),
    ("User Service", "Users and profiles", "=COUNTIF('User Service'!$A:$A,\"USER-0*\")", "Public endpoints (USER-0n)"),
    ("Notification Service", "Email delivery (event-driven, no public API)", "=COUNTIF('Notification Service'!$A:$A,\"NOTIF-E*\")", "Consumed commands (NOTIF-En)"),
    ("Events", "RabbitMQ event and command catalogue", "=COUNTIF(Events!$A:$A,\"EVT-*\")", "Routing keys (EVT-n)"),
    ("Error Codes", "Every error.type, status and client action", "=COUNTIF('Error Codes'!$B:$B,\">=400\")", "Error types (rows with a status)"),
    ("Schemas", "Field-level request, response, token and event schemas", "=COUNTIF(Schemas!$E:$E,\"Yes\")+COUNTIF(Schemas!$E:$E,\"No\")", "Fields documented"),
]
for name, what, formula, rule in index:
    a = ov.cell(row=r, column=1, value=name)
    a.hyperlink = f"#'{name}'!A1"
    a.font = Font(name=FONT, size=10, color="1F5FBF", underline="single")
    ov.cell(row=r, column=2, value=what).font = Font(name=FONT, size=10)
    n = ov.cell(row=r, column=3, value=formula)
    n.font, n.number_format = Font(name=FONT, size=10, bold=True), "0"
    ov.cell(row=r, column=4, value=rule).font = Font(name=FONT, size=9, color=SLATE)
    for c in range(1, 5):
        ov.cell(row=r, column=c).border = BORDER
        ov.cell(row=r, column=c).alignment = WRAP_TOP
    r += 1
ov.cell(row=r - len(index), column=3).comment = Comment("Counts are formulas over each sheet's ID column, so they update when rows are added.", "CoGo docs")
for col, w in zip("ABCD", (24, 70, 10, 30)):
    ov.column_dimensions[col].width = w

wb.move_sheet("Gateway Routes", offset=-(wb.sheetnames.index("Gateway Routes") - 1))
for ws in wb:
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
OUT.parent.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print("saved", OUT)
