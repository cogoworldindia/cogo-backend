#!/usr/bin/env python3
"""End-to-end smoke test against a running stack (`make up && make smoke`).

Stdlib only. Talks to the public gateway exactly like a client would, reads OTP codes from
Mailpit, and checks the RabbitMQ dead-letter queues stay empty.
"""

import base64
import json
import os
import re
import socket
import struct
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


class WebSocket:
    """Minimal RFC 6455 client (text frames, ping/pong, close) so the smoke test stays stdlib."""

    class Closed(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(code)
            self.code = code

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        parts = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.sock.sendall(
            (f"GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake closed")
            head += chunk
        status, _, self._buf = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n")[0]:
            raise ConnectionError(status.split(b"\r\n")[0].decode())

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocket.Closed(1006)
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x80 | opcode]) + (
            bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
            if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n)
        )
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, type_: str, data: dict[str, Any] | None = None, id_: str | None = None) -> None:
        frame: dict[str, Any] = {"type": type_, "data": data or {}}
        if id_:
            frame["id"] = id_
        self._frame(0x1, json.dumps(frame).encode())

    def recv(self, type_: str, timeout: float = 8.0, where: Any = None) -> dict[str, Any]:
        """The next frame of this type (others are skipped)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                b1, b2 = self._read(2)
            except TimeoutError:
                break
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload, opcode = self._read(n), b1 & 0x0F
            if opcode == 0x8:
                raise WebSocket.Closed(struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005)
            if opcode == 0x9:
                self._frame(0xA, payload)
            elif opcode == 0x1:
                frame = json.loads(payload)
                if frame.get("type") == type_ and (where is None or where(frame.get("data") or {})):
                    return dict(frame)
        raise TimeoutError(f"no {type_} frame within {timeout}s")

    def close(self) -> None:
        try:
            self._frame(0x8, struct.pack(">H", 1000))
        finally:
            self.sock.close()


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
    for _ in range(4):  # like the app: honour the gateway's per-IP limit (5/min) via Retry-After
        status, headers, body = call("POST", f"{GATEWAY}/v1/auth/email/send", {"email": email})
        if not (status == 429 and ((body or {}).get("error") or {}).get("type") == "rate_limited"):
            break
        time.sleep(int(headers.get("retry-after", "10")) + 1)
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

    print("journeys (M2)")
    start = time.time() + 3 * 86400
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    journey_body = {"origin_place_id": "mock:noida-uttar-pradesh", "destination_place_id": "mock:rishikesh-uttarakhand",
                    "start_at": iso(start), "end_at": iso(start + 30 * 3600), "who_can_join": "anyone", "group_size": 4,
                    "caption": "Weekend in the hills"}
    status, _, body = call("POST", f"{GATEWAY}/v1/journeys", journey_body, token=a["access_token"])
    journey = (body or {}).get("data") or {}
    check("alice posts a journey (journey-service)",
          status == 201 and journey.get("seats_left") == 3 and journey.get("matches", {}).get("state") == "pending", body)
    status, _, body = call("POST", f"{GATEWAY}/v1/journeys", {**journey_body, "start_at": iso(time.time() + 60)}, token=a["access_token"])
    check("journey starting in < 15 min is rejected", status == 400 and body["error"]["type"] == "invalid_start", body)
    status, _, body = call("GET", f"{GATEWAY}/v1/journeys/{journey.get('id')}", token=b["access_token"])
    view = (body or {}).get("data") or {}
    check("bob sees alice's journey with host + vibe match, no coordinates",
          status == 200 and view.get("host", {}).get("name") == "Alice Traveller" and isinstance(view.get("vibe_match"), int)
          and "lat" not in json.dumps(view), body)
    status, _, body = call("GET", f"{GATEWAY}/v1/users/{a['user_id']}/journeys", token=b["access_token"])
    check("alice's public journeys list", status == 200 and [j["id"] for j in body["data"]["items"]] == [journey.get("id")], body)
    status, _, body = call("GET", f"{GATEWAY}/v1/me/journeys?tab=upcoming", token=a["access_token"])
    check("Your Journeys (upcoming)", status == 200 and body["data"]["items"][0]["id"] == journey.get("id"), body)
    counted = False
    for _ in range(40):  # journeys_count is projected into user-service from journey.created
        _, _, body = call("GET", f"{GATEWAY}/v1/me", token=a["access_token"])
        if ((body or {}).get("data") or {}).get("stats", {}).get("journeys") == 1:
            counted = True
            break
        time.sleep(0.25)
    check("profile journey count updated via events", counted, body)
    status, _, body = call("PATCH", f"{GATEWAY}/v1/journeys/{journey.get('id')}", {"caption": "Rishikesh rafting!"}, token=b["access_token"])
    check("only the host can edit", status == 403 and body["error"]["type"] == "not_owner", body)
    status, _, body = call("PATCH", f"{GATEWAY}/v1/journeys/{journey.get('id')}", {"caption": "Rishikesh rafting!"}, token=a["access_token"])
    check("host edits caption", status == 200 and body["data"]["caption"] == "Rishikesh rafting!", body)
    status, _, _ = call("DELETE", f"{GATEWAY}/v1/journeys/{journey.get('id')}", token=a["access_token"])
    status2, _, _ = call("GET", f"{GATEWAY}/v1/journeys/{journey.get('id')}", token=b["access_token"])
    check("host deletes; journey disappears", status == 200 and status2 == 404, (status, status2))

    print("feed, engagement, safety (M3)")
    hampi = "mock:hampi-karnataka"
    status, _, body = call("POST", f"{GATEWAY}/v1/journeys", {**journey_body, "destination_place_id": hampi}, token=b["access_token"])
    bob_journey = ((body or {}).get("data") or {}).get("id")
    check("bob posts a journey to Hampi", status == 201, body)

    def alice_feed_has_bob() -> bool:
        _, _, fb = call("GET", f"{GATEWAY}/v1/feed?to_place_id={hampi}", token=a["access_token"])
        return bob_journey in [i["journey"]["id"] for i in ((fb or {}).get("data") or {}).get("items", [])]

    check("bob's journey is in alice's feed (filtered by destination)", alice_feed_has_bob())
    status, _, body = call("POST", f"{GATEWAY}/v1/journeys/{bob_journey}/like", token=a["access_token"])
    check("alice likes it", status == 200 and body["data"] == {"liked": True, "likes": 1}, body)
    status, _, body = call("POST", f"{GATEWAY}/v1/journeys/{bob_journey}/comments", {"body": "Count me in!"}, token=a["access_token"])
    status2, _, body2 = call("GET", f"{GATEWAY}/v1/journeys/{bob_journey}/comments", token=b["access_token"])
    check("alice comments; bob sees it", status == 201 and [c["body"] for c in body2["data"]["items"]] == ["Count me in!"], (body, body2))
    status, _, body = call("POST", f"{GATEWAY}/v1/journeys/{bob_journey}/share", {"channel": "whatsapp"}, token=a["access_token"])
    share_url = ((body or {}).get("data") or {}).get("url", "")
    try:
        with urllib.request.urlopen(f"{GATEWAY}/j/{bob_journey}", timeout=5) as r:
            page = r.read().decode()
            public_ok = r.status == 200 and "Hampi" in page and "og:title" in page
    except Exception:
        public_ok = False
    check("share link + public preview page (no token)", share_url.endswith(f"/j/{bob_journey}") and public_ok, body)

    status, _, body = call("POST", f"{GATEWAY}/v1/users/{b['user_id']}/block", token=a["access_token"])
    check("alice blocks bob", status == 200 and body["data"] == {"blocked": True}, body)
    hidden = profile_gone = False
    for _ in range(40):  # block reaches journey- and user-service projections via events (< 2 s)
        s1, _, _ = call("GET", f"{GATEWAY}/v1/journeys/{bob_journey}", token=a["access_token"])
        s2, _, bp = call("GET", f"{GATEWAY}/v1/users/{b['user_id']}", token=a["access_token"])
        hidden, profile_gone = s1 == 404 and not alice_feed_has_bob(), s2 == 404 and bp["error"]["type"] == "profile_unavailable"
        if hidden and profile_gone:
            break
        time.sleep(0.25)
    check("after the block: bob's journey and profile are gone for alice", hidden and profile_gone)
    status, _, body = call("POST", f"{GATEWAY}/v1/users/{a['user_id']}/follow", token=b["access_token"])
    check("bob can't follow alice while blocked", status == 403 and body["error"]["type"] == "blocked", body)
    call("DELETE", f"{GATEWAY}/v1/users/{b['user_id']}/block", token=a["access_token"])
    restored = False
    for _ in range(40):
        if call("GET", f"{GATEWAY}/v1/journeys/{bob_journey}", token=a["access_token"])[0] == 200:
            restored = True
            break
        time.sleep(0.25)
    check("unblock restores visibility", restored)
    report = {"target_type": "journey", "target_id": bob_journey, "reason": "spam"}
    s1, _, _ = call("POST", f"{GATEWAY}/v1/reports", report, token=a["access_token"])
    s2, _, b2 = call("POST", f"{GATEWAY}/v1/reports", report, token=a["access_token"])
    check("report once; duplicate rejected", s1 == 201 and s2 == 409 and b2["error"]["type"] == "already_reported", (s1, s2))

    print("matching (M4)")
    trip = {**journey_body, "origin_place_id": "mock:pune-maharashtra", "destination_place_id": "mock:panaji-goa"}
    status, _, body = call("POST", f"{GATEWAY}/v1/journeys", trip, token=a["access_token"])
    a_trip = ((body or {}).get("data") or {}).get("id")
    status2, _, body2 = call("POST", f"{GATEWAY}/v1/journeys", {**trip, "destination_place_id": "mock:calangute-goa"},
                             token=b["access_token"])
    b_trip = ((body2 or {}).get("data") or {}).get("id")
    check("alice (Pune -> Panaji) and bob (Pune -> Calangute) post trips", status == 201 and status2 == 201, (body, body2))

    def a_matches() -> list[dict[str, Any]]:
        _, _, mb = call("GET", f"{GATEWAY}/v1/journeys/{a_trip}/matches", token=a["access_token"])
        return list(((mb or {}).get("data") or {}).get("items", []))

    def a_banner() -> dict[str, Any]:
        _, _, jb = call("GET", f"{GATEWAY}/v1/journeys/{a_trip}", token=a["access_token"])
        return dict(((jb or {}).get("data") or {}).get("matches") or {})

    matched: dict[str, Any] | None = None
    for _ in range(40):  # journey.created -> matching worker -> journey_matches (< 2 s)
        matched = next((m for m in a_matches() if m["id"] == b_trip), None)
        if matched and a_banner().get("state") == "done":
            break
        time.sleep(0.25)
    check("bob's trip is in alice's matches with a Journey Match score",
          matched is not None and isinstance(matched.get("journey_match"), int) and matched["journey_match"] >= 50, matched)
    banner = a_banner()
    check("alice's banner: done, with a count and best score",
          banner.get("state") == "done" and banner.get("count", 0) >= 1
          and (banner.get("best_score") or 0) >= ((matched or {}).get("journey_match") or 101), banner)
    status, _, body = call("GET", f"{GATEWAY}/v1/journeys/{a_trip}/matches", token=b["access_token"])
    check("only the host can list a journey's matches", status == 403 and body["error"]["type"] == "not_owner", body)
    before = banner.get("count", 0)
    call("DELETE", f"{GATEWAY}/v1/journeys/{b_trip}", token=b["access_token"])
    purged = False
    for _ in range(40):  # journey.deleted -> worker purges both directions and recounts
        if b_trip not in [m["id"] for m in a_matches()] and a_banner().get("count") == before - 1:
            purged = True
            break
        time.sleep(0.25)
    check("bob deletes his trip: it leaves alice's matches and banner", purged, a_banner())
    call("DELETE", f"{GATEWAY}/v1/journeys/{a_trip}", token=a["access_token"])

    print("chat + notifications (M5)")
    ws_base = GATEWAY.replace("http", "ws", 1) + "/v1/ws?token="
    status, _, body = call("POST", f"{GATEWAY}/v1/me/devices", {"token": f"smoke-{run}-alice-device", "platform": "android"},
                           token=a["access_token"])
    check("alice registers a push device (notification-service)", status == 200, body)
    status, _, body = call("POST", f"{GATEWAY}/v1/journeys", {**trip, "group_size": 4}, token=a["access_token"])
    host_trip = ((body or {}).get("data") or {}).get("id")
    alice_ws = WebSocket(ws_base + a["access_token"])
    alice_ws.send("ping", id_="p1")
    check("alice's socket is up (gateway -> chat-ws)", alice_ws.recv("pong")["id"] == "p1")

    status, _, body = call("POST", f"{GATEWAY}/v1/connection-requests",
                           {"target_journey_id": host_trip, "message": "Count me in! Call me on 98765 43210"},
                           token=b["access_token"])
    sent = (body or {}).get("data") or {}
    check("bob asks to join; contact details are masked",
          status == 201 and sent.get("message") == "Count me in! Call me on •••" and sent.get("journey_match") is None, body)
    try:
        live = alice_ws.recv("request.new")["data"]
        live_ok = live["id"] == sent.get("id") and live["user"]["id"] == b["user_id"]
    except Exception as exc:
        live_ok, live = False, exc
    check("alice sees the request live on her socket", live_ok, live)

    def alice_pushes(fragment: str) -> bool:
        for _ in range(40):
            _, _, pb = call("GET", f"{GATEWAY}/v1/me/devices/test-pushes", token=a["access_token"])
            if any(fragment in p["body"] for p in ((pb or {}).get("data") or [])):
                return True
            time.sleep(0.25)
        return False

    check("...and as a push (mock FCM)", alice_pushes("wants to join your trip Pune → Panaji"))
    status, _, body = call("POST", f"{GATEWAY}/v1/connection-requests/{sent.get('id')}/accept", token=a["access_token"])
    conversation = ((body or {}).get("data") or {}).get("conversation_id")
    check("alice accepts: they're connected, with a conversation", status == 200 and bool(conversation), body)
    seats = False
    for _ in range(40):  # chat.request_accepted -> journey-service takes a seat
        _, _, jb = call("GET", f"{GATEWAY}/v1/journeys/{host_trip}", token=a["access_token"])
        if ((jb or {}).get("data") or {}).get("seats_left") == 2:
            seats = True
            break
        time.sleep(0.25)
    check("the accepted request takes a seat on alice's trip", seats)
    _, _, pb = call("GET", f"{GATEWAY}/v1/users/{b['user_id']}", token=a["access_token"])
    check("bob's profile shows connection_status=connected for alice",
          ((pb or {}).get("data") or {}).get("connection_status") == "connected", pb)

    bob_ws = WebSocket(ws_base + b["access_token"])
    alice_ws.send("message.send", {"conversation_id": conversation, "client_msg_id": f"{run}-1", "body": "Welcome aboard!"}, "m1")
    ack = alice_ws.recv("ack", where=None)
    got = bob_ws.recv("message.new", where=lambda d: d.get("body") == "Welcome aboard!")
    check("alice -> bob in real time", ack["data"].get("message_id") == got["data"]["id"])
    bob_ws.send("message.send", {"conversation_id": conversation, "client_msg_id": f"{run}-2", "body": "Thanks! See you there"}, "m2")
    bob_ws.recv("ack")
    back = alice_ws.recv("message.new", where=lambda d: d.get("body") == "Thanks! See you there")
    check("bob -> alice in real time", back["data"]["sender_id"] == b["user_id"])
    alice_ws.close()
    time.sleep(0.5)  # alice's presence clears when her socket closes
    bob_ws.send("message.send", {"conversation_id": conversation, "client_msg_id": f"{run}-3", "body": "Shall we leave at 6?"}, "m3")
    bob_ws.recv("ack")
    check("with alice's socket closed, bob's message reaches her as a push", alice_pushes("Shall we leave at 6?"))
    _, _, pb = call("GET", f"{GATEWAY}/v1/me/devices/test-pushes", token=a["access_token"])
    check("...but not the one she already saw live (presence at send time)",
          not any("See you there" in p["body"] for p in ((pb or {}).get("data") or [])), pb)
    status, _, body = call("GET", f"{GATEWAY}/v1/conversations/badge", token=a["access_token"])
    check("alice's chat badge counts the unread message",
          status == 200 and body["data"]["unread_total"] >= 1, body)
    bob_ws.close()
    call("DELETE", f"{GATEWAY}/v1/journeys/{host_trip}", token=a["access_token"])

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
    for q in ("user-service.dlq", "social-service.dlq", "journey-service.dlq", "chat-service.dlq",
              "notification-service.dlq", "notification.email.dlq"):
        depth = queue_depth(q)
        check(f"{q} is empty", depth == 0, depth)

    failed = [name for name, ok in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
