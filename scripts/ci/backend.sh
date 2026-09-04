#!/usr/bin/env bash
set -euo pipefail

# Canonical backend CI — runnable locally and in GitHub Actions.
# Mirrors the `backend` job in .github/workflows/ci.yml.
# Usage:
#   ./scripts/ci/backend.sh
#   DATABASE_URL=postgresql://... ./scripts/ci/backend.sh   # with disposable Postgres

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/backend"

echo "== backend: install (if needed) =="
if ! python3 -c "import fastapi, pytest, ruff" 2>/dev/null; then
  python3 -m pip install --upgrade pip -q
  python3 -m pip install -r requirements-dev.txt -q
fi

echo "== backend: static checks (ruff) =="
python3 -m ruff check . --select E9,F63,F7,F82

DB_URL="${POSTGRES_URL_NON_POOLING:-${POSTGRES_URL:-${DATABASE_URL:-}}}"
if [[ -n "$DB_URL" ]]; then
  echo "== backend: migrate disposable DB =="
  if [[ "$DB_URL" == *"ci_test"* && "$DB_URL" != *"sslmode="* ]]; then
    if [[ "$DB_URL" == *"?"* ]]; then
      DB_URL="${DB_URL}&sslmode=disable"
    else
      DB_URL="${DB_URL}?sslmode=disable"
    fi
  fi
  export DATABASE_URL="$DB_URL"
  export POSTGRES_URL_NON_POOLING="$DB_URL"
  export FAULT_TEST_DATABASE_URL="$DB_URL"
  for i in {1..15}; do
    if python3 -c "import os, psycopg2; psycopg2.connect(os.environ['DATABASE_URL']).close()" 2>/dev/null; then break; fi
    sleep 1
  done
  python3 -m scripts.migrate
  echo "== backend: alembic current =="
  alembic current
  echo "== backend: alembic history (head) =="
  alembic history | tail -n 20
else
  echo "== backend: no DB URL — skipping migration and Postgres-marked tests =="
fi

echo "== backend: pytest =="
PYTEST_ARGS=(tests/ -v)
if [[ -z "$DB_URL" ]]; then
  PYTEST_ARGS+=(-m "not postgres")
fi
ALLOW_MOCK_AUTH=true NODE_ENV=test python3 -m pytest -p no:xonsh -p no:cacheprovider "${PYTEST_ARGS[@]}"
