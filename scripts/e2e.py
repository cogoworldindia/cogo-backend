#!/usr/bin/env python3
"""Full end-to-end API test through the public gateway (`make up && make e2e`).

Four people use the app the way the product intends: sign up with an email code, set up their
profiles (photos, tags, city), post journeys, get matched, follow, like, comment, ask to join
a trip, chat over the WebSocket, block, report, and receive push notifications (mock FCM).

Every request is matched against the OpenAPI contracts (contracts/openapi/*.json). The run fails
if any check fails OR any public endpoint was never exercised, and prints the coverage.
Stdlib only; reuses the smoke test's helpers (HTTP, WebSocket, Mailpit).
"""

import json
import re
import sys
import time
import uuid
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


def api(method: str, path: str, body: Any = None, token: str | None = None,
        headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:  # fmt: skip
    bare = path.split("?", 1)[0]
    for svc, m, template, pattern in OPS:
        if m == method and pattern.match(bare):
            HITS[(svc, f"{m} {template}")] = HITS.get((svc, f"{m} {template}"), 0) + 1
            break
    return smoke.call(method, GW + path, body, token, headers)


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


def upload(user: User, kind: str) -> str | None:
    """Presigned PUT straight to object storage, then confirm (USER-06/07)."""
    up = data(api("POST", "/v1/media/upload-url", {"kind": kind, "content_type": "image/png"}, user.token)) or {}
    if not up.get("upload_url"):
        return None
    req = urllib.request.Request(up["upload_url"], data=b"\x89PNG\r\n\x1a\n" + b"0" * 512, method="PUT",
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
    feed = (data(api("GET", f"/v1/feed?to_place_id={RISHIKESH}", token=ravi.token)) or {}).get("items", [])
    feed_ids = [i["journey"]["id"] for i in feed if i["type"] == "journey"]
    check("Ravi's feed has Asha's trip, not his own, not the women-only one", a_j in feed_ids and r_j not in feed_ids and nw_j not in feed_ids, feed_ids)
    check("Asha (female) does see the women-only trip",
          nw_j in [i["journey"]["id"] for i in (data(api("GET", f"/v1/feed?to_place_id={RISHIKESH}", token=asha.token)) or {}).get("items", [])])
    day = start.astimezone(IST).date()
    on_day = [i["journey"]["id"] for i in (data(api("GET", f"/v1/feed?to_place_id={RISHIKESH}&date_from={day}&date_to={day}", token=ravi.token)) or {}).get("items", [])]
    later = (data(api("GET", f"/v1/feed?to_place_id={RISHIKESH}&date_from={day + timedelta(days=20)}", token=ravi.token)) or {}).get("items", [])
    check("date filter (IST dates) includes the day and excludes later dates", a_j in on_day and not [i for i in later if i["journey"]["id"] == a_j])
    check("people filter: 3 free seats needed", a_j in [i["journey"]["id"] for i in (data(api("GET", f"/v1/feed?to_place_id={RISHIKESH}&people=3", token=ravi.token)) or {}).get("items", [])])
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
    check("...it leaves his feed", a_j not in [i["journey"]["id"] for i in (data(api("GET", f"/v1/feed?to_place_id={RISHIKESH}", token=kabir.token)) or {}).get("items", [])])
    check("...and his matches", a_j not in match_ids(kabir, k_j))
    api("DELETE", f"/v1/journeys/{a_j}/hide", token=kabir.token)
    check("unhide brings it back", a_j in [i["journey"]["id"] for i in (data(api("GET", f"/v1/feed?to_place_id={RISHIKESH}", token=kabir.token)) or {}).get("items", [])])
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

    # ------------------------------------------------------------ lifecycle + cleanup
    section("Deleting journeys (matches purged, requests expire)")
    check("Asha deletes her trip", api("DELETE", f"/v1/journeys/{a_j}", token=asha.token)[0] == 200)
    check("...it's gone for everyone", api("GET", f"/v1/journeys/{a_j}", token=kabir.token)[0] == 404)
    check("...and from other trips' matches", bool(eventually(lambda: a_j not in match_ids(kabir, k_j))))
    for user in (ravi, neha, kabir):  # Ravi's and Neha's tokens were revoked by logout: access tokens live 15 min
        for jid in user.journeys.values():
            api("DELETE", f"/v1/journeys/{jid}", token=user.token)
    check("dead-letter queues are empty", all(smoke.queue_depth(q) == 0 for q in (
        "user-service.dlq", "social-service.dlq", "journey-service.dlq", "chat-service.dlq",
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
