#!/usr/bin/env python3
"""Full end-to-end API test through the public gateway (`make up && make e2e`).

Four people use the app the way the product intends: sign up with an email code, set up their
profiles (photos, tags, city), post journeys, get matched, follow, like, comment, ask to join
a trip, chat over the WebSocket, block, report, and receive push notifications (mock FCM).

Every request is matched against the OpenAPI contracts (contracts/openapi/*.json). The run fails
if any check fails OR any public endpoint was never exercised, and prints the coverage.
Stdlib only; reuses the smoke test's helpers (HTTP, WebSocket, Mailpit).
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import smoke  # noqa: E402  (loads .env files; helpers)

ROOT = Path(__file__).resolve().parents[1]
GW = smoke.GATEWAY
WS_BASE = GW.replace("http", "ws", 1) + "/v1/ws?token="
IST = timezone(timedelta(hours=5, minutes=30))
RUN = uuid.uuid4().hex[:6]
NOIDA, DELHI, RISHIKESH, HARIDWAR = (
    "mock:noida-uttar-pradesh", "mock:delhi-delhi", "mock:rishikesh-uttarakhand", "mock:haridwar-uttarakhand",
)  # fmt: skip
MUMBAI, PANAJI, MANALI = "mock:mumbai-maharashtra", "mock:panaji-goa", "mock:manali-himachal-pradesh"

# ---------------------------------------------------------------- contract coverage
OPS: list[tuple[str, str, str, re.Pattern[str]]] = []
for spec_file in sorted((ROOT / "contracts" / "openapi").glob("*.json")):
    spec = json.loads(spec_file.read_text())
    for path, item in spec["paths"].items():
        for method in item:
            if method in ("get", "post", "put", "patch", "delete"):
                pattern = re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}")) + "$")
                OPS.append((spec_file.stem, method.upper(), path, pattern))
HITS: dict[tuple[str, str], int] = {}
RESULTS: list[tuple[str, str, bool]] = []
SECTION = [""]


def track(method: str, path: str) -> None:
    bare = path.split("?", 1)[0]
    for svc, m, template, pattern in OPS:
        if m == method and pattern.match(bare):
            HITS[(svc, f"{m} {template}")] = HITS.get((svc, f"{m} {template}"), 0) + 1
            return


def api(method: str, path: str, body: Any = None, token: str | None = None,
        headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:  # fmt: skip
    track(method, path)
    return smoke.call(method, GW + path, body, token, headers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # an in-app browser hands cogo:// links to the app; we just read Location


def browse(method: str, url: str, form: str | None = None) -> tuple[int, dict[str, str], str]:
    """What the in-app browser does on the eKYC provider's page (HTML, forms, redirects)."""
    track(method, url.removeprefix(GW))
    body = form.encode() if form is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=10) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode(errors="replace")


def totp(secret: str, at: float) -> str:
    """RFC 6238 (SHA-1, 30 s, 6 digits), as an authenticator app computes it."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, int(at // 30).to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(int.from_bytes(digest[offset:offset + 4], 'big') & 0x7FFFFFFF) % 1_000_000:06d}"


class Admin:
    """The dev bootstrap admin (ADMIN_BOOTSTRAP_* from .env/.env.secrets), signed in once."""

    def __init__(self) -> None:
        self.email = os.environ.get("ADMIN_BOOTSTRAP_EMAIL", "admin@cogo.local")
        self.token = ""

    def login(self) -> bool:
        secret = os.environ.get("ADMIN_BOOTSTRAP_TOTP_SECRET", "")
        password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD", "")
        for offset in (0, 30, -30):  # a code is accepted once (a rerun may have used this one)
            body = {"email": self.email, "password": password, "totp_code": totp(secret, time.time() + offset)}
            status, _, resp = api("POST", "/admin/auth/login", body)
            if status == 200:
                self.token = resp["data"]["token"]
                return True
        return False

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, str], Any]:
        return api(method, path, body, headers={"Authorization": f"Bearer {self.token}"})

    def set_config(self, key: str, value: Any) -> bool:
        current = data(self.call("GET", f"/admin/config/{key}")) or {}
        status, _, _ = self.call("PUT", f"/admin/config/{key}", {"value": value, "version": current.get("version")})
        time.sleep(1)  # services drop their cached value on the config:updated broadcast
        return status == 200


ADMIN = Admin()


def admin_find(path: str, pred: Callable[[dict[str, Any]], bool], pages: int = 40) -> dict[str, Any] | None:
    """Page through an admin list (older runs' items may come first) for a matching item."""
    cursor = None
    for _ in range(pages):
        sep = "&" if "?" in path else "?"
        page = data(ADMIN.call("GET", path + (f"{sep}cursor={cursor}" if cursor else ""))) or {}
        for item in page.get("items", []):
            if pred(item):
                return dict(item)
        cursor = page.get("next_cursor")
        if not cursor:
            return None
    return None


def new_code(email: str, previous: str | None, timeout: float = 20.0) -> str:
    """The newest emailed code once it differs from `previous` (the inbox already has some)."""
    deadline = time.time() + timeout
    while True:
        code = smoke.otp_for(email, timeout=timeout)
        if code != previous or time.time() > deadline:
            return code
        time.sleep(0.5)


def section(name: str) -> None:
    SECTION[0] = name
    print(f"\n{name}")


def check(name: str, ok: bool, detail: Any = "") -> bool:
    RESULTS.append((SECTION[0], name, bool(ok)))
    shown = json.dumps(detail, default=str)[:400] if not isinstance(detail, str) else detail[:400]
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -> {shown}"))
    return bool(ok)


def eventually(probe: Callable[[], Any], timeout: float = 12.0, every: float = 0.5) -> Any:
    """Events settle in < 2 s; poll instead of sleeping a fixed time."""
    deadline = time.time() + timeout
    while True:
        value = probe()
        if value or time.time() > deadline:
            return value
        time.sleep(every)


def data(resp: tuple[int, dict[str, str], Any]) -> Any:
    return ((resp[2] or {}).get("data")) if isinstance(resp[2], dict) else None


def err(resp: tuple[int, dict[str, str], Any]) -> tuple[int, str | None]:
    body = resp[2] if isinstance(resp[2], dict) else {}
    return resp[0], ((body.get("error") or {}).get("type"))


@dataclass(eq=False)  # identity semantics: usable as a dict key
class User:
    name: str
    gender: str
    email: str = ""
    id: str = ""
    token: str = ""
    refresh: str = ""
    journeys: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.email = f"e2e-{self.name.lower()}-{RUN}@example.com"


def upload(user: User, kind: str, content: bytes = b"0" * 512) -> str | None:
    """Presigned PUT straight to object storage, then confirm (USER-06/07)."""
    up = data(api("POST", "/v1/media/upload-url", {"kind": kind, "content_type": "image/png"}, user.token)) or {}
    if not up.get("upload_url"):
        return None
    req = urllib.request.Request(up["upload_url"], data=b"\x89PNG\r\n\x1a\n" + content, method="PUT",
                                 headers={"Content-Type": "image/png"})  # fmt: skip
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status != 200:
            return None
    confirmed = data(api("POST", f"/v1/media/{up['media_id']}/confirm", token=user.token)) or {}
    return confirmed.get("file_url") if confirmed.get("status") == "ready" else None


def pushes(user: User) -> list[dict[str, Any]]:
    return list(data(api("GET", "/v1/me/devices/test-pushes", token=user.token)) or [])


def pushed(user: User, fragment: str, timeout: float = 12.0) -> bool:
    return bool(eventually(lambda: any(fragment in p["body"] for p in pushes(user)), timeout))


def journey_body(origin: str, dest: str, start: datetime, hours: int = 30, **extra: Any) -> dict[str, Any]:
    return {"origin_place_id": origin, "destination_place_id": dest, "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=hours)).isoformat(), "who_can_join": "anyone", "group_size": 4, **extra}  # fmt: skip


def match_ids(user: User, journey_id: str, query: str = "") -> list[str]:
    return [m["id"] for m in (data(api("GET", f"/v1/journeys/{journey_id}/matches{query}", token=user.token)) or {}).get("items", [])]


def feed_ids(user: User, query: str) -> set[str]:
    """Every journey id in the user's feed for this query, across all pages: the dev database
    also holds seed and load-test journeys, so a trip can rank past the first page."""
    ids: set[str] = set()
    cursor = None
    for _ in range(30):
        path = f"/v1/feed?{query}" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        page = data(api("GET", path, token=user.token)) or {}
        ids |= {i["journey"]["id"] for i in page.get("items", []) if i.get("type") == "journey"}
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return ids


# ======================================================================== scenario
PEOPLE: list["User"] = []


def cleanup() -> None:
    """Delete every journey this run created (also after a crash), so reruns start clean."""
    for user in PEOPLE:
        for jid in user.journeys.values():
            if jid:
                smoke.call("DELETE", f"{GW}/v1/journeys/{jid}", token=user.token)


def main() -> int:
    try:
        return scenario()
    finally:
        cleanup()


def scenario() -> int:
    asha, ravi, neha, kabir = User("Asha", "female"), User("Ravi", "male"), User("Neha", "female"), User("Kabir", "male")
    people = (asha, ravi, neha, kabir)
    PEOPLE.extend(people)

    # ------------------------------------------------------------ gateway edge
    section("Gateway edge")
    check("stack is ready", api("GET", "/health/ready")[0] == 200)
    check("unknown route -> 404", err(api("GET", "/v1/nope")) == (404, "not_found"))
    check("wrong method -> 405", err(api("DELETE", "/v1/feed")) == (405, "method_not_allowed"))
    check("no token -> 401 missing_token", err(api("GET", "/v1/me")) == (401, "missing_token"))
    check("forged token -> 401", api("GET", "/v1/me", token="eyJhbGciOiJub25lIn0.e30.x")[0] == 401)
    check("encoded traversal -> 400 bad_path", err(api("GET", "/v1/journeys/%2e%2e/me")) == (400, "bad_path"))
    check("internal endpoints are never routed", api("POST", "/internal/relations/follow-status", {})[0] == 404)
    rid = f"e2e-{RUN}-request-id"
    check("X-Request-Id is echoed", api("GET", "/health/live", headers={"X-Request-Id": rid})[1].get("x-request-id") == rid)

    # ------------------------------------------------------------ registration
    section("Registration and sign-in (auth-service)")
    def send_code(email: str) -> tuple[int, dict[str, str], Any]:
        """Like the app: on the gateway's per-IP limit (5/min), wait Retry-After and retry."""
        for _ in range(3):
            resp = api("POST", "/v1/auth/email/send", {"email": email})
            if err(resp) != (429, "rate_limited"):
                return resp
            wait = int(resp[1].get("retry-after", "10"))
            print(f"  ....  gateway sign-in limit hit (5/min per IP): waiting {wait}s as Retry-After says")
            time.sleep(wait + 1)
        return resp

    for i, user in enumerate(people):
        status, _, body = send_code(user.email)
        check(f"{user.name}: sign-in code requested", status == 202 and body["data"]["resend_after"] == 60, body)
        if i == 0:
            again = send_code(user.email)  # through the gateway's per-IP window, to the service
            check("a second code within 60 s is refused (otp_cooldown + Retry-After)",
                  err(again) == (429, "otp_cooldown") and int(again[1].get("retry-after", 0)) > 0, again[2])
        code = smoke.otp_for(user.email)
        if i == 0:
            wrong = "000000" if code != "000000" else "111111"
            check("a wrong code is rejected (otp_invalid)",
                  err(api("POST", "/v1/auth/email/verify", {"email": user.email, "code": wrong})) == (400, "otp_invalid"))
        status, _, body = api("POST", "/v1/auth/email/verify", {"email": user.email.upper(), "code": code})
        tokens = (body or {}).get("data") or {}
        check(f"{user.name}: verified (email case-insensitive), new account, token pair",
              status == 200 and tokens.get("is_new_user") is True and tokens.get("expires_in") == 900, body)
        user.id, user.token, user.refresh = tokens.get("user_id", ""), tokens.get("access_token", ""), tokens.get("refresh_token", "")
    status, kind = err(api("POST", "/v1/auth/google", {"id_token": "not-a-firebase-token"}))
    check("Google sign-in rejects an invalid Firebase token", (status, kind) in ((401, "google_token_invalid"), (503, "provider_disabled")), kind)

    # ------------------------------------------------------------ devices + prefs (before anything pushes)
    section("Push devices and preferences (notification-service)")
    for user in people:
        status, _, body = api("POST", "/v1/me/devices", {"token": f"e2e-{RUN}-{user.name}-phone", "platform": "android", "app_version": "1.0.0"}, user.token)
        check(f"{user.name}: device registered", status == 200, body)
    prefs = (data(api("GET", "/v1/me/notification-prefs", token=asha.token)) or {}).get("prefs", {})
    check("all 8 notification types default to on", len(prefs) == 8 and all(prefs.values()), prefs)
    updated = (data(api("PUT", "/v1/me/notification-prefs", {"prefs": {"trip_reminder": False}}, kabir.token)) or {}).get("prefs", {})
    check("a partial PUT changes only that type", updated.get("trip_reminder") is False and updated.get("message") is True, updated)
    check("unknown preference type -> 422", api("PUT", "/v1/me/notification-prefs", {"prefs": {"spam": True}}, kabir.token)[0] == 422)
    spare = f"e2e-{RUN}-old-phone"
    api("POST", "/v1/me/devices", {"token": spare, "platform": "ios"}, kabir.token)
    check("a device can be unregistered (logout)", api("DELETE", "/v1/me/devices", {"token": spare}, kabir.token)[0] == 200)

    # ------------------------------------------------------------ profiles
    section("Profiles, tags and photos (user-service)")
    me = data(api("GET", "/v1/me", token=asha.token)) or {}
    check("GET /v1/me returns the new account", me.get("id") == asha.id and me.get("account_status") == "active", me)
    tags = data(api("GET", "/v1/personality-tags", token=asha.token)) or []
    check("personality tags are available", len(tags) >= 6, len(tags))
    tag_ids = [t["id"] for t in tags]
    profiles = {
        asha: {"name": "Asha Rao", "dob": "1996-04-02", "bio": "Weekend hills, early starts", "profession": "Architect", "personality_tag_ids": tag_ids[:3]},
        ravi: {"name": "Ravi Kumar", "dob": "1994-09-15", "bio": "Rafting and road trips", "profession": "Engineer", "personality_tag_ids": tag_ids[:3]},
        neha: {"name": "Neha Tiwari", "dob": "1998-01-20", "bio": "Temples and trails", "profession": "Designer", "personality_tag_ids": tag_ids[1:4]},
        kabir: {"name": "Kabir Shah", "dob": "1980-06-30", "bio": "Beaches only", "profession": "Chef", "personality_tag_ids": tag_ids[3:6]},
    }  # fmt: skip
    for user, profile in profiles.items():
        body = {**profile, "username": f"{user.name.lower()}_{RUN}", "gender": user.gender, "city_place_id": NOIDA}
        status, _, resp = api("PATCH", "/v1/me", body, user.token)
        out = (resp or {}).get("data") or {}
        check(f"{user.name}: profile set (username, city resolved via Places, 3 tags)",
              status == 200 and out.get("city", {}).get("name") == "Noida" and len(out.get("personality_tags", [])) == 3, resp)
    check("a taken username -> 409 username_taken",
          err(api("PATCH", "/v1/me", {"username": f"asha_{RUN}"}, ravi.token)) == (409, "username_taken"))
    check("an unknown tag -> 400 invalid_tags",
          err(api("PATCH", "/v1/me", {"personality_tag_ids": [987654, tag_ids[0], tag_ids[1]]}, ravi.token)) == (400, "invalid_tags"))
    check("an empty PATCH -> 400 empty_update", err(api("PATCH", "/v1/me", {}, ravi.token)) == (400, "empty_update"))
    avatar = upload(asha, "avatar")
    status, _, resp = api("PATCH", "/v1/me", {"avatar_url": avatar}, asha.token)
    try:
        with urllib.request.urlopen(avatar or "http://invalid", timeout=5) as r:
            served = r.status == 200
    except Exception:
        served = False
    check("avatar: presigned upload -> confirm -> set -> publicly served", bool(avatar) and status == 200 and served, resp)
    cover = upload(asha, "cover")
    covers = data(api("PUT", "/v1/me/cover-photos", [{"position": 0, "url": cover}], asha.token)) or []
    check("cover photo set", [c["url"] for c in covers] == [cover], covers)
    check("an avatar URL that isn't your upload -> 400 invalid_media",
          err(api("PATCH", "/v1/me", {"avatar_url": avatar}, ravi.token)) == (400, "invalid_media"))
    api("POST", "/v1/me/guide/seen", token=asha.token)
    me = data(api("GET", "/v1/me", token=asha.token)) or {}
    check("first-run guide marked seen; age computed; KYC prompt shown", me.get("guide_seen") is True and me.get("age") == 30
          and me.get("kyc_prompt", {}).get("show") is True, me)  # fmt: skip
    legacy = data(api("GET", "/v1/users/me", token=asha.token)) or {}
    check("legacy GET /v1/users/me still works (app v0)", (legacy.get("profile") or {}).get("full_name") == "Asha Rao", legacy)
    status, _, resp = api("PATCH", "/v1/users/me/profile", {"bio": "Weekend hills, early starts!"}, asha.token)
    check("legacy PATCH /v1/users/me/profile still works", status == 200 and resp["data"]["profile"]["bio"].endswith("!"), resp)

    # ------------------------------------------------------------ places
    section("Places (journey-service proxy)")
    sugg = data(api("GET", "/v1/places/autocomplete?q=rish&session_token=e2e", token=asha.token)) or []
    check("autocomplete finds Rishikesh", any(s["place_id"] == RISHIKESH for s in sugg), sugg)
    place = data(api("GET", f"/v1/places/{RISHIKESH}?session_token=e2e", token=asha.token)) or {}
    check("place details with coordinates", place.get("name") == "Rishikesh" and 29 < place.get("lat", 0) < 31, place)
    check("a 1-character query -> 422", api("GET", "/v1/places/autocomplete?q=r", token=asha.token)[0] == 422)

    # ------------------------------------------------------------ follows
    section("Follows (social-service)")
    for fan in (ravi, neha):
        check(f"{fan.name} follows Asha", data(api("POST", f"/v1/users/{asha.id}/follow", token=fan.token)) == {"following": True})
    check("following yourself -> 400", err(api("POST", f"/v1/users/{asha.id}/follow", token=asha.token)) == (400, "cannot_follow_self"))
    followers = eventually(lambda: {u["id"] for u in (data(api("GET", f"/v1/users/{asha.id}/followers", token=kabir.token)) or {}).get("items", [])} >= {ravi.id, neha.id} and True)
    check("Asha's followers list (names from social's projection)", bool(followers))
    following = (data(api("GET", f"/v1/users/{ravi.id}/following", token=ravi.token)) or {}).get("items", [])
    check("Ravi's following list", [u["id"] for u in following] == [asha.id], following)
    check("follower count reaches the profile through events",
          bool(eventually(lambda: (data(api("GET", "/v1/me", token=asha.token)) or {}).get("stats", {}).get("followers") == 2)))
    check("Asha got a 'started following you' push", pushed(asha, "started following you"))
    check("unfollow", data(api("DELETE", f"/v1/users/{asha.id}/follow", token=neha.token)) == {"following": False})
    public = data(api("GET", f"/v1/users/{asha.id}", token=ravi.token)) or {}
    check("public profile: no dob/gender, is_following=true, connection_status=none",
          "dob" not in public and "gender" not in public and public.get("is_following") is True
          and public.get("connection_status") == "none" and public.get("age") == 30, public)  # fmt: skip

    # ------------------------------------------------------------ journeys
    section("Journeys (journey-service)")
    start = (datetime.now(UTC) + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    check("start < 15 min from now -> 400 invalid_start",
          err(api("POST", "/v1/journeys", journey_body(NOIDA, RISHIKESH, datetime.now(UTC) + timedelta(minutes=5)), asha.token)) == (400, "invalid_start"))
    check("same origin and destination -> 400 same_route",
          err(api("POST", "/v1/journeys", journey_body(NOIDA, NOIDA, start), asha.token)) == (400, "same_route"))
    check("group of 1 -> 400 invalid_group_size",
          err(api("POST", "/v1/journeys", journey_body(NOIDA, RISHIKESH, start, group_size=1), asha.token)) == (400, "invalid_group_size"))
    plans = [
        (asha, "main", journey_body(NOIDA, RISHIKESH, start, caption="Weekend rafting, splitting fuel")),
        (ravi, "main", journey_body(DELHI, RISHIKESH, start)),
        (neha, "main", journey_body(NOIDA, HARIDWAR, start + timedelta(hours=6))),
        (neha, "women", journey_body(NOIDA, RISHIKESH, start, who_can_join="women_only")),
        (kabir, "main", journey_body(MUMBAI, PANAJI, start)),
    ]
    for user, key, body in plans:
        status, _, resp = api("POST", "/v1/journeys", body, user.token)
        out = (resp or {}).get("data") or {}
        user.journeys[key] = out.get("id", "")
        check(f"{user.name} posts {key}: {out.get('origin', {}).get('name')} → {out.get('destination', {}).get('name')}",
              status == 201 and out.get("matches", {}).get("state") == "pending" and out.get("seats_left") == 3, resp)
    a_j, r_j, n_j, nw_j, k_j = asha.journeys["main"], ravi.journeys["main"], neha.journeys["main"], neha.journeys["women"], kabir.journeys["main"]
    detail = data(api("GET", f"/v1/journeys/{a_j}", token=ravi.token)) or {}
    check("journey detail: host summary, vibe match, no coordinates, not own",
          detail.get("host", {}).get("name") == "Asha Rao" and isinstance(detail.get("vibe_match"), int)
          and detail.get("is_own") is False and "lat" not in json.dumps(detail), detail)  # fmt: skip
    mine = (data(api("GET", "/v1/me/journeys?tab=upcoming", token=neha.token)) or {}).get("items", [])
    check("Your Journeys (upcoming)", {j["id"] for j in mine} == {n_j, nw_j}, [j["id"] for j in mine])
    check("Your Journeys (completed) is empty", (data(api("GET", "/v1/me/journeys?tab=completed&limit=3", token=neha.token)) or {}).get("items") == [])
    theirs = (data(api("GET", f"/v1/users/{asha.id}/journeys?tab=upcoming", token=kabir.token)) or {}).get("items", [])
    check("another user's journeys", [j["id"] for j in theirs] == [a_j], theirs)
    check("only the host can edit -> 403 not_owner",
          err(api("PATCH", f"/v1/journeys/{a_j}", {"caption": "mine now"}, ravi.token)) == (403, "not_owner"))

    # ------------------------------------------------------------ matching
    section("Matching (spec §5.4): who matches whom")
    banner = eventually(lambda: (b := (data(api("GET", f"/v1/journeys/{a_j}", token=asha.token)) or {}).get("matches", {}))
                        and b.get("state") == "done" and b.get("count", 0) >= 3 and b)  # fmt: skip
    check("Asha's banner fills in: done, ≥ 3 matches, best score", bool(banner) and (banner or {}).get("best_score", 0) >= 80, banner)
    matches = (data(api("GET", f"/v1/journeys/{a_j}/matches", token=asha.token)) or {}).get("items", [])
    ids = [m["id"] for m in matches]
    check("Asha matches Ravi (Delhi, 20 km away), Neha → Haridwar (19 km), Neha's women-only trip",
          {r_j, n_j, nw_j} <= set(ids), ids)
    check("...but not Kabir (Mumbai → Goa)", k_j not in ids)
    scores = [m["journey_match"] for m in matches]
    check("matches are sorted by Journey Match, all ≥ 50, each with a Vibe Match",
          scores == sorted(scores, reverse=True) and min(scores or [0]) >= 50 and all(isinstance(m["vibe_match"], int) for m in matches), scores)
    ravi_ids = match_ids(ravi, r_j)
    check("mutual eligibility: Ravi (male) doesn't match Neha's women-only trip", a_j in ravi_ids and nw_j not in ravi_ids, ravi_ids)
    women = (data(api("GET", f"/v1/journeys/{a_j}/matches?who_can_join=women_only", token=asha.token)) or {}).get("items", [])
    check("filter who_can_join=women_only: only women-only trips, Neha's included",
          nw_j in [m["id"] for m in women] and all(m["who_can_join"]["value"] == "women_only" for m in women), women)
    check("only the host can list matches -> 403 not_owner", err(api("GET", f"/v1/journeys/{a_j}/matches", token=ravi.token)) == (403, "not_owner"))
    check("Asha (who posted first) got a 'New match' push", pushed(asha, "New match for your trip to Rishikesh"))
    status, _, resp = api("PATCH", f"/v1/journeys/{k_j}", {"origin_place_id": NOIDA, "destination_place_id": RISHIKESH}, kabir.token)
    check("Kabir re-routes his trip to Noida → Rishikesh (matching re-runs)",
          status == 200 and resp["data"]["matches"]["state"] == "pending", resp)
    check("...and now appears in Asha's matches", bool(eventually(lambda: k_j in match_ids(asha, a_j))))
    api("PATCH", f"/v1/journeys/{k_j}", {"caption": "Changed plans!"}, kabir.token)
    check("a caption-only edit doesn't re-run matching",
          (data(api("GET", f"/v1/journeys/{k_j}", token=kabir.token)) or {}).get("matches", {}).get("state") == "done")

    # ------------------------------------------------------------ feed
    section("Home feed (ranked, filtered)")
    ravis = feed_ids(ravi, f"to_place_id={RISHIKESH}")
    check("Ravi's feed has Asha's trip, not his own, not the women-only one", a_j in ravis and r_j not in ravis and nw_j not in ravis)
    check("Asha (female) does see the women-only trip", nw_j in feed_ids(asha, f"to_place_id={RISHIKESH}"))
    day = start.astimezone(IST).date()
    on_day = feed_ids(ravi, f"to_place_id={RISHIKESH}&date_from={day}&date_to={day}")
    later = feed_ids(ravi, f"to_place_id={RISHIKESH}&date_from={day + timedelta(days=20)}")
    check("date filter (IST dates) includes the day and excludes later dates", a_j in on_day and a_j not in later)
    check("people filter: 3 free seats needed", a_j in feed_ids(ravi, f"to_place_id={RISHIKESH}&people=3"))
    check("a bad date range -> 400 invalid_filter", err(api("GET", "/v1/feed?date_from=2026-12-10&date_to=2026-12-01", token=ravi.token)) == (400, "invalid_filter"))
    page = data(api("GET", "/v1/feed", token=kabir.token)) or {}
    if page.get("next_cursor"):
        nxt = api("GET", f"/v1/feed?cursor={page['next_cursor']}", token=kabir.token)
        check("feed paging: the cursor returns the next page", nxt[0] == 200 and not ({i["journey"]["id"] for i in page["items"]} & {i["journey"]["id"] for i in data(nxt)["items"]}))
        check("a cursor reused with other filters -> 400 invalid_cursor",
              err(api("GET", f"/v1/feed?cursor={page['next_cursor']}&people=2", token=kabir.token)) == (400, "invalid_cursor"))

    # ------------------------------------------------------------ engagement
    section("Likes, comments, shares, hides")
    check("Ravi likes Asha's trip", data(api("POST", f"/v1/journeys/{a_j}/like", token=ravi.token)) == {"liked": True, "likes": 1})
    check("liking twice is idempotent", data(api("POST", f"/v1/journeys/{a_j}/like", token=ravi.token)) == {"liked": True, "likes": 1})
    check("Neha likes it too", (data(api("POST", f"/v1/journeys/{a_j}/like", token=neha.token)) or {}).get("likes") == 2)
    check("unlike", data(api("DELETE", f"/v1/journeys/{a_j}/like", token=neha.token)) == {"liked": False, "likes": 1})
    check("Asha got a like push", pushed(asha, "liked your trip"))
    status, _, resp = api("POST", f"/v1/journeys/{a_j}/comments", {"body": "  Count me in for the rafting!  "}, ravi.token)
    check("Ravi comments (trimmed)", status == 201 and resp["data"]["body"] == "Count me in for the rafting!", resp)
    neha_comment = data(api("POST", f"/v1/journeys/{a_j}/comments", {"body": "Is there space for two?"}, neha.token)) or {}
    comments = (data(api("GET", f"/v1/journeys/{a_j}/comments", token=asha.token)) or {}).get("items", [])
    check("comments list, oldest first, host can delete any", [c["body"] for c in comments] == ["Count me in for the rafting!", "Is there space for two?"]
          and all(c["can_delete"] for c in comments), comments)  # fmt: skip
    check("someone else can't delete it -> 403 not_allowed", err(api("DELETE", f"/v1/comments/{neha_comment.get('id')}", token=ravi.token)) == (403, "not_allowed"))
    check("the host deletes it", api("DELETE", f"/v1/comments/{neha_comment.get('id')}", token=asha.token)[0] == 200)
    check("Asha got a comment push", pushed(asha, "commented on your trip"))
    share = data(api("POST", f"/v1/journeys/{a_j}/share", {"channel": "whatsapp"}, ravi.token)) or {}
    with urllib.request.urlopen(f"{GW}/j/{a_j}", timeout=5) as r:
        page_html = r.read().decode()
    check("share link + public preview page (no token, first name only)",
          share.get("url", "").endswith(f"/j/{a_j}") and "Rishikesh" in page_html and "Asha" in page_html and "Rao" not in page_html)
    counts = (data(api("GET", f"/v1/journeys/{a_j}", token=asha.token)) or {}).get("counts", {})
    check("counters: 1 like, 1 comment, 1 share", counts == {"likes": 1, "comments": 1, "shares": 1}, counts)
    check("Kabir hides Asha's trip", api("POST", f"/v1/journeys/{a_j}/hide", token=kabir.token)[0] == 200)
    check("...it leaves his feed", a_j not in feed_ids(kabir, f"to_place_id={RISHIKESH}"))
    check("...and his matches", a_j not in match_ids(kabir, k_j))
    api("DELETE", f"/v1/journeys/{a_j}/hide", token=kabir.token)
    check("unhide brings it back", a_j in feed_ids(kabir, f"to_place_id={RISHIKESH}"))
    check("hiding your own -> 400 cannot_hide_own", err(api("POST", f"/v1/journeys/{a_j}/hide", token=asha.token)) == (400, "cannot_hide_own"))

    # ------------------------------------------------------------ chat
    section("Connection requests (chat-service)")
    asha_ws = smoke.WebSocket(WS_BASE + asha.token)
    asha_ws.send("ping", id_="p1")
    check("Asha's socket is up", asha_ws.recv("pong")["id"] == "p1")
    status, _, resp = api("POST", "/v1/connection-requests",
                          {"target_journey_id": a_j, "sender_journey_id": r_j, "message": "Hi Asha! Same weekend. WhatsApp +91 98765 43210"}, ravi.token)  # fmt: skip
    req = (resp or {}).get("data") or {}
    check("Ravi asks to join: phone masked, Journey Match from his own trip, Vibe Match",
          status == 201 and req.get("message", "").endswith("•••") and (req.get("journey_match") or 0) >= 50
          and isinstance(req.get("vibe_match"), int), resp)  # fmt: skip
    try:
        live = asha_ws.recv("request.new")["data"]
        check("Asha sees it live on her socket", live["id"] == req.get("id") and live["user"]["id"] == ravi.id)
    except Exception as exc:
        check("Asha sees it live on her socket", False, str(exc))
    check("...and as a push", pushed(asha, "wants to join your trip Noida → Rishikesh"))
    incoming = (data(api("GET", "/v1/connection-requests?box=incoming", token=asha.token)) or {}).get("items", [])
    check("Asha's incoming requests", [r["id"] for r in incoming] == [req.get("id")], incoming)
    outgoing = (data(api("GET", "/v1/connection-requests?box=outgoing", token=ravi.token)) or {}).get("items", [])
    check("Ravi's sent requests", outgoing and outgoing[0]["status"] == "pending", outgoing)
    check("chat badge counts the pending request", (data(api("GET", "/v1/conversations/badge", token=asha.token)) or {}).get("pending_requests") == 1)
    check("a second request in the pair -> 409 pending_exists",
          err(api("POST", "/v1/connection-requests", {"target_journey_id": a_j, "message": "again"}, ravi.token)) == (409, "pending_exists"))
    check("eligibility: Ravi can't ask to join a women-only trip -> 403 not_eligible",
          err(api("POST", "/v1/connection-requests", {"target_journey_id": nw_j, "message": "hi"}, ravi.token)) == (403, "not_eligible"))
    check("asking to join your own trip -> 400 cannot_request_own",
          err(api("POST", "/v1/connection-requests", {"target_journey_id": a_j, "message": "hi"}, asha.token)) == (400, "cannot_request_own"))
    status, _, resp = api("POST", f"/v1/connection-requests/{req.get('id')}/accept", token=asha.token)
    conversation = (data((status, {}, resp)) or {}).get("conversation_id")
    check("Asha accepts: connected, conversation opened", status == 200 and bool(conversation), resp)
    check("accepting twice -> 409 not_pending", err(api("POST", f"/v1/connection-requests/{req.get('id')}/accept", token=asha.token)) == (409, "not_pending"))
    check("Ravi got an 'accepted' push", pushed(ravi, "accepted your request"))
    check("the accepted request takes a seat (journey-service)",
          bool(eventually(lambda: (data(api("GET", f"/v1/journeys/{a_j}", token=asha.token)) or {}).get("seats_left") == 2)))
    check("connection_status=connected on Ravi's profile and Asha's journey card",
          (data(api("GET", f"/v1/users/{ravi.id}", token=asha.token)) or {}).get("connection_status") == "connected"
          and (data(api("GET", f"/v1/journeys/{a_j}", token=ravi.token)) or {}).get("host", {}).get("connection_status") == "connected")  # fmt: skip
    check("asking again once connected -> 409 already_connected",
          err(api("POST", "/v1/connection-requests", {"target_journey_id": a_j, "message": "hi"}, ravi.token)) == (409, "already_connected"))
    # reject + cooldown, withdraw
    neha_req = data(api("POST", "/v1/connection-requests", {"target_journey_id": a_j, "message": "Can I come along?"}, neha.token)) or {}
    check("Asha declines Neha's request", api("POST", f"/v1/connection-requests/{neha_req.get('id')}/reject", token=asha.token)[0] == 200)
    neha_out = (data(api("GET", "/v1/connection-requests?box=outgoing", token=neha.token)) or {}).get("items", [])
    check("Neha sees 'not accepted' (no push for a rejection)", neha_out and neha_out[0]["status"] == "not_accepted", neha_out)
    status, headers, _ = api("POST", "/v1/connection-requests", {"target_journey_id": a_j, "message": "Please?"}, neha.token)
    check("asking again within 7 days -> 429 cooldown with Retry-After", status == 429 and int(headers.get("retry-after", 0)) > 6 * 86400)
    kabir_req = data(api("POST", "/v1/connection-requests", {"target_journey_id": a_j, "message": "Room for one more?"}, kabir.token)) or {}
    check("only the sender can withdraw -> 403", err(api("POST", f"/v1/connection-requests/{kabir_req.get('id')}/withdraw", token=asha.token)) == (403, "not_sender"))
    check("Kabir withdraws his request", api("POST", f"/v1/connection-requests/{kabir_req.get('id')}/withdraw", token=kabir.token)[0] == 200)
    check("Asha's incoming list is empty again", (data(api("GET", "/v1/connection-requests?box=incoming", token=asha.token)) or {}).get("items") == [])

    section("Chat over the WebSocket (chat-ws) and REST")
    ravi_ws = smoke.WebSocket(WS_BASE + ravi.token)
    asha_ws.send("message.send", {"conversation_id": conversation, "client_msg_id": f"{RUN}-a1", "body": "Welcome! We leave Noida at 6."}, "m1")
    ack = asha_ws.recv("ack", where=None)
    got = ravi_ws.recv("message.new", where=lambda d: d.get("body") == "Welcome! We leave Noida at 6.")
    check("Asha → Ravi in real time (socket ack + delivery)", ack["data"].get("message_id") == got["data"]["id"])
    ravi_ws.send("receipt", {"conversation_id": conversation, "read_up_to": ack["data"]["message_id"]}, "r1")
    check("Ravi's read receipt reaches Asha", asha_ws.recv("receipt", where=lambda d: d.get("user_id") == ravi.id)["data"].get("read_up_to") == ack["data"]["message_id"])
    ravi_ws.send("typing", {"conversation_id": conversation})
    check("typing indicator relayed", asha_ws.recv("typing")["data"] == {"conversation_id": conversation, "user_id": ravi.id})
    status, _, resp = api("POST", f"/v1/conversations/{conversation}/messages", {"client_msg_id": f"{RUN}-r1", "body": "Perfect, see you there"}, ravi.token)
    first_id = (resp or {}).get("data", {}).get("id")
    check("Ravi replies over REST (fallback)", status == 201, resp)
    status, _, resp = api("POST", f"/v1/conversations/{conversation}/messages", {"client_msg_id": f"{RUN}-r1", "body": "Perfect, see you there"}, ravi.token)
    check("resending the same client_msg_id is idempotent (200, same message)", status == 200 and resp["data"]["id"] == first_id, resp)
    check("Asha receives it live", asha_ws.recv("message.new", where=lambda d: d.get("id") == first_id)["data"]["sender_id"] == ravi.id)
    check("...and the reply marks her earlier message read (receipt)",
          bool(asha_ws.recv("receipt", where=lambda d: d.get("read_up_to") == first_id)))
    chats = (data(api("GET", "/v1/conversations", token=asha.token)) or {}).get("items", [])
    check("Asha's chat list: Ravi, last message, unread 1", chats and chats[0]["other_user"]["id"] == ravi.id
          and chats[0]["last_message"]["body"] == "Perfect, see you there" and chats[0]["unread_count"] == 1, chats)  # fmt: skip
    history = data(api("GET", f"/v1/conversations/{conversation}/messages?limit=2", token=asha.token)) or {}
    older = data(api("GET", f"/v1/conversations/{conversation}/messages?limit=2&before={history.get('next_cursor')}", token=asha.token)) or {}
    check("history newest first, paged with before=",
          [m["body"] for m in history.get("items", [])] == ["Perfect, see you there", "Welcome! We leave Noida at 6."]
          and [m["body"] for m in older.get("items", [])][:1] == ["Hi Asha! Same weekend. WhatsApp •••"], (history, older))  # fmt: skip
    api("POST", f"/v1/conversations/{conversation}/read", {"message_id": first_id}, asha.token)
    check("marking read clears the badge", (data(api("GET", "/v1/conversations/badge", token=asha.token)) or {}).get("unread_total") == 0)
    asha_ws.close()
    time.sleep(0.5)
    api("POST", f"/v1/conversations/{conversation}/messages", {"client_msg_id": f"{RUN}-r2", "body": "Bring a rain jacket"}, ravi.token)
    check("with Asha's socket closed, the message becomes a push", pushed(asha, "Bring a rain jacket"))
    check("...but not the ones she saw live", not any("see you there" in p["body"] for p in pushes(asha)))
    until = (datetime.now(UTC) + timedelta(hours=8)).isoformat()
    muted = data(api("PATCH", f"/v1/conversations/{conversation}", {"muted_until": until}, asha.token)) or {}
    check("mute for 8 hours", muted.get("muted") is True, muted)
    api("POST", f"/v1/conversations/{conversation}/messages", {"client_msg_id": f"{RUN}-r3", "body": "Muted ping"}, ravi.token)
    time.sleep(2)
    check("a muted chat doesn't push", not any("Muted ping" in p["body"] for p in pushes(asha)))
    api("PATCH", f"/v1/conversations/{conversation}", {"muted_until": None}, asha.token)
    check("delete chat (for me only)", api("DELETE", f"/v1/conversations/{conversation}", token=asha.token)[0] == 200)
    check("...gone from Asha's list, still in Ravi's",
          not [c for c in (data(api("GET", "/v1/conversations", token=asha.token)) or {}).get("items", []) if c["id"] == conversation]
          and [c for c in (data(api("GET", "/v1/conversations", token=ravi.token)) or {}).get("items", []) if c["id"] == conversation])  # fmt: skip
    ravi_ws.close()

    # ------------------------------------------------------------ safety
    section("Blocks and reports (social-service, enforced everywhere)")
    check("Asha blocks Ravi", data(api("POST", f"/v1/users/{ravi.id}/block", token=asha.token)) == {"blocked": True})
    blocked = (data(api("GET", "/v1/me/blocks", token=asha.token)) or {}).get("items", [])
    check("Asha's block list", [u["id"] for u in blocked] == [ravi.id], blocked)
    check("within seconds: Ravi can't see Asha's trip or profile",
          bool(eventually(lambda: api("GET", f"/v1/journeys/{a_j}", token=ravi.token)[0] == 404
                          and err(api("GET", f"/v1/users/{asha.id}", token=ravi.token)) == (404, "profile_unavailable"))))  # fmt: skip
    check("...their matches are purged", bool(eventually(lambda: r_j not in match_ids(asha, a_j))))
    check("...the chat is read-only (403 blocked)",
          err(api("POST", f"/v1/conversations/{conversation}/messages", {"client_msg_id": f"{RUN}-r4", "body": "hello?"}, ravi.token)) == (403, "blocked"))
    check("...and Ravi can't follow her", err(api("POST", f"/v1/users/{asha.id}/follow", token=ravi.token)) == (403, "blocked"))
    check("blocking yourself -> 400", err(api("POST", f"/v1/users/{asha.id}/block", token=asha.token)) == (400, "cannot_block_self"))
    check("unblock", data(api("DELETE", f"/v1/users/{ravi.id}/block", token=asha.token)) == {"blocked": False})
    check("visibility and matches come back", bool(eventually(lambda: api("GET", f"/v1/journeys/{a_j}", token=ravi.token)[0] == 200
                                                               and r_j in match_ids(asha, a_j))))  # fmt: skip
    report = {"target_type": "journey", "target_id": k_j, "reason": "spam", "note": "e2e"}
    check("Neha reports a journey", (data(api("POST", "/v1/reports", report, neha.token)) or {}).get("status") == "open")
    check("reporting twice -> 409 already_reported", err(api("POST", "/v1/reports", report, neha.token)) == (409, "already_reported"))
    check("reporting yourself -> 400", err(api("POST", "/v1/reports", {"target_type": "user", "target_id": neha.id, "reason": "other"}, neha.token)) == (400, "cannot_report_self"))

    # ------------------------------------------------------------ eKYC
    section("eKYC (user-service) and what it unlocks")
    me = data(api("GET", "/v1/me", token=kabir.token)) or {}
    check("Kabir: the eKYC sheet shows (variant 'none'); name and DOB editable",
          me.get("kyc_prompt") == {"show": True, "variant": "none"} and me.get("identity_locked") is False, me)
    snooze = data(api("POST", "/v1/me/kyc/prompt/snooze", token=kabir.token)) or {}
    check("'Skip for now' hides it for the 3-day cool-down",
          snooze.get("kyc_prompt", {}).get("show") is False and bool(snooze.get("snoozed_until"))
          and (data(api("GET", "/v1/me", token=kabir.token)) or {}).get("kyc_prompt", {}).get("show") is False, snooze)  # fmt: skip
    status = data(api("GET", "/v1/me/kyc", token=kabir.token)) or {}
    check("GET /v1/me/kyc: no check started yet",
          status.get("email", {}).get("status") == status.get("gov_id", {}).get("status") == "not_started", status)

    check("a personal email -> 400 email_domain_not_allowed",
          err(api("POST", "/v1/me/kyc/email/start", {"email": f"asha.{RUN}@gmail.com"}, asha.token)) == (400, "email_domain_not_allowed"))
    work = f"asha.rao-{RUN}@studio.example.com"
    code_status, _, resp = api("POST", "/v1/me/kyc/email/start", {"email": work}, asha.token)
    check("Asha asks for a code at her work email (202)", code_status == 202 and (data((0, {}, resp)) or {}).get("email") == work, resp)
    code = smoke.otp_for(work)
    wrong = "000000" if code != "000000" else "111111"
    resp = api("POST", "/v1/me/kyc/email/verify", {"otp": wrong}, asha.token)
    check("a wrong code -> 400 otp_invalid, with attempts left",
          err(resp) == (400, "otp_invalid") and resp[2]["error"].get("details") == {"attempts_left": 4}, resp[2])
    status = data(api("POST", "/v1/me/kyc/email/verify", {"otp": code}, asha.token)) or {}
    check("the emailed code verifies it: level partial, the sheet now asks for an ID",
          status.get("kyc_level") == "partial" and status.get("kyc_prompt") == {"show": True, "variant": "partial_need_id"}, status)

    check("an ID check needs the consent screen -> 422", api("POST", "/v1/me/kyc/gov-id/start", {}, asha.token)[0] == 422)
    started = data(api("POST", "/v1/me/kyc/gov-id/start", {"consent": True}, asha.token)) or {}
    page_url = started.get("redirect_url", "")
    check("Asha starts the ID check: a provider page to open", page_url.startswith(f"{GW}/v1/kyc/mock/"), started)
    check("...status is pending meanwhile",
          (data(api("GET", "/v1/me/kyc", token=asha.token)) or {}).get("gov_id", {}).get("status") == "pending")
    page_status, _, page = browse("GET", page_url)
    check("the provider page opens (mock provider, through the gateway)", page_status == 200 and 'value="verified"' in page, page[:200])
    sig = page_url.split("sig=", 1)[-1]
    done_status, headers, _ = browse("POST", page_url.split("?", 1)[0], f"sig={sig}&outcome=verified")
    check("finishing it sends the browser back to the app's deep link",
          done_status == 303 and headers.get("location") == f"cogo://kyc/return?attempt_id={started.get('attempt_id')}", headers)
    status = eventually(lambda: (s := data(api("GET", "/v1/me/kyc", token=asha.token)) or {})
                        and s["gov_id"]["status"] != "pending" and s, timeout=60) or {}  # fmt: skip
    check("the app polls /v1/me/kyc: verified, level full, prompt gone, identity locked",
          status.get("gov_id", {}).get("status") == "verified" and status.get("kyc_level") == "full"
          and status.get("kyc_prompt", {}).get("show") is False and status.get("identity_locked") is True
          and len(status.get("gov_id", {}).get("last4") or "") == 4, status)  # fmt: skip
    check("a verified name can't change -> 403 identity_locked",
          err(api("PATCH", "/v1/me", {"name": "Not Asha"}, asha.token)) == (403, "identity_locked"))
    check("a second ID check -> 409 already_verified",
          err(api("POST", "/v1/me/kyc/gov-id/start", {"consent": True}, asha.token)) == (409, "already_verified"))

    review = data(api("POST", "/v1/me/kyc/gov-id/start", {"consent": True}, neha.token)) or {}
    review_url = review.get("redirect_url", "")
    browse("POST", review_url.split("?", 1)[0], f"sig={review_url.split('sig=', 1)[-1]}&outcome=mismatch")
    status = data(api("GET", "/v1/me/kyc", token=neha.token)) or {}
    check("Neha's ID name doesn't match her profile: under review, level unchanged, no prompt, locked",
          status.get("gov_id", {}).get("status") == "review" and status.get("kyc_level") == "none"
          and status.get("kyc_prompt", {}).get("show") is False and status.get("identity_locked") is True, status)  # fmt: skip
    check("...and no new ID check until it's reviewed -> 409 kyc_review_pending",
          err(api("POST", "/v1/me/kyc/gov-id/start", {"consent": True}, neha.token)) == (409, "kyc_review_pending"))
    check("a forged provider callback -> 401 invalid_signature",
          err(api("POST", "/v1/webhooks/kyc/mock", {"ref": "mock_x", "status": "success"},
                  headers={"x-mock-signature": "0" * 64})) == (401, "invalid_signature"))  # fmt: skip

    api("POST", f"/v1/users/{asha.id}/follow", token=kabir.token)
    check("Asha's verified badge: on her profile", (data(api("GET", f"/v1/users/{asha.id}", token=kabir.token)) or {}).get("kyc_level") == "full")
    check("...on her journey card (journey-service, via user.kyc_level_changed)",
          bool(eventually(lambda: (data(api("GET", f"/v1/journeys/{a_j}", token=kabir.token)) or {}).get("host", {}).get("kyc_level") == "full")))
    check("...and in follower lists (social-service)",
          bool(eventually(lambda: any(u["id"] == asha.id and u.get("kyc_level") == "full"
                                      for u in (data(api("GET", f"/v1/users/{kabir.id}/following", token=kabir.token)) or {}).get("items", [])))))  # fmt: skip
    resp = api("POST", "/v1/journeys", journey_body(NOIDA, RISHIKESH, start + timedelta(hours=2), who_can_join="verified_only"), kabir.token)
    kabir.journeys["verified"] = kv_j = (data(resp) or {}).get("id", "")
    check("Kabir posts a verified-only trip", resp[0] == 201, resp[2])
    check("Asha (verified) matches it...", bool(eventually(lambda: kv_j in match_ids(asha, a_j))))
    check("...Ravi (unverified) doesn't", kv_j not in match_ids(ravi, r_j))
    check("...and can't ask to join it -> 403 not_eligible",
          err(api("POST", "/v1/connection-requests", {"target_journey_id": kv_j, "message": "hi"}, ravi.token)) == (403, "not_eligible"))

    # spec §13 M6: "a gate change in config takes effect without a deploy" (M7: through the admin API)
    check("the admin signs in to the console API (email + password + authenticator code)", ADMIN.login())
    original_kyc = (data(ADMIN.call("GET", "/admin/config/kyc")) or {}).get("value")
    gates = {"gates": {"create_journey": "partial", "send_connection_request": "full"}}
    check("admin tightens the KYC gates (PUT /admin/config/kyc)", ADMIN.set_config("kyc", gates))
    try:
        resp = api("POST", "/v1/journeys", journey_body(DELHI, HARIDWAR, start), ravi.token)
        check("Ravi (none) can't post a journey -> 403 kyc_required (required_level partial)",
              err(resp) == (403, "kyc_required") and resp[2]["error"].get("details") == {"required_level": "partial"}, resp[2])
        resp = api("POST", "/v1/connection-requests", {"target_journey_id": k_j, "message": "hi"}, ravi.token)
        check("...nor send a request -> 403 kyc_required (required_level full, chat-service)",
              err(resp) == (403, "kyc_required") and resp[2]["error"].get("details") == {"required_level": "full"}, resp[2])
        resp = api("POST", "/v1/journeys", journey_body(DELHI, HARIDWAR, start), asha.token)
        asha.journeys["gated"] = (data(resp) or {}).get("id", "")
        check("Asha (full) still can", resp[0] == 201, resp[2])
        resp = api("POST", "/v1/connection-requests", {"target_journey_id": k_j, "message": "Room for me?"}, asha.token)
        check("...and can ask to join Kabir's trip", resp[0] == 201, resp[2])
        api("POST", f"/v1/connection-requests/{(data(resp) or {}).get('id')}/withdraw", token=asha.token)
    finally:
        ADMIN.set_config("kyc", original_kyc)
    resp = api("POST", "/v1/journeys", journey_body(DELHI, HARIDWAR, start), ravi.token)
    ravi.journeys["after_gate"] = (data(resp) or {}).get("id", "")
    check("gates reopened (admin put them back): Ravi can post again", resp[0] == 201, resp[2])

    # ------------------------------------------------------------ sessions
    section("Sessions (auth-service)")
    status, _, resp = api("POST", "/v1/auth/token/refresh", {"refresh_token": kabir.refresh})
    check("refresh rotates the pair", status == 200 and resp["data"]["refresh_token"] != kabir.refresh, resp)
    check("replaying the old refresh token is caught -> 401 refresh_token_reused",
          err(api("POST", "/v1/auth/token/refresh", {"refresh_token": kabir.refresh})) == (401, "refresh_token_reused"))
    check("logout", api("POST", "/v1/auth/logout", token=neha.token)[0] == 200)
    status, _, resp = api("POST", "/v1/auth/logout-all", token=ravi.token)
    check("logout everywhere", status == 200 and resp["data"]["revoked_sessions"] >= 1, resp)
    check("a revoked refresh token can't be used", api("POST", "/v1/auth/token/refresh", {"refresh_token": ravi.refresh})[0] == 401)

    # ------------------------------------------------------------ admin + moderation (M7)
    section("Admin console and moderation (admin-service, spec §5.10)")
    public = api("GET", "/v1/config")
    visible = data(public) or {}
    check("GET /v1/config: client-visible config only, with an ETag",
          public[0] == 200 and {"kyc", "features", "journeys"} <= set(visible) and "matching" not in visible
          and bool(public[1].get("etag")), sorted(visible))  # fmt: skip
    check("...unchanged config -> 304 Not Modified", api("GET", "/v1/config", headers={"If-None-Match": public[1].get("etag", "")})[0] == 304)
    check("GET /admin/me: signed in as an admin", (data(ADMIN.call("GET", "/admin/me")) or {}).get("role") == "admin")
    keys = {e["key"] for e in (data(ADMIN.call("GET", "/admin/config")) or {}).get("items", [])}
    check("config list: every key the services read", {"kyc", "matching", "moderation", "features"} <= keys, sorted(keys))
    check("the console itself is served", browse("GET", f"{GW}/admin/console/")[0] == 200)

    report = admin_find("/admin/reports?status=open&target_type=journey&limit=50", lambda g: g["target_id"] == k_j)
    check("Neha's report on Kabir's trip is in the queue, with a preview",
          bool(report) and (report or {}).get("target", {}).get("title", "").startswith("Journey ·")
          and report["target"]["owner"]["id"] == kabir.id, report)  # fmt: skip
    warning = f"Please keep trip details accurate ({RUN})."
    resolved = ADMIN.call("POST", f"/admin/reports/{(report or {}).get('report_id')}/resolve",
                          {"action": "warn", "note": "first report", "warning": warning})  # fmt: skip
    check("resolve with a warning: reports actioned", resolved[0] == 200 and (data(resolved) or {}).get("status") == "actioned", resolved[2])
    check("...Kabir gets the warning as a push", pushed(kabir, warning))
    again = ADMIN.call("POST", f"/admin/reports/{(report or {}).get('report_id')}/resolve", {"action": "dismiss", "note": "x"})
    check("...resolving twice -> 409 already_resolved", err(again) == (409, "already_resolved"))

    bad = upload(ravi, "avatar", b"cogo-test:flag" + b"0" * 128)
    set_bad = api("PATCH", "/v1/me", {"avatar_url": bad}, ravi.token)
    check("Ravi sets an avatar that SafeSearch (mock) flags...", set_bad[0] == 200, set_bad[2])
    check("...it's taken down within seconds",
          bool(eventually(lambda: (data(api("GET", "/v1/me", token=ravi.token)) or {}).get("avatar_url") is None, timeout=15)))  # fmt: skip
    media = eventually(lambda: admin_find("/admin/reports?status=open&target_type=media&limit=50",
                                          lambda g: g["target_owner_id"] == ravi.id))  # fmt: skip
    check("...and a system report opens for admins (no reporter)",
          bool(media) and media["reports"][0]["reporter_id"] is None and "adult" in (media["reports"][0]["note"] or ""), media)  # fmt: skip
    removed = ADMIN.call("POST", f"/admin/reports/{(media or {}).get('report_id')}/resolve", {"action": "remove", "note": "nudity"})
    check("admin confirms the takedown (remove)", removed[0] == 200 and (data(removed) or {}).get("hidden") is True, removed[2])

    review = admin_find("/admin/kyc-reviews?limit=50", lambda r: r["user_id"] == neha.id)
    check("Neha's ID mismatch waits in the KYC review queue",
          bool(review) and review["result"]["name_match"] is False and review["result"]["dob_match"] is True, review)  # fmt: skip
    approved = ADMIN.call("POST", f"/admin/kyc-reviews/{neha.id}/approve", {"note": "name order differs"})
    check("approve it: Neha is verified (full)", (data(approved) or {}).get("kyc_level") == "full", approved[2])
    check("...the app shows it", (data(api("GET", "/v1/me/kyc", token=neha.token)) or {}).get("gov_id", {}).get("status") == "verified")
    check("...and so does her badge elsewhere (journey-service, via the event)",
          bool(eventually(lambda: (data(api("GET", f"/v1/journeys/{n_j}", token=asha.token)) or {}).get("host", {}).get("kyc_level") == "full")))  # fmt: skip

    hits = (data(ADMIN.call("GET", f"/admin/users?q=ravi_{RUN}")) or {}).get("items", [])
    check("search users by username", [h["id"] for h in hits] == [ravi.id], hits)
    detail = data(ADMIN.call("GET", f"/admin/users/{ravi.id}")) or {}
    check("user detail: profile, status, KYC, open reports",
          detail.get("username") == f"ravi_{RUN}" and detail.get("account_status") == "active" and "open_reports" in detail, detail)  # fmt: skip
    until = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    suspended = ADMIN.call("POST", f"/admin/users/{ravi.id}/suspend", {"until": until, "reason": "e2e"})
    check("suspend Ravi for 2 days", (data(suspended) or {}).get("account_status") == "suspended", suspended[2])
    check("...his trip and profile disappear for others (user.status_changed)",
          bool(eventually(lambda: api("GET", f"/v1/journeys/{r_j}", token=asha.token)[0] == 404
                          and api("GET", f"/v1/users/{ravi.id}", token=asha.token)[0] == 404)))  # fmt: skip
    check("...and he can't change anything", err(api("PATCH", "/v1/me", {"bio": "x"}, ravi.token)) == (403, "account_inactive"))
    banned = ADMIN.call("POST", f"/admin/users/{ravi.id}/ban", {"reason": "e2e"})
    check("ban (admins only)", (data(banned) or {}).get("account_status") == "banned", banned[2])
    back = ADMIN.call("POST", f"/admin/users/{ravi.id}/reinstate", {"reason": "appeal upheld"})
    check("reinstate", (data(back) or {}).get("account_status") == "active", back[2])
    check("...his trip is back", bool(eventually(lambda: api("GET", f"/v1/journeys/{r_j}", token=asha.token)[0] == 200)))
    hello = f"Hello from the CoGo team ({RUN})"
    warned = ADMIN.call("POST", f"/admin/users/{asha.id}/warn", {"message": hello, "reason": "e2e"})
    check("warn a user directly: a push they can't switch off", (data(warned) or {}).get("sent") is True and pushed(asha, hello), warned[2])

    viewed = data(ADMIN.call("GET", f"/admin/journeys/{kv_j}")) or {}
    check("admin journey view", viewed.get("owner", {}).get("id") == kabir.id and viewed.get("deleted") is False, viewed)
    check("admin removes Kabir's verified-only trip", data(ADMIN.call("DELETE", f"/admin/journeys/{kv_j}?reason=e2e")) == {"deleted": True})
    check("...gone for everyone and from Asha's matches",
          bool(eventually(lambda: api("GET", f"/v1/journeys/{kv_j}", token=asha.token)[0] == 404 and kv_j not in match_ids(asha, a_j))))  # fmt: skip

    audit = (data(ADMIN.call("GET", "/admin/audit-log?limit=100")) or {}).get("items", [])
    mine = {e["action"] for e in audit if e["admin"]["email"] == ADMIN.email}
    needed = {"admin.login", "config.update", "report.resolve", "kyc.approve", "user.suspend", "user.ban", "user.reinstate", "user.warn", "journey.delete"}
    check("every admin action is in the audit log", needed <= mine, sorted(needed - mine))
    about_ravi = (data(ADMIN.call("GET", f"/admin/audit-log?target_id={ravi.id}")) or {}).get("items", [])
    check("...filterable by target", about_ravi and all(e["target_id"] == ravi.id for e in about_ravi), len(about_ravi))

    # ------------------------------------------------------------ account deletion (M7, spec §7)
    section("Deleting an account (spec §7, E2E flow 4)")
    followers_before = (data(api("GET", "/v1/me", token=asha.token)) or {}).get("stats", {}).get("followers", 0)
    check("deleting needs a fresh code -> 403 reauth_required", err(api("DELETE", "/v1/me", token=kabir.token)) == (403, "reauth_required"))
    previous = smoke.otp_for(kabir.email)
    sent = api("POST", "/v1/auth/reauth/email/send", token=kabir.token)
    check("Kabir asks for a confirmation code (sent to his account email)",
          sent[0] == 202 and (data(sent) or {}).get("email_hint", "").endswith("@example.com"), sent[2])  # fmt: skip
    code = new_code(kabir.email, previous)
    proof = data(api("POST", "/v1/auth/reauth/email/verify", {"code": code, "purpose": "delete_account"}, kabir.token)) or {}
    check("...the code buys a 5-minute deletion token", bool(proof.get("reauth_token")) and proof.get("expires_in") == 300, proof)
    gone = api("DELETE", "/v1/me", token=kabir.token, headers={"X-Reauth-Token": proof.get("reauth_token", "")})
    check("DELETE /v1/me -> 202 deleting", gone[0] == 202 and (data(gone) or {}).get("status") == "deleting", gone[2])
    check("his profile is gone (to him and to others)",
          api("GET", "/v1/me", token=kabir.token)[0] == 404 and api("GET", f"/v1/users/{kabir.id}", token=asha.token)[0] == 404)  # fmt: skip
    check("his trips are cancelled for everyone (journey-service)",
          bool(eventually(lambda: api("GET", f"/v1/journeys/{k_j}", token=asha.token)[0] == 404)))  # fmt: skip
    check("Asha's follower count drops (social-service removed his follow)",
          bool(eventually(lambda: (data(api("GET", "/v1/me", token=asha.token)) or {}).get("stats", {}).get("followers") == followers_before - 1)))  # fmt: skip
    check("every service reports it's done (user-service's deletion tracker)",
          bool(eventually(lambda: (data(ADMIN.call("GET", f"/admin/users/{kabir.id}")) or {}).get("deletion_completed_at"), timeout=60, every=2)))  # fmt: skip
    resent = send_code(kabir.email)
    if err(resent) == (429, "otp_cooldown"):  # like the app: wait as Retry-After says
        time.sleep(int(resent[1].get("retry-after", "30")) + 1)
        resent = send_code(kabir.email)
    check("...signing up again with the same email: a code is sent", resent[0] == 202, resent[2])
    fresh = api("POST", "/v1/auth/email/verify", {"email": kabir.email, "code": new_code(kabir.email, code)})
    out = data(fresh) or {}
    check("his email address can start over: a new, empty account",
          fresh[0] == 200 and out.get("is_new_user") is True and out.get("user_id") != kabir.id, fresh[2])  # fmt: skip
    if out.get("access_token"):  # the new account is this run's too
        PEOPLE.append(User("Kabir2", "male", id=out["user_id"], token=out["access_token"]))
    check("admin signs out", ADMIN.call("POST", "/admin/auth/logout")[0] == 200
          and err(ADMIN.call("GET", "/admin/me")) == (401, "invalid_session"))  # fmt: skip

    # ------------------------------------------------------------ lifecycle + cleanup
    section("Deleting journeys (matches purged, requests expire)")
    check("Asha deletes her trip", api("DELETE", f"/v1/journeys/{a_j}", token=asha.token)[0] == 200)
    check("...it's gone for everyone", api("GET", f"/v1/journeys/{a_j}", token=kabir.token)[0] == 404)
    check("...and from other trips' matches", bool(eventually(lambda: a_j not in match_ids(neha, n_j))))
    for user in (ravi, neha, kabir):  # Ravi's and Neha's tokens were revoked by logout: access tokens live 15 min
        for jid in user.journeys.values():
            api("DELETE", f"/v1/journeys/{jid}", token=user.token)
    check("dead-letter queues are empty", all(smoke.queue_depth(q) == 0 for q in (
        "auth-service.dlq", "user-service.dlq", "social-service.dlq", "journey-service.dlq", "chat-service.dlq",
        "notification-service.dlq", "notification.email.dlq")))  # fmt: skip

    # ------------------------------------------------------------ coverage
    print("\nAPI coverage (public endpoints in contracts/openapi)")
    missing = []
    for svc in sorted({o[0] for o in OPS}):
        ops = [(m, p) for s, m, p, _ in OPS if s == svc]
        hit = [op for op in ops if HITS.get((svc, f"{op[0]} {op[1]}"))]
        print(f"  {svc:22} {len(hit)}/{len(ops)}")
        missing += [f"{svc}: {m} {p}" for m, p in ops if (m, p) not in hit]
    for m in missing:
        print(f"  NOT EXERCISED  {m}")
    failed = [(s, n) for s, n, ok in RESULTS if not ok]
    total_ops = len(OPS)
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed; {total_ops - len(missing)}/{total_ops} endpoints exercised")
    return 1 if failed or missing else 0


if __name__ == "__main__":
    sys.exit(main())
