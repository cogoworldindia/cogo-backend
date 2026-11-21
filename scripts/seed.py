#!/usr/bin/env python3
"""Seed data through the public API (spec §12 `make seed`): users with avatars and tags,
journeys around Delhi NCR to Rishikesh / Manali / Jaipur / Goa with varied dates, and
connected pairs with chats. Everything goes through the gateway, so events and projections
fill the way they do in production.

    python3 scripts/seed.py [--users 50]       (make seed; make load-seed for 200)

Writes loadtest/.seed.json (tokens, journeys, conversations) for the k6 load tests. Reruns
reuse the users in that file while their tokens work. Stdlib only; reuses smoke's helpers.
"""

import argparse
import json
import random
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import smoke  # noqa: E402  (loads .env files; HTTP + Mailpit helpers)

GW = smoke.GATEWAY
OUT = Path(__file__).resolve().parents[1] / "loadtest" / ".seed.json"
ORIGINS = ["mock:noida-uttar-pradesh", "mock:delhi-delhi", "mock:gurugram-haryana",
           "mock:ghaziabad-uttar-pradesh", "mock:faridabad-haryana", "mock:greater-noida-uttar-pradesh"]  # fmt: skip
DESTINATIONS = ["mock:rishikesh-uttarakhand", "mock:manali-himachal-pradesh", "mock:jaipur-rajasthan", "mock:panaji-goa"]
FIRST = ["Aarav", "Diya", "Kabir", "Ananya", "Vihaan", "Isha", "Arjun", "Meera", "Rohan", "Sara", "Ishaan", "Tara",
         "Aditya", "Nisha", "Karan", "Riya", "Dev", "Pooja", "Neel", "Kavya", "Yash", "Aisha", "Rahul", "Sneha"]  # fmt: skip
LAST = ["Sharma", "Verma", "Gupta", "Mehta", "Iyer", "Nair", "Reddy", "Singh", "Kapoor", "Bose", "Das", "Joshi"]
CAPTIONS = ["Weekend escape, splitting fuel", "Early start, chai stops on the way", "Looking for trek buddies",
            "Road trip, playlist ready", "Beach days and good food", "Temples, ghats and sunsets", None]  # fmt: skip


def call(method: str, path: str, body: Any = None, token: str | None = None) -> tuple[int, dict[str, str], Any]:
    return smoke.call(method, GW + path, body, token)


def data(resp: tuple[int, dict[str, str], Any]) -> Any:
    return (resp[2] or {}).get("data") if isinstance(resp[2], dict) else None


def sign_in(email: str) -> dict[str, Any]:
    previous = None
    try:
        previous = smoke.otp_for(email, timeout=0.5)
    except SystemExit:
        pass
    for _ in range(6):
        status, headers, body = call("POST", "/v1/auth/email/send", {"email": email})
        if status == 202:
            break
        time.sleep(int(headers.get("retry-after", "10")) + 1)  # gateway limit or OTP cooldown
    deadline = time.time() + 30
    while True:
        code = smoke.otp_for(email, timeout=20)
        if code != previous or time.time() > deadline:
            break
        time.sleep(0.5)
    status, _, body = call("POST", "/v1/auth/email/verify", {"email": email, "code": code})
    assert status == 200, (email, status, body)
    return dict(body["data"])


def upload_avatar(token: str) -> str | None:
    up = data(call("POST", "/v1/media/upload-url", {"kind": "avatar", "content_type": "image/png"}, token)) or {}
    if not up.get("upload_url"):
        return None
    png = b"\x89PNG\r\n\x1a\n" + bytes(random.getrandbits(8) for _ in range(256))
    req = urllib.request.Request(up["upload_url"], data=png, method="PUT", headers={"Content-Type": "image/png"})
    with urllib.request.urlopen(req, timeout=10):
        pass
    confirmed = data(call("POST", f"/v1/media/{up['media_id']}/confirm", token=token)) or {}
    return confirmed.get("file_url")


def make_user(i: int, tags: list[int], known: dict[str, Any]) -> dict[str, Any]:
    email = f"seed-{i:03d}@example.com"
    user = known.get(email)
    if user and call("GET", "/v1/me", token=user["token"])[0] == 200:
        return dict(user)  # still signed in
    tokens = sign_in(email)
    rnd = random.Random(i)
    token = tokens["access_token"]
    gender = "female" if i % 2 else "male"
    profile = {
        "name": f"{FIRST[i % len(FIRST)]} {LAST[(i * 7) % len(LAST)]}",
        "username": f"seed_{i:03d}",
        "gender": gender,
        "dob": (date(1985, 1, 1) + timedelta(days=rnd.randrange(18 * 365))).isoformat(),
        "bio": rnd.choice(["Weekend traveller", "Mountains over beaches", "Always up for a road trip", "Foodie on the move"]),
        "personality_tag_ids": rnd.sample(tags, 3),
        "city_place_id": rnd.choice(ORIGINS),
    }
    status, _, body = call("PATCH", "/v1/me", profile, token)
    assert status == 200, (email, status, body)
    if avatar := upload_avatar(token):
        call("PATCH", "/v1/me", {"avatar_url": avatar}, token)
    return {"i": i, "email": email, "id": tokens["user_id"], "token": token, "refresh": tokens["refresh_token"], "gender": gender}


def add_journeys(user: dict[str, Any], count: int) -> list[str]:
    token, rnd = user["token"], random.Random(user["i"] * 31)
    upcoming = (data(call("GET", "/v1/me/journeys?tab=upcoming&limit=20", token=token)) or {}).get("items", [])
    ids = [j["id"] for j in upcoming]
    while len(ids) < count:
        start = (datetime.now(UTC) + timedelta(days=rnd.uniform(2, 60))).replace(minute=0, second=0, microsecond=0)
        body = {
            "origin_place_id": rnd.choice(ORIGINS),
            "destination_place_id": rnd.choice(DESTINATIONS),
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=rnd.choice([30, 54, 78, 120]))).isoformat(),
            "who_can_join": rnd.choices(["anyone", "women_only", "men_only", "verified_only"], [8, 1, 1, 1])[0],
            "group_size": rnd.randint(2, 6),
        }
        if caption := rnd.choice(CAPTIONS):
            body["caption"] = caption
        status, _, resp = call("POST", "/v1/journeys", body, token)
        if status != 201:
            break  # e.g. the 5-upcoming limit
        ids.append(resp["data"]["id"])
    return ids


def connect(host: dict[str, Any], guest: dict[str, Any]) -> str | None:
    """guest asks to join host's first journey, host accepts, they chat."""
    status, _, resp = call("POST", "/v1/connection-requests",
                           {"target_journey_id": host["journeys"][0], "message": "Hi! Same dates, can I join?"}, guest["token"])  # fmt: skip
    if status == 201:
        status, _, resp = call("POST", f"/v1/connection-requests/{resp['data']['id']}/accept", token=host["token"])
        conversation = (data((status, {}, resp)) or {}).get("conversation_id")
    elif status == 409 and (resp or {}).get("error", {}).get("type") == "already_connected":
        conversation = (resp["error"].get("details") or {}).get("conversation_id")
    else:
        return None
    if conversation:
        for n, (who, text) in enumerate([(guest, "Hey!"), (host, "Welcome aboard"), (guest, "What time do we leave?")]):
            call("POST", f"/v1/conversations/{conversation}/messages", {"client_msg_id": f"seed-{n}", "body": text}, who["token"])
    return conversation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--journeys", type=int, default=4, help="upcoming journeys per user (max 5)")
    args = parser.parse_args()
    known = {u["email"]: u for u in json.loads(OUT.read_text())["users"]} if OUT.exists() else {}
    if call("GET", "/health/ready")[0] != 200:
        print("The stack isn't ready (make up)")
        return 1
    started = time.time()
    probe = sign_in("seed-tags@example.com")
    tags = [t["id"] for t in data(call("GET", "/v1/personality-tags", token=probe["access_token"])) or []]
    with ThreadPoolExecutor(max_workers=8) as pool:
        users = list(pool.map(lambda i: make_user(i, tags, known), range(args.users)))
        print(f"{len(users)} users ready ({time.time() - started:.0f}s)")
        for user, ids in zip(users, pool.map(lambda u: add_journeys(u, args.journeys), users), strict=True):
            user["journeys"] = ids
    print(f"{sum(len(u['journeys']) for u in users)} upcoming journeys")
    pairs = []
    for k in range(0, len(users) - 1, 2):
        host, guest = users[k], users[k + 1]
        if host["journeys"] and (conversation := connect(host, guest)):
            pairs.append({"a": host["id"], "b": guest["id"], "a_token": host["token"], "b_token": guest["token"], "conversation": conversation})
    print(f"{len(pairs)} connected pairs with chats")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"users": users, "pairs": pairs}, indent=1))
    print(f"wrote {OUT.relative_to(OUT.parents[1])} in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
