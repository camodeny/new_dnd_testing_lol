#!/usr/bin/env bash
# Vercel build-time migration gate — issue #187
# Runs only for production deployments; preview/development skip.
# Failing this script fails the Vercel build, preventing promotion.
set -euo pipefail

ENV_NAME="${VERCEL_ENV:-${VERCEL_GIT_COMMIT_REF:-}}"
# Normalize: VERCEL_ENV is production|preview|development when set.
if [[ "${VERCEL_ENV:-}" == "production" ]]; then
  echo "[migrate] VERCEL_ENV=production — running explicit migrations before promotion"
  # POSTGRES_URL/DATABASE_URL are injected by Vercel's Supabase integration for
  # production. Locally, database.py / scripts/migrate load .env via dotenv,
  # so no manual sourcing is required here.
  # Run the explicit migration runner (logs revision before/after and exits non-zero on failure)
  # Prefer python3 (Vercel) but fall back to python
  if command -v python3 >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
  set +e
  $PY -m scripts.migrate
  RC=$?
  set -e
  if [[ $RC -ne 0 ]]; then
    echo "[migrate] ERROR: Migration failed (exit $RC) — failing build to prevent unhealthy promotion" >&2
    exit $RC
  fi
  echo "[migrate] Migrations verified — proceeding with build"
else
  echo "[migrate] Skipping migrations for VERCEL_ENV=${VERCEL_ENV:-unknown} (only production runs migrations; previews do not race)"
fi
