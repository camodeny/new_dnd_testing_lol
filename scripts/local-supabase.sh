#!/usr/bin/env bash
set -euo pipefail

# Local Supabase stack for development and gameplay testing (docs/local-dev.md).
#
#   scripts/local-supabase.sh up      start the stack, apply the schema, schedule the
#                                     cron sweeps, seed test players, and point
#                                     backend/.env + frontend/.env.local at it
#   scripts/local-supabase.sh reset   wipe the local database (accounts too), then `up`
#   scripts/local-supabase.sh down    stop the stack; data survives until `reset`
#   scripts/local-supabase.sh status  print local URLs and keys
#
# Needs Docker. Everything here is 127.0.0.1; it never touches a hosted project.
# The cron sweeps reach the backend at BACKEND_URL from frontend/.env.local
# (default http://localhost:8000).

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_ENV="$ROOT/backend/.env"
FRONTEND_ENV="$ROOT/frontend/.env.local"

supabase() { npx --yes supabase@2.122.0 --workdir "$ROOT" "$@"; }

require_docker() {
  if ! docker info >/dev/null 2>&1; then
    echo "Docker is not running. Start Docker Desktop and retry." >&2
    exit 1
  fi
}

# stack_value KEY: one value from `supabase status` (API_URL, DB_URL, ...).
STATUS_JSON=""
stack_value() {
  [[ -n "$STATUS_JSON" ]] || STATUS_JSON="$(supabase status -o json 2>/dev/null)"
  python3 -c 'import json, sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$STATUS_JSON" "$1"
}

# point_env FILE KEY=VALUE...: set each KEY in a dotenv file in place (append
# if absent). An empty VALUE removes the key.
point_env() {
  python3 - "$@" <<'PY'
import sys

path, assignments = sys.argv[1], [arg.split("=", 1) for arg in sys.argv[2:]]
try:
    with open(path) as f:
        lines = f.read().splitlines()
except FileNotFoundError:
    lines = []

for key, value in assignments:
    active = [i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == key]
    if value and active:
        lines[active[0]] = f"{key}={value}"
        active = active[1:]
    elif value:
        lines.append(f"{key}={value}")
    lines = [line for i, line in enumerate(lines) if i not in active]

with open(path, "w") as f:
    f.write("\n".join(lines) + "\n")
PY
}

env_value() {  # env_value FILE KEY
  [[ -f "$1" ]] && grep -E "^$2=" "$1" | tail -n1 | cut -d= -f2- || true
}

# save_hosted FILE URL_KEY: on the first switch away from a hosted project,
# keep the file as .hosted.env beside it (gitignored) so it can be restored.
save_hosted() {
  local url backup
  url="$(env_value "$1" "$2")"
  backup="$(dirname "$1")/.hosted.env"
  case "$url" in "" | http://127.0.0.1:* | http://localhost:*) return ;; esac
  if [[ ! -e "$backup" ]]; then
    cp "$1" "$backup"
    echo "   saved the hosted settings in ${backup#"$ROOT"/}"
  fi
}

up() {
  require_docker
  supabase start
  STATUS_JSON=""

  local api_url db_url publishable secret service_role cron_secret backend_url backend_port
  api_url="$(stack_value API_URL)"
  db_url="$(stack_value DB_URL)?sslmode=disable"
  publishable="$(stack_value PUBLISHABLE_KEY)"
  secret="$(stack_value SECRET_KEY)"
  service_role="$(stack_value SERVICE_ROLE_KEY)"
  cron_secret="$(env_value "$BACKEND_ENV" CRON_SECRET)"
  [[ -n "$cron_secret" ]] || cron_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  backend_url="$(env_value "$FRONTEND_ENV" BACKEND_URL)"
  backend_url="${backend_url:-http://localhost:8000}"
  backend_port="$(python3 -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).port or 80)' "$backend_url")"

  echo "== pointing backend/.env and frontend/.env.local at the local stack"
  save_hosted "$BACKEND_ENV" SUPABASE_URL
  save_hosted "$FRONTEND_ENV" NEXT_PUBLIC_SUPABASE_URL
  # The other DB URL aliases outrank POSTGRES_URL in scripts/migrate.py, so
  # they are removed rather than left pointing at a hosted project.
  point_env "$BACKEND_ENV" \
    "POSTGRES_URL=$db_url" \
    "POSTGRES_URL_NON_POOLING=" "POSTGRES_PRISMA_URL=" "DATABASE_URL=" "SUPABASE_DB_URL=" \
    "SUPABASE_URL=$api_url" \
    "SUPABASE_JWKS_URL=" \
    "SUPABASE_PUBLISHABLE_KEY=$publishable" \
    "SUPABASE_SECRET_KEY=$secret" \
    "SUPABASE_SERVICE_ROLE_KEY=$service_role" \
    "CRON_SECRET=$cron_secret"
  point_env "$FRONTEND_ENV" \
    "NEXT_PUBLIC_SUPABASE_URL=$api_url" \
    "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=$publishable" \
    "BACKEND_URL=$backend_url" \
    "NEXT_PUBLIC_DEV_USER_EMAIL=" "NEXT_PUBLIC_DEV_USER_PASSWORD="

  # Explicit env outranks backend/.env, so these commands cannot reach
  # anything but the local stack.
  local -a local_env=(
    "POSTGRES_URL=$db_url" "POSTGRES_URL_NON_POOLING=$db_url" "POSTGRES_PRISMA_URL=$db_url"
    "DATABASE_URL=$db_url" "SUPABASE_DB_URL=$db_url"
    "SUPABASE_URL=$api_url" "SUPABASE_SERVICE_ROLE_KEY=$service_role" "CRON_SECRET=$cron_secret"
    "LOCAL_BACKEND_ORIGIN=http://host.docker.internal:$backend_port"
  )
  echo "== applying the Alembic schema"
  (cd "$ROOT/backend" && env "${local_env[@]}" python3 -m scripts.migrate)
  echo "== scheduling cron sweeps and seeding test players"
  (cd "$ROOT/backend" && env "${local_env[@]}" python3 -m scripts.seed_local)

  cat <<EOF

Local stack ready. Restart the backend and frontend so they pick up the new env:
  cd backend && uvicorn main:app --reload --port $backend_port
  cd frontend && npm run dev

  Sign in:  login page -> Local test players (player1..4@local.test / password)
            each tab keeps its own session, so open one tab per player
  Studio:   $(stack_value STUDIO_URL)
  Email:    $(stack_value INBUCKET_URL)
EOF
}

case "${1:-}" in
  up) up ;;
  reset)
    require_docker
    supabase db reset
    up
    ;;
  down) supabase stop ;;
  status) supabase status ;;
  *)
    sed -n '4,/^$/p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
