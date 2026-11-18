import json, os, sys, tempfile
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

service, out = sys.argv[1], sys.argv[2]
common = dict(ENV="development", DATABASE_URL="postgresql+asyncpg://u:p@h/db", RABBITMQ_URL="amqp://u:p@h/",
              REDIS_URL="redis://h/0", METRICS_ENABLED=False)
if service == "auth":
    key = Path(tempfile.mkdtemp()) / "k.pem"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    from app.config import Settings; from app.main import create_app
    app = create_app(Settings(**common, JWT_PRIVATE_KEY_FILE=key, OTP_HMAC_KEY="x"))
else:
    from app.config import Settings; from app.main import create_app
    app = create_app(Settings(**common))
Path(out).write_text(json.dumps(app.openapi(), indent=1))
print(service, "paths:", sorted(app.openapi()["paths"]))
