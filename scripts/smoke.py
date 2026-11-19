#!/usr/bin/env python3
"""End-to-end smoke test against a running stack (`make up && make smoke`).

Stdlib only. Talks to the public gateway exactly like a client would, reads OTP codes from
Mailpit, and checks the RabbitMQ dead-letter queues stay empty.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

def _load_env_files(*names: str) -> None:
    """Read KEY=VALUE files the way docker compose does (no shell sourcing)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in names:
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


_load_env_files(".env", ".env.secrets")
GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8000")
MAILPIT = os.environ.get("MAILPIT_URL", "http://localhost:8025")
RABBIT = os.environ.get("RABBITMQ_API_URL", "http://localhost:15672")
_results: list[tuple[str, bool]] = []


def call(method: str, url: str, body: Any = None, token: str | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, {k.lower(): v for k, v in r.headers.items()}, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode(errors="replace")
        return e.code, {k.lower(): v for k, v in e.headers.items()}, payload


def check(name: str, ok: bool, detail: Any = "") -> None:
    _results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -> {detail}"))


def otp_for(email: str, timeout: float = 20.0) -> str:
    query = urllib.parse.quote(f"to:{email}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, _, found = call("GET", f"{MAILPIT}/api/v1/search?query={query}")
        if found and found.get("messages"):
            _, _, msg = call("GET", f"{MAILPIT}/api/v1/message/{found['messages'][0]['ID']}")
            match = re.search(r"\b(\d{6})\b", msg["Text"])
            if match:
                return match.group(1)
        time.sleep(0.5)
    raise SystemExit(f"no OTP email for {email} within {timeout}s (is notification-service up?)")


def sign_in(email: str) -> dict[str, Any]:
    status, _, _ = call("POST", f"{GATEWAY}/v1/auth/email/send", {"email": email})
    assert status == 202, status
    status, _, body = call("POST", f"{GATEWAY}/v1/auth/email/verify", {"email": email, "code": otp_for(email)})
    assert status == 200, body
    return body["data"]


def queue_depth(name: str) -> int:
    auth = base64.b64encode(f"{os.environ.get('RABBITMQ_USER', 'cogo')}:{os.environ['RABBITMQ_PASSWORD']}".encode()).decode()
    _, _, q = call("GET", f"{RABBIT}/api/queues/%2F/{urllib.parse.quote(name)}", headers={"Authorization": f"Basic {auth}"})
    if not isinstance(q, dict) or "messages" not in q:
        return -1  # queue missing: a typo or a lost declaration must not read as "empty"
    return int(q["messages"])


def main() -> int:
    run = uuid.uuid4().hex[:8]
    alice, bob, carol = (f"{n}-{run}@example.com" for n in ("alice", "bob", "carol"))

    print("health")
    status, _, body = call("GET", f"{GATEWAY}/health/ready")
    check("gateway ready", status == 200, body)

    print("email sign-in")
    a = sign_in(alice)
    check("alice gets a token pair and is new", a["is_new_user"] is True and a["access_token"] and a["refresh_token"])
    status, headers, body = call("POST", f"{GATEWAY}/v1/auth/email/send", {"email": alice})
    check("resend within 60s is rate limited", status == 429 and "retry-after" in headers, (status, body))

    print("profile")
    status, _, body = call("GET", f"{GATEWAY}/v1/users/me", token=a["access_token"])
    check("GET /v1/users/me", status == 200 and body["data"]["id"] == a["user_id"], body)
    status, _, body = call("PATCH", f"{GATEWAY}/v1/users/me/profile",
                           {"full_name": "Alice Traveller", "date_of_birth": "1995-05-05", "bio": "Delhi to Goa by train"},
                           token=a["access_token"])
    check("PATCH /v1/users/me/profile", status == 200 and body["data"]["profile"]["full_name"] == "Alice Traveller", body)

    print("access control")
    b = sign_in(bob)
    status, _, body = call("GET", f"{GATEWAY}/v1/users/{a['user_id']}", token=b["access_token"])
    private = {"dob", "date_of_birth", "gender", "email", "phone", "kyc_prompt", "account_status"}
    check("bob sees only alice's public profile (no DOB, gender or KYC detail)",
          status == 200 and body["data"]["id"] == a["user_id"] and not private & set(body["data"]), body)
    for method, path in (("PUT", f"/v1/users/user/{a['user_id']}"), ("PATCH", f"/v1/users/{a['user_id']}/profile")):
        status, _, _ = call(method, f"{GATEWAY}{path}", {"status": "deleted"}, token=b["access_token"])
        check(f"{method} {path.replace(a['user_id'], '<alice>')} impossible", status in (404, 405), status)
    for method, path in (("POST", "/v1/api/emails"), ("POST", "/v1/emails"), ("GET", "/.well-known/jwks.json"), ("GET", "/v1/auth/validate")):
        status, _, _ = call(method, f"{GATEWAY}{path}", {"to": "x@y.z"} if method == "POST" else None)
        check(f"{method} {path} not exposed", status == 404, status)
    status, _, _ = call("GET", f"{GATEWAY}/v1/users/me", headers={"Authorization": a["access_token"]})
    check("token without 'Bearer' prefix rejected", status == 401, status)

    print("profiles (M1)")
    status, _, body = call("GET", f"{GATEWAY}/v1/personality-tags", token=a["access_token"])
    tags = body["data"] if status == 200 else []
    check("personality tags", len(tags) >= 10, status)
    status, _, body = call("GET", f"{GATEWAY}/v1/places/autocomplete?q=noi", token=a["access_token"])
    noida = next((p for p in (body or {}).get("data", []) if p["primary_text"] == "Noida"), None)
    check("places autocomplete (journey-service)", status == 200 and noida is not None, body)
    status, _, body = call("PATCH", f"{GATEWAY}/v1/me", {
        "name": "Alice Traveller", "dob": "1995-05-05", "gender": "female", "profession": "Designer",
        "city_place_id": noida["place_id"] if noida else "x", "personality_tag_ids": [t["id"] for t in tags[:3]]},
        token=a["access_token"])
    me = (body or {}).get("data") or {}
    check("PATCH /v1/me resolves city via journey-service and assigns a username",
          status == 200 and (me.get("city") or {}).get("name") == "Noida" and bool(me.get("username")), body)
    status, _, body = call("POST", f"{GATEWAY}/v1/media/upload-url", {"kind": "avatar", "content_type": "image/png"},
                           token=a["access_token"])
    up = (body or {}).get("data") or {}
    ok_put = False
    if status == 200:
        req = urllib.request.Request(up["upload_url"], data=b"\x89PNG\r\n" + b"0" * 256, method="PUT",
                                     headers={"Content-Type": "image/png"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ok_put = r.status == 200
    status, _, body = call("POST", f"{GATEWAY}/v1/media/{up.get('media_id')}/confirm", token=a["access_token"])
    status2, _, body2 = call("PATCH", f"{GATEWAY}/v1/me", {"avatar_url": up.get("file_url")}, token=a["access_token"])
    try:
        with urllib.request.urlopen(up.get("file_url", "http://invalid"), timeout=5) as r:
            served = r.status == 200
    except Exception:
        served = False
    check("avatar: presigned upload -> confirm -> set -> publicly served",
          ok_put and status == 200 and status2 == 200 and served, (body, body2))

    call("GET", f"{GATEWAY}/v1/me", token=b["access_token"])
    status, _, body = call("POST", f"{GATEWAY}/v1/users/{a['user_id']}/follow", token=b["access_token"])
    check("bob follows alice (social-service)", status == 200, body)
    followers_seen = False
    for _ in range(40):  # counters are projected from social.followed events (eventual, < 2 s)
        _, _, body = call("GET", f"{GATEWAY}/v1/me", token=a["access_token"])
        if ((body or {}).get("data") or {}).get("stats", {}).get("followers") == 1:
            followers_seen = True
            break
        time.sleep(0.25)
    check("alice's follower count updated via events", followers_seen, body)
    status, _, body = call("GET", f"{GATEWAY}/v1/users/{a['user_id']}", token=b["access_token"])
    check("alice's public profile shows is_following=true for bob",
          status == 200 and body["data"]["is_following"] is True and "dob" not in body["data"], body)
    listed = False
    for _ in range(40):  # follower names come from social-service's user projection (eventual, < 2 s)
        status, _, body = call("GET", f"{GATEWAY}/v1/users/{a['user_id']}/followers", token=b["access_token"])
        if status == 200 and [u["id"] for u in body["data"]["items"]] == [b["user_id"]]:
            listed = True
            break
        time.sleep(0.25)
    check("followers list", listed, body)
    status, _, body = call("GET", f"{GATEWAY}/v1/users/me", token=a["access_token"])
    check("legacy GET /v1/users/me still works for the current app",
          status == 200 and body["data"]["profile"]["full_name"] == "Alice Traveller", body)

    print("OTP brute force")
    call("POST", f"{GATEWAY}/v1/auth/email/send", {"email": carol})
    real = otp_for(carol)
    wrong = f"{(int(real) + 1) % 1_000_000:06d}"
    codes = [call("POST", f"{GATEWAY}/v1/auth/email/verify", {"email": carol, "code": wrong})[0] for _ in range(5)]
    check("5th wrong code locks the code", codes == [400, 400, 400, 400, 429], codes)
    status, _, _ = call("POST", f"{GATEWAY}/v1/auth/email/verify", {"email": carol, "code": real})
    check("locked code can't be used afterwards", status == 400, status)

    print("sessions")
    status, _, body = call("POST", f"{GATEWAY}/v1/auth/token/refresh", {"refresh_token": a["refresh_token"]})
    check("refresh rotates", status == 200 and body["data"]["refresh_token"] != a["refresh_token"], body)
    rotated = body["data"]
    status, _, body = call("POST", f"{GATEWAY}/v1/auth/token/refresh", {"refresh_token": a["refresh_token"]})
    check("reusing an old refresh token is detected", status == 401 and body["error"]["type"] == "refresh_token_reused", body)
    status, _, _ = call("POST", f"{GATEWAY}/v1/auth/token/refresh", {"refresh_token": rotated["refresh_token"]})
    check("...and kills the whole session", status == 401, status)
    status, _, body = call("POST", f"{GATEWAY}/v1/auth/logout-all", token=b["access_token"])
    check("logout-all", status == 200 and body["data"]["revoked_sessions"] >= 1, body)

    print("events")
    time.sleep(1)
    for q in ("user-service.dlq", "social-service.dlq", "journey-service.dlq", "notification.email.dlq"):
        depth = queue_depth(q)
        check(f"{q} is empty", depth == 0, depth)

    failed = [name for name, ok in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
