#!/usr/bin/env bash
# Idempotent local secret bootstrap: never overwrites an existing non-placeholder value.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { cp .env.example .env; echo "created .env"; }
[[ -f .env.secrets ]] || { cp .env.secrets.example .env.secrets; echo "created .env.secrets"; }
chmod 600 .env.secrets

# Rewrite .env.secrets through a temp file (portable: no `sed -i` differences).
set_secret() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp .env.secrets.XXXXXX)"
  awk -v k="$key" -v v="$value" -F= '
    $1 == k { if (!done) print k "=" v; done = 1; next } { print }
    END { if (!done) print k "=" v }' .env.secrets > "$tmp"
  chmod 600 "$tmp" && mv "$tmp" .env.secrets
}

# The old shared HS256 signing secret was leaked in git history and is no longer used.
if grep -q '^JWT_SECRET_KEY=' .env.secrets; then
  tmp="$(mktemp .env.secrets.XXXXXX)"
  grep -v '^JWT_SECRET_KEY=' .env.secrets > "$tmp" || true
  chmod 600 "$tmp" && mv "$tmp" .env.secrets
  echo "removed retired JWT_SECRET_KEY from .env.secrets"
fi

for key in POSTGRES_PASSWORD AUTH_DB_PASSWORD USER_DB_PASSWORD JOURNEY_DB_PASSWORD SOCIAL_DB_PASSWORD \
           CHAT_DB_PASSWORD NOTIFICATION_DB_PASSWORD RABBITMQ_PASSWORD OTP_HMAC_KEY CURSOR_SECRET MEDIA_ACCESS_KEY MEDIA_SECRET_KEY; do
  current="$(grep -E "^${key}=" .env.secrets | tail -1 | cut -d= -f2- || true)"
  if [[ -z "$current" || "$current" == "CHANGE_ME" ]]; then
    set_secret "$key" "$(openssl rand -hex 24)"
    echo "generated ${key}"
  fi
done

mkdir -p secrets && chmod 700 secrets
if [[ ! -f secrets/jwt_private_key.pem ]]; then
  openssl genpkey -algorithm ed25519 -out secrets/jwt_private_key.pem
  echo "generated secrets/jwt_private_key.pem (Ed25519 access-token signing key)"
fi
# Readable by the non-root container user; the secrets/ directory itself stays 700.
chmod 644 secrets/jwt_private_key.pem

# Per-service Ed25519 keys for service-to-service tokens (spec §1A.4). Each service gets
# only its own private key; every service sees the public keys of all of them.
mkdir -p secrets/service-keys/public
for svc in auth-service user-service journey-service social-service chat-service chat-ws notification-service admin-service; do
  if [[ ! -f "secrets/service-keys/${svc}.pem" ]]; then
    openssl genpkey -algorithm ed25519 -out "secrets/service-keys/${svc}.pem"
    echo "generated service key for ${svc}"
  fi
  openssl pkey -in "secrets/service-keys/${svc}.pem" -pubout -out "secrets/service-keys/public/${svc}.pem" 2>/dev/null
  chmod 644 "secrets/service-keys/${svc}.pem" "secrets/service-keys/public/${svc}.pem"
done
