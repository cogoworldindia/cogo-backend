"""Write a service's OpenAPI spec without any infrastructure: dummy settings + throwaway keys.
Run inside a service dir: `PYTHONPATH=. uv run python ../../scripts/export_openapi.py out.json`."""

import json
import os
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

tmp = Path(tempfile.mkdtemp())
(tmp / "public").mkdir()
pem = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption())
(tmp / "key.pem").write_bytes(pem)
os.environ.update({
    "ENV": "development", "METRICS_ENABLED": "false", "LOG_JSON": "false",
    "DATABASE_URL": "postgresql+asyncpg://u:p@h/db", "RABBITMQ_URL": "amqp://u:p@h/", "REDIS_URL": "redis://h/0",
    "OTP_HMAC_KEY": "x", "CURSOR_SECRET": "x", "JWT_PRIVATE_KEY_FILE": str(tmp / "key.pem"), "SMTP_HOST": "localhost",
    "SERVICE_PRIVATE_KEY_FILE": str(tmp / "key.pem"), "SERVICE_PUBLIC_KEYS_DIR": str(tmp / "public"),
})
from app.main import create_app  # noqa: E402

spec = create_app().openapi()
Path(sys.argv[1]).write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
print(f"{spec['info']['title']}: {len(spec['paths'])} paths")
