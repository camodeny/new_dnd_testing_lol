#!/usr/bin/env bash
set -euo pipefail

# Canonical backend CI — runnable locally and in GitHub Actions.
# Mirrors the `backend` job in .github/workflows/ci.yml.
# Usage:
#   ./scripts/ci/backend.sh
#   DATABASE_URL=postgresql://... ./scripts/ci/backend.sh   # with disposable Postgres

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/backend"

echo "== backend: python compile check =="
python3 -m compileall -q app database.py main.py models.py auth.py

echo "== backend: install (if needed) =="
if ! python3 -c "import fastapi" 2>/dev/null; then
  python3 -m pip install --upgrade pip -q
  pip install -r requirements.txt -q
  pip install pytest httpx -q
fi

# If a test DB URL is available, verify the explicit migration path (issue #187/#286).
# CI always provides this via the postgres service. Locally it's optional.
# The disposable Postgres service does not use SSL, so CI URLs must carry
# ?sslmode=disable to remain compatible with backend/database.py (which
# appends sslmode=require when no sslmode is present). Without it, any
# test that uses the real app DB stack would fail to connect.
DB_URL="${POSTGRES_URL_NON_POOLING:-${POSTGRES_URL:-${DATABASE_URL:-}}}"
if [[ -n "$DB_URL" ]]; then
  echo "== backend: migrate disposable DB =="
  # Ensure CI URL is explicitly sslmode=disable for the disposable service.
  if [[ "$DB_URL" == *"ci_test"* && "$DB_URL" != *"sslmode="* ]]; then
    if [[ "$DB_URL" == *"?"* ]]; then
      DB_URL="${DB_URL}&sslmode=disable"
    else
      DB_URL="${DB_URL}?sslmode=disable"
    fi
  fi
  export DATABASE_URL="$DB_URL"
  export POSTGRES_URL_NON_POOLING="$DB_URL"
  # Wait briefly for postgres service to be ready (service healthcheck covers most cases)
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
  echo "== backend: no DB URL — skipping disposable-DB migration (set DATABASE_URL to verify) =="
fi

echo "== backend: pytest =="
# Disable xonsh plugin that breaks on newer pytest (present in local dev env, not in CI)
ALLOW_MOCK_AUTH=true python3 -m pytest -p no:xonsh -p no:cacheprovider tests/ -v
