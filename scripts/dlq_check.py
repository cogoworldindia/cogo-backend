#!/usr/bin/env python3
"""Dead-letter queues and their depths (spec §11: alert when a DLQ is above 0; §13 M8: all
DLQs empty after a 1-hour soak). Also lists work queues still holding messages.

    make dlq            exit 1 if any DLQ holds a message, or no DLQ exists at all

Reads RabbitMQ's management API with the credentials in .env/.env.secrets (never printed).
"""

import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import smoke  # noqa: E402  (loads .env files; HTTP helper)


def main() -> int:
    user, password = os.environ.get("RABBITMQ_USER", "cogo"), os.environ["RABBITMQ_PASSWORD"]
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    status, _, queues = smoke.call(
        "GET", f"{smoke.RABBIT}/api/queues/%2F?columns=name,messages,consumers",
        headers={"Authorization": f"Basic {auth}"},
    )  # fmt: skip
    if status != 200 or not isinstance(queues, list):
        print(f"RabbitMQ management API answered {status}")
        return 1
    dlqs = sorted((q for q in queues if q["name"].endswith(".dlq")), key=lambda q: q["name"])
    work = [q for q in queues if not q["name"].endswith(".dlq") and q.get("messages")]
    for q in dlqs:
        print(f"{'FULL ' if q.get('messages') else 'empty'} {q['name']}: {q.get('messages', 0)}")
    for q in work:
        print(f"backlog {q['name']}: {q['messages']} (consumers {q.get('consumers', 0)})")
    full = [q for q in dlqs if q.get("messages")]
    print(f"{len(dlqs)} dead-letter queues, {len(full)} holding messages")
    return 1 if full or not dlqs else 0


if __name__ == "__main__":
    sys.exit(main())
