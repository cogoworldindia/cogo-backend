#!/bin/bash
set -e

DB_NAME="${POSTGRES_DB:-authdb}"
DB_USER="${POSTGRES_USER:-postgres}"
HOST="localhost"

echo "🧠 Checking for existing database '$DB_NAME'..."

if psql -h "$HOST" -U "$DB_USER" -lqt 2>/dev/null | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
  echo "✅ Database '$DB_NAME' already exists."
else
  echo "🆕 Creating database '$DB_NAME'..."
  createdb -h "$HOST" -U "$DB_USER" "$DB_NAME"
  echo "✅ Database '$DB_NAME' created."
fi
