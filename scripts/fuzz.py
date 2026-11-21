#!/usr/bin/env python3
"""Property-based fuzzing of the public API through the gateway (spec stack: Schemathesis).

    make fuzz            (MAX_EXAMPLES=25 per operation; any 5xx fails the run)

Signs in a throwaway user (fuzzing edits and deletes what it can reach, so never a real or
seed account), gives it a profile, then runs Schemathesis from its pinned image against
each service's committed OpenAPI contract (`contracts/openapi/`), `/v1/*` routes only:
- `not_a_server_error`: no 5xx for any generated input, valid or not;
- skipped: sign-out, token refresh and account deletion (they end the fuzzing session),
  the WebSocket, and admin-only routes (behind the admin allow-list and admin sign-in).

Reports go to loadtest/results/fuzz/<service>/ (JUnit + NDJSON). Run against `make load-up`,
so the gateway's rate limits don't turn the run into 429s.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import smoke  # noqa: E402  (loads .env files; HTTP + Mailpit helpers)

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "schemathesis/schemathesis:v4.33.4"
SERVICES = ["auth", "user", "journey", "social", "chat", "notification", "admin"]
SKIP_OPERATIONS = [
    "logout_v1_auth_logout_post",
    "logout_all_v1_auth_logout_all_post",
    "refresh_v1_auth_token_refresh_post",
    "delete_me_v1_me_delete",
]


def main() -> int:
    email = f"fuzz-{uuid.uuid4().hex[:8]}@example.com"
    tokens = smoke.sign_in(email)
    token = tokens["access_token"]
    status, _, body = smoke.call(
        "PATCH", f"{smoke.GATEWAY}/v1/me",
        {"name": "Fuzz Tester", "username": f"fuzz_{uuid.uuid4().hex[:8]}", "gender": "female",
         "dob": "1995-05-05", "city_place_id": "mock:delhi-delhi"}, token,
    )  # fmt: skip
    if status != 200:
        print(f"profile setup failed: {status} {body}")
        return 1
    failed = []
    for service in SERVICES:
        out = ROOT / "loadtest" / "results" / "fuzz" / service
        out.mkdir(parents=True, exist_ok=True)
        cmd = [
            "docker", "run", "--rm", "--network", "host", "--memory", "2g",
            "-v", f"{ROOT / 'contracts' / 'openapi'}:/contracts:ro", "-v", f"{out}:/report",
            IMAGE, "run", f"/contracts/{service}-service.json", "--url", smoke.GATEWAY,
            "--header", f"Authorization: Bearer {token}",
            "--include-path-regex", "^/v1/", "--exclude-path-regex", "^/v1/ws",
            *[arg for op in SKIP_OPERATIONS for arg in ("--exclude-operation-id", op)],
            "--checks", "not_a_server_error",
            "--max-examples", os.environ.get("MAX_EXAMPLES", "25"),
            "--workers", "4", "--seed", os.environ.get("SEED", "8"),
            "--report", "junit,ndjson", "--report-dir", "/report",
        ]  # fmt: skip
        print(f"== {service}", flush=True)
        if subprocess.run(cmd, check=False).returncode != 0:
            failed.append(service)
    print(f"fuzzed {len(SERVICES)} services: " + (f"failures in {', '.join(failed)}" if failed else "no server errors"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
