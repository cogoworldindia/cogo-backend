#!/bin/bash
set -e

HOST="localhost"
PORT=6379

if redis-cli -h "$HOST" -p "$PORT" ping > /dev/null 2>&1; then
  echo "✅ Local Redis is running at $HOST:$PORT."
  exit 0
else
  echo "⚠️  No local Redis detected."
  exit 1
fi
