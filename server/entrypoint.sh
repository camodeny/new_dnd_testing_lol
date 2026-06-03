#!/usr/bin/env sh
set -eu

mkdir -p /app/data

worker_class="${GUNICORN_WORKER_CLASS:-gthread}"
web_concurrency="${WEB_CONCURRENCY:-3}"
web_threads="${WEB_THREADS:-12}"

case "${DATABASE_URL:-}" in
  sqlite:*)
    web_concurrency="${WEB_CONCURRENCY:-1}"
    web_threads="${WEB_THREADS:-1}"
    ;;
esac

exec gunicorn \
  -k "${worker_class}" \
  -w "${web_concurrency}" \
  --threads "${web_threads}" \
  -b "0.0.0.0:${PORT:-5001}" \
  --timeout "${GUNICORN_TIMEOUT:-300}" \
  app:app
