#!/bin/bash
set -e

HOST="localhost"
PORT=5432

if pg_isready -h "$HOST" -p "$PORT" > /dev/null 2>&1; then
  echo "✅ Local PostgreSQL is running at $HOST:$PORT."
  exit 0
else
  echo "⚠️  No local PostgreSQL detected."
  exit 1
fi
