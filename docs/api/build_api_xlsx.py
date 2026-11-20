"""Builds docs/api/CoGo-API-Reference.xlsx from the code, plus the curated prose in api_content.py.

From the code: every public endpoint, its parameters, request/response schemas and success status
(contracts/openapi/*.json), the gateway route table and each endpoint's edge auth and rate limit
(services/api-gateway/app/routes.py), the RabbitMQ topology and event contracts (cogo_common),
and the error types raised anywhere. The build FAILS if api_content.py misses or invents an
endpoint, a consumed event, an event, or an error type, so the workbook can't silently drift.

Run from libs/common (cogo_common importable):
    uv run --with openpyxl python ../../docs/api/build_api_xlsx.py \\
        ../../contracts/openapi ../../docs/api/CoGo-API-Reference.xlsx
"""

import importlib.util
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR, OUT = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(Path(__file__).parent))

import api_content as C  # noqa: E402
from cogo_common.events.envelope import EventEnvelope  # noqa: E402
from cogo_common.events.topology import ALL_QUEUES  # noqa: E402


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GW = _load("gateway_routes", ROOT / "services/api-gateway/app/routes.py")
EVENTS: dict[str, Any] = _load("event_schemas", ROOT / "scripts/export_event_schemas.py").EVENTS
SPECS = {s["key"]: json.loads((SPECS_DIR / f"{s['key']}-service.json").read_text()) for s in C.SERVICES}
QUEUE_OWNER = {"user-service": "user", "social-service": "social", "journey-service": "journey",
               "chat-service": "chat", "notification-service": "notification", "notification.email": "notification"}  # fmt: skip
problems: list[str] = []

# --------------------------------------------------------------------------- styling (unchanged)
FONT = "Arial"
NAVY, SLATE, LIGHT, ZEBRA = "1F3A5F", "44546A", "DCE6F1", "F5F8FC"
METHOD_FILL = {"GET": "DFF1E1", "POST": "DCE9F7", "PATCH": "FCE8D5", "PUT": "FCE8D5", "DELETE": "F8D9D9",
               "CONSUME": "EDE3F6", "ANY": "EEEEEE", "C→S": "DCE9F7", "S→C": "DFF1E1", "close": "F8D9D9", "error": "FCE8D5"}  # fmt: skip
thin = Side(style="thin", color="C9D3E0")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
WRAP_TOP = Alignment(wrap_text=True, vertical="top")
MONO_HINT = Font(name=FONT, size=9, color="333333")
wb = Workbook()


def sheet(title: str, heading: str, subtitle: str, headers: list[str], widths: list[int], rows: list[list[Any]],
          method_col: int | None = None, mono_cols: tuple[int, ...] = ()) -> Any:  # fmt: skip
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


# --------------------------------------------------------------------------- OpenAPI helpers
def clean(name: str) -> str:
    """FastAPI's mangled names for same-named models: app__api__places__PlaceOut -> PlaceOut (places)."""
    if name.startswith("app__"):
        parts = name.split("__")
        return parts[-1] if parts[-2] == "schemas" else f"{parts[-1]} ({parts[-2]})"
    return name


def ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def type_of(p: dict[str, Any]) -> str:
    if "$ref" in p:
        name = ref_name(p["$ref"])
        return f"Page[{clean(name[5:-1])}]" if name.startswith("Page_") else clean(name)
    if "anyOf" in p:
        return " | ".join(type_of(v) for v in p["anyOf"] if v.get("type") != "null") or "null"
    t = p.get("type", "any")
    if t == "array":
        return f"array of {type_of(p.get('items', {}))}"
    if t == "object" and isinstance(p.get("additionalProperties"), dict):
        key = "type" if "propertyNames" not in p else type_of(p["propertyNames"])
        return f"map {key} → {type_of(p['additionalProperties'])}"
    if "enum" in p:
        return "string (enum)"
    if "const" in p:
        return f"'{p['const']}'"
    return f"{t} ({p['format']})" if "format" in p else t


def constraints(p: dict[str, Any]) -> str:
    out: list[str] = []
    for v in [p, *p.get("anyOf", [])]:
        for k, label in (("minLength", "min length"), ("maxLength", "max length"), ("minimum", "≥"),
                         ("maximum", "≤"), ("minItems", "min items"), ("maxItems", "max items"),
                         ("pattern", "pattern")):  # fmt: skip
            if k in v:
                out.append(f"{label} {v[k]}")
        if "enum" in v:
            out.append(" | ".join(map(str, v["enum"])))
        if "enum" in v.get("propertyNames", {}):
            out.append("keys: " + " | ".join(map(str, v["propertyNames"]["enum"])))
    if "default" in p and p["default"] is not None:
        out.append(f"default {json.dumps(p['default'], ensure_ascii=False)}")
    return "; ".join(out)


def nullable(p: dict[str, Any]) -> bool:
    return any(v.get("type") == "null" for v in p.get("anyOf", []))


def refs_in(spec: dict[str, Any], node: Any, seen: set[str] | None = None) -> set[str]:
    """Every component schema reachable from a node (through nested refs)."""
    seen = set() if seen is None else seen
    if isinstance(node, dict):
        if "$ref" in node:
            name = ref_name(node["$ref"])
            if name not in seen:
                seen.add(name)
                refs_in(spec, spec["components"]["schemas"].get(name, {}), seen)
        for v in node.values():
            refs_in(spec, v, seen)
    elif isinstance(node, list):
        for v in node:
            refs_in(spec, v, seen)
    return seen


def op_facts(spec: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    op = spec["paths"][path][method.lower()]
    params = op.get("parameters", [])
    path_p = [p["name"] for p in params if p["in"] == "path"]
    query_p = [p["name"] + ("" if p.get("required") else "?") for p in params if p["in"] == "query"]
    bits = ([f"Path: {', '.join(path_p)}"] if path_p else []) + ([f"Query: {', '.join(query_p)}"] if query_p else [])
    body = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
    code = next(c for c in op["responses"] if c[0] in "23")  # 2xx, or a redirect
    resp = op["responses"][code].get("content", {}).get("application/json", {}).get("schema", {})
    data: dict[str, Any] = {}
    if "$ref" in resp:
        data = spec["components"]["schemas"][ref_name(resp["$ref"])].get("properties", {}).get("data", {})
    status = {"200": "200 OK", "201": "201 Created", "202": "202 Accepted", "303": "303 See Other"}.get(code, code)
    return {"params": "\n".join(bits) or "—", "req_schema": type_of(body) if body else "—",
            "resp_schema": type_of(data) if data else "—", "success": status,
            "refs": refs_in(spec, body) | refs_in(spec, data), "body_refs": refs_in(spec, body)}  # fmt: skip


def gateway_rule(method: str, path: str) -> Any:
    sample = re.sub(r"\{[^}]+\}", "0198f0c2-0000-7000-8000-000000000001", path)
    try:
        return GW.resolve(method, sample)
    except Exception:
        problems.append(f"gateway doesn't route {method} {path}")
        return None


def limit_text(route: Any, service_limit: str) -> str:
    gw = "—"
    if route is not None:
        who = "user" if route.limit_by == "user" else "IP"
        gw = f"Gateway: {route.limit.replace('/', ' / ')} per {who}" + ("" if route.fail_open else " (fails closed)")
    return gw + (f"\n{service_limit}" if service_limit else "")


# --------------------------------------------------------------------------- service sheets
SERVICE_HEADERS = ["ID", "Method", "Path", "Parameters (path · query)", "Summary", "Auth", "Request body (schema)",
                   "Request example", "Success", "Response data (schema)", "Response example",
                   "Errors (status error.type)", "Rate limits", "Notes"]  # fmt: skip
SERVICE_WIDTHS = [9, 9, 30, 22, 30, 15, 20, 34, 11, 22, 50, 40, 26, 46]
USED_BY: dict[tuple[str, str], set[str]] = {}
REQUEST_SCHEMAS: dict[tuple[str, str], bool] = {}
consumer_rows_count = 0

for svc in C.SERVICES:
    key, prefix = svc["key"], svc["prefix"]
    spec = SPECS[key]
    documented = {(e["method"], e["path"]) for e in C.ENDPOINTS[key]}
    actual = {(m.upper(), p) for p, item in spec["paths"].items() for m in item if m in ("get", "post", "put", "patch", "delete")}
    for missing in sorted(actual - documented):
        problems.append(f"{key}: {missing} is in the OpenAPI spec but not documented in api_content.py")
    for stale in sorted(documented - actual):
        problems.append(f"{key}: {stale} is documented but no longer in the OpenAPI spec")

    rows: list[list[Any]] = [["Public endpoints (through the gateway; base URL http://localhost:8000 in dev)"]]
    for e in C.ENDPOINTS[key]:
        if (e["method"], e["path"]) not in actual:
            continue
        facts = op_facts(spec, e["method"], e["path"])
        route = gateway_rule(e["method"], e["path"])
        for name in facts["refs"]:
            USED_BY.setdefault((key, name), set()).add(e["id"])
            REQUEST_SCHEMAS[(key, name)] = REQUEST_SCHEMAS.get((key, name), True) and name in facts["body_refs"]
        rows.append([e["id"], e["method"], e["path"], facts["params"], e["summary"],
                     "Bearer access token" if route is not None and route.auth else "None",
                     facts["req_schema"], e["req"], facts["success"], facts["resp_schema"], e["resp"],
                     e["errors"] or "—", limit_text(route, e["limit"]), e["notes"]])  # fmt: skip
    if C.INTERNAL.get(key):
        rows.append(["Internal and web endpoints (internal: service tokens only, never routed by the gateway)"])
        for e in C.INTERNAL[key]:
            rows.append([e["id"], e["method"], e["path"], "—", e["summary"], e["auth"], e["req_schema"], e["req"],
                         "200 OK", e["resp_schema"], e["resp"], e["errors"] or "—", "—", e["notes"]])  # fmt: skip
    rows.append([f"{prefix}-H1", "GET", "/health/live · /health/ready · /metrics", "—", "Liveness, readiness, Prometheus metrics",
                 "Internal", "—", "—", "200 / 503", "Health JSON / Prometheus text", "—", "503 when a dependency check fails", "—", C.HEALTH_NOTE])  # fmt: skip

    bound = [(spec_q.name, k) for spec_q in ALL_QUEUES if QUEUE_OWNER.get(spec_q.name) == key for k in spec_q.bindings]
    effects = C.CONSUMES.get(key, {})
    for _, k in bound:
        if k not in effects:
            problems.append(f"{key} consumes {k} (topology) but api_content.CONSUMES doesn't say what it does")
    for k in effects:
        if k not in {b for _, b in bound}:
            problems.append(f"api_content.CONSUMES[{key!r}] lists {k}, which its queue doesn't bind")
    if bound:
        rows.append(["Consumed events (RabbitMQ; see the Events sheet)"])
        for n, (queue, k) in enumerate(bound, 1):
            model = EVENTS[k].__name__ if k in EVENTS else "—"
            dedupe = "Redis key per event id (24 h)" if k == "notification.email.requested" else "processed_events (event id)"
            rows.append([f"{prefix}-E{n}", "CONSUME", k, "—", effects.get(k, ""), "Internal (RabbitMQ)", model, "—", "ack",
                         "—", "—", "Invalid payload → DLQ at once. Handler failure → retried with backoff, then nacked (DLQ after 5 deliveries).",
                         "Prefetch 20", f"Queue {queue} (quorum, DLQ {queue}.dlq). Idempotent: {dedupe}."])  # fmt: skip
            consumer_rows_count += 1
    sheet(svc["sheet"], svc["heading"], svc["subtitle"], SERVICE_HEADERS, SERVICE_WIDTHS, rows,
          method_col=2, mono_cols=(3, 8, 11))  # fmt: skip

# --------------------------------------------------------------------------- chat websocket
sheet("Chat WebSocket", "chat-ws  —  real-time protocol (/v1/ws)",
      "Plain WebSocket + JSON (not Socket.IO). Frames are {type, id?, data}. One socket per app session; messages and receipts persist through chat-api, so the REST and socket paths follow the same rules. chat-ws is stateless and scales horizontally (no sticky sessions).",
      ["ID", "Direction", "Type / code", "Frame (example) / meaning", "Reply", "Notes"],
      [8, 10, 18, 60, 44, 60], C.WS_FRAMES, method_col=2, mono_cols=(4, 5))  # fmt: skip

# --------------------------------------------------------------------------- gateway
UPSTREAM_NAME = {"auth": "auth-service", "user": "user-service", "journey": "journey-service", "social": "social-service",
                 "chat": "chat-service", "notification": "notification-service"}  # fmt: skip
METHOD_ORDER = ("GET", "POST", "PUT", "PATCH", "DELETE")
gw_rows: list[list[Any]] = [["Routed, in order: the first match wins. Anything not listed returns 404 at the edge."]]
for n, route in enumerate(GW.ROUTES, 1):
    if route.pattern is not None:
        shown, match = route.path[1:], "regex"
    elif route.path.endswith("/"):
        shown, match = route.path + "*", "prefix"
    else:
        shown, match = route.path, "exact"
    gw_rows.append([f"GW-R{n:02d}", " ".join(m for m in METHOD_ORDER if m in route.methods), shown, match, UPSTREAM_NAME[route.upstream],
                    "Yes (Bearer)" if route.auth else "No", route.limit.replace("/", " / "),
                    "User id" if route.limit_by == "user" else "Client IP",
                    "Fail open" if route.fail_open else "Fail closed (503)"])  # fmt: skip
gw_rows += C.GATEWAY_EXTRA
ws = sheet("Gateway Routes", "api-gateway  —  public route table",
           "The single public entry point (dev: http://localhost:8000). Generated from services/api-gateway/app/routes.py. Public paths equal upstream paths. Authenticated routes are verified at the edge (EdDSA via JWKS) and again by the service. Limits are sliding windows in Redis, shared by all gateway replicas.",
           ["ID", "Method(s)", "Public path", "Match", "Upstream", "Auth at edge", "Rate limit", "Limited per", "If Redis is down"],
           [9, 24, 52, 9, 20, 18, 13, 18, 20], gw_rows, method_col=2, mono_cols=(3,))  # fmt: skip
base = 5 + len(gw_rows) + 1
for i, (k, v) in enumerate(C.EDGE_NOTES):
    a, b = ws.cell(row=base + i, column=1, value=k), ws.cell(row=base + i, column=3, value=v)
    ws.merge_cells(start_row=base + i, start_column=1, end_row=base + i, end_column=2)
    ws.merge_cells(start_row=base + i, start_column=3, end_row=base + i, end_column=9)
    a.font = Font(name=FONT, size=10, bold=True, color=NAVY if i == 0 else "000000", underline="single" if i == 0 else None)
    b.font = Font(name=FONT, size=10)
    a.alignment = b.alignment = WRAP_TOP
    ws.row_dimensions[base + i].height = 28 if i else 18

# --------------------------------------------------------------------------- events
consumers: dict[str, list[tuple[str, str]]] = {}
for q in ALL_QUEUES:
    for k in q.bindings:
        consumers.setdefault(k, []).append((QUEUE_OWNER.get(q.name, q.name), q.name))
for k in sorted(set(EVENTS) ^ set(C.EVENT_INFO)):
    problems.append(f"event {k}: in {'contracts' if k in EVENTS else 'api_content.EVENT_INFO'} only")
event_ids: dict[str, str] = {}
events_rows: list[list[Any]] = []
for n, (k, model) in enumerate(EVENTS.items(), 1):
    kind, producer, notes = C.EVENT_INFO.get(k, ("", "", ""))
    who = consumers.get(k, [])
    event_ids[k] = f"EVT-{n:02d}"
    command = kind == "Command"
    events_rows.append([event_ids[k], k, kind, producer,
                        "Direct publish with confirms (mandatory)" if command else "Transactional outbox (same DB transaction as the change) → relay",
                        ", ".join(f"{s}-service" for s, _ in who) or "— (none yet: published without mandatory)",
                        ", ".join(q for _, q in who) or "—", "Quorum · delivery-limit 5" if who else "—",
                        ", ".join(f"{q}.dlq" for _, q in who) or "—", f"EventEnvelope + {model.__name__}",
                        "Redis key per event id (24 h)" if command else "processed_events (event id)", notes])  # fmt: skip
sheet("Events", "Event catalogue  —  RabbitMQ (exchange cogo.events, topic)",
      "Generated from cogo_common's topology and contracts. State crosses services only as messages; delivery is at-least-once, so every consumer is idempotent. Dead letters go to exchange cogo.dlx and a per-queue .dlq. JSON Schemas for every payload are in contracts/events/.",
      ["ID", "Routing key", "Kind", "Producer", "Published via", "Consumer(s)", "Queue", "Queue type", "Dead-letter queue", "Payload", "Idempotency", "Notes"],
      [8, 28, 13, 22, 34, 30, 30, 18, 32, 30, 24, 44], events_rows, mono_cols=(2, 7, 9))  # fmt: skip

# --------------------------------------------------------------------------- errors
documented_errors = [row[0] for row in C.ERRORS if len(row) > 1]
for dup in sorted({e for e in documented_errors if documented_errors.count(e) > 1}):
    problems.append(f"error {dup} is listed twice")
raised: set[str] = set()
for path in [*ROOT.glob("services/*/app/**/*.py"), *ROOT.glob("libs/common/src/**/*.py")]:
    if "services/mail" in str(path):
        continue
    text = path.read_text()
    raised |= set(re.findall(r'error_type="([a-z_]+)"', text))
    raised |= set(re.findall(r'error_type, default_message = (?:\(\s*)?\d+, "([a-z_]+)"', text))
raised -= {"error"}
for missing in sorted(raised - set(documented_errors)):
    problems.append(f"error type {missing} is raised in code but not in api_content.ERRORS")
sheet("Error Codes", "Error codes  —  error.type catalogue",
      'Every error uses the envelope {"status":"error","code":<http>,"message":str,"data":null,"error":{"type":str,"request_id":str,"details"?}}. Branch on error.type (stable), show message to humans, log request_id (also in the X-Request-Id header). This list is checked against every error type raised in the code.',
      ["error.type", "HTTP", "Raised by", "Meaning", "Client action"], [26, 8, 22, 70, 46], C.ERRORS, mono_cols=(1,))  # fmt: skip

# --------------------------------------------------------------------------- schemas
SKIP = ("Envelope", "HTTPValidationError", "ValidationError", "Page_")
schema_rows: list[list[Any]] = []
for svc in C.SERVICES:
    key = svc["key"]
    schema_rows.append([f"{svc['sheet'].replace(' Service', '').lower()}-service (generated from its OpenAPI spec)"])
    for name, sc in SPECS[key]["components"]["schemas"].items():
        if name.startswith(SKIP):
            continue
        shown, used = clean(name), ", ".join(sorted(USED_BY.get((key, name), set())))
        kind = "request" if REQUEST_SCHEMAS.get((key, name)) else "response"
        if "enum" in sc:
            schema_rows.append([shown, "enum", "(values)", "string", "—", "No", " | ".join(sc["enum"]), "Enumeration.", sc["enum"][0], used])
            continue
        required = set(sc.get("required", []))
        for field, prop in sc.get("properties", {}).items():
            desc, ex = C.DESC.get((key, shown, field), ("", ""))
            schema_rows.append([shown, kind, field, type_of(prop), "Yes" if field in required else "No",
                                "Yes" if nullable(prop) else "No", constraints(prop), desc or prop.get("description", ""), ex, used])  # fmt: skip
schema_rows += [
    ["Shared wire formats (cogo_common)"],
    ["Envelope (success)", "response", "status / code / message / data / error", "object", "Yes", "—", "status='success'; code mirrors HTTP status; error=null", "Wraps every JSON response body.", '{"status":"success","code":200,"message":"OK","data":{…},"error":null}', "all"],
    ["Envelope (error)", "response", "error.type / error.request_id / error.details", "object", "Yes", "—", "status='error'; data=null; details for 422 and some 403/409", "See Error Codes sheet.", '{"status":"error","code":401,"message":"Token expired","data":null,"error":{"type":"token_expired","request_id":"…"}}', "all"],
    ["Page", "response", "items / next_cursor", "object", "Yes", "—", "next_cursor null on the last page", "Every list endpoint.", '{"items":[…],"next_cursor":"eyJ0Ijoi…"}', "all lists"],
    ["AccessToken (JWT)", "token", "header: alg, kid, typ", "JOSE header", "Yes", "No", "alg=EdDSA (Ed25519); typ=at+jwt", "kid = RFC 7638 thumbprint of the signing key.", '{"alg":"EdDSA","kid":"…","typ":"at+jwt"}', "all Bearer routes"],
    ["AccessToken (JWT)", "token", "claims: sub, sid, iss, aud, iat, nbf, exp, jti", "JWT claims", "Yes", "No", "iss=cogo-auth; aud=cogo-api; exp = iat + 900", "sub = user id; sid = session id. Clients should treat the token as opaque.", '{"sub":"01a1…","sid":"…","iss":"cogo-auth","aud":"cogo-api"}', "all Bearer routes"],
    ["Event contracts (cogo_common.events.contracts; JSON Schemas in contracts/events/)"],
]  # fmt: skip
models: dict[str, tuple[dict[str, Any], set[str]]] = {}
envelope = EventEnvelope.model_json_schema()
models["EventEnvelope"] = (envelope, set(event_ids.values()))
for k, model in EVENTS.items():
    schema = model.model_json_schema()
    for name, sub in [(model.__name__, schema), *schema.get("$defs", {}).items()]:
        entry = models.setdefault(name, ({k: v for k, v in sub.items() if k != "$defs"}, set()))
        entry[1].add(event_ids[k])
for name, (sc, users) in models.items():
    required = set(sc.get("required", []))
    for field, prop in sc.get("properties", {}).items():
        schema_rows.append([name, "event", field, type_of(prop), "Yes" if field in required else "No",
                            "Yes" if nullable(prop) else "No", constraints(prop), prop.get("description", ""), "",
                            ", ".join(sorted(users)) if len(users) < 6 else f"{len(users)} events"])  # fmt: skip
sheet("Schemas", "Schemas  —  request, response, token and event fields",
      "Generated from each service's OpenAPI spec and the event contracts, so names, types and constraints match the code. Every response body below sits inside the envelope's data field. 'Used by' lists the endpoint or event ids.",
      ["Schema", "Kind", "Field", "Type", "Required", "Nullable", "Constraints", "Description", "Example", "Used by"],
      [24, 10, 26, 24, 9, 9, 32, 52, 30, 22], schema_rows, mono_cols=(3, 9))  # fmt: skip

# --------------------------------------------------------------------------- refuse to build a stale workbook
if problems:
    print("api_content.py is out of date with the code:", *problems, sep="\n  - ", file=sys.stderr)
    raise SystemExit(1)

# --------------------------------------------------------------------------- overview (first sheet)
branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
ov = wb.active
ov.title = "Overview"
ov.sheet_view.showGridLines = False
ov["A1"] = "CoGo Backend — API Reference (v1)"
ov["A1"].font = Font(name=FONT, size=18, bold=True, color=NAVY)
ov["A2"] = (f"Generated from the code on branch {branch or 'unknown'} ({date.today().isoformat()}): OpenAPI specs, the gateway "
            "route table, the event topology and contracts. Regenerate after any API change (docs/api/README.md).")  # fmt: skip
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


kv_block("Basics", C.BASICS)
kv_block("Breaking changes from the pre-refactor API", C.BREAKING)
ov.cell(row=r, column=1, value="Sheets").font = Font(name=FONT, size=12, bold=True, color=NAVY)
r += 1
for c, h in enumerate(["Sheet", "What's in it", "Entries", "Counting rule"], 1):
    cell = ov.cell(row=r, column=c, value=h)
    cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF")
    cell.fill = PatternFill("solid", fgColor=NAVY)
    cell.border = BORDER
r += 1
index = [("Gateway Routes", "Public route table, edge auth, rate limits, WebSocket, edge behaviour",
          "=COUNTIF('Gateway Routes'!$A:$A,\"GW-R*\")", "Routed HTTP entries (GW-Rnn); /v1/ws is GW-WS")]  # fmt: skip
what = {"auth": "Sign-in, tokens and sessions", "user": "Profile, tags, covers, media, public profiles",
        "journey": "Journeys, feed, matches, likes/comments/shares/hides, Places, share pages",
        "social": "Follows, blocks, reports", "chat": "Connection requests, connections, conversations, messages",
        "notification": "Device tokens, notification preferences; push and email (event-driven)"}  # fmt: skip
for svc in C.SERVICES:
    index.append((svc["sheet"], what[svc["key"]], f"=COUNTIF('{svc['sheet']}'!$C:$C,\"/v1/*\")", "Public endpoints (paths under /v1/)"))
    if svc["key"] == "chat":
        index.append(("Chat WebSocket", "Real-time frames, close codes and error codes",
                      "=COUNTIF('Chat WebSocket'!$B:$B,\"C→S\")+COUNTIF('Chat WebSocket'!$B:$B,\"S→C\")",
                      "Frames (client → server and server → client)"))  # fmt: skip
index += [
    ("Events", "RabbitMQ event and command catalogue", "=COUNTIF(Events!$A:$A,\"EVT-*\")", "Routing keys (EVT-nn)"),
    ("Error Codes", "Every error.type, status and client action", "=COUNTIF('Error Codes'!$B:$B,\">=400\")", "Error types (rows with a status)"),
    ("Schemas", "Field-level request, response, token and event schemas", "=COUNTIF(Schemas!$E:$E,\"Yes\")+COUNTIF(Schemas!$E:$E,\"No\")", "Fields documented"),
]  # fmt: skip
for name, desc, formula, rule in index:
    a = ov.cell(row=r, column=1, value=name)
    a.hyperlink = f"#'{name}'!A1"
    a.font = Font(name=FONT, size=10, color="1F5FBF", underline="single")
    ov.cell(row=r, column=2, value=desc).font = Font(name=FONT, size=10)
    n = ov.cell(row=r, column=3, value=formula)
    n.font, n.number_format = Font(name=FONT, size=10, bold=True), "0"
    ov.cell(row=r, column=4, value=rule).font = Font(name=FONT, size=9, color=SLATE)
    for c in range(1, 5):
        ov.cell(row=r, column=c).border = BORDER
        ov.cell(row=r, column=c).alignment = WRAP_TOP
    r += 1
ov.cell(row=r - len(index), column=3).comment = Comment(
    "Counts are formulas over each sheet's columns, so they update when rows are added.", "CoGo docs"
)
for col, w in zip("ABCD", (24, 70, 10, 34), strict=True):
    ov.column_dimensions[col].width = w

order = ["Overview", "Gateway Routes", *(s["sheet"] for s in C.SERVICES[:5]), "Chat WebSocket",
         C.SERVICES[5]["sheet"], "Events", "Error Codes", "Schemas"]  # fmt: skip
wb._sheets = [wb[name] for name in order]  # noqa: SLF001 - openpyxl has no public reorder
for sheet_ in wb:
    sheet_.page_setup.orientation = "landscape"
    sheet_.page_setup.fitToWidth, sheet_.page_setup.fitToHeight = 1, 0
    sheet_.sheet_properties.pageSetUpPr.fitToPage = True
OUT.parent.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print(f"saved {OUT}: {len(order)} sheets, {sum(len(C.ENDPOINTS[s['key']]) for s in C.SERVICES)} public endpoints, "
      f"{len(GW.ROUTES)} routes, {len(EVENTS)} events, {consumer_rows_count} consumers, {len(documented_errors)} error types")  # fmt: skip
