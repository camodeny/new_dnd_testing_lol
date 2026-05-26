#!/usr/bin/env sh
set -eu

mkdir -p /app/data

exec gunicorn \
  -k "${GUNICORN_WORKER_CLASS:-gthread}" \
  -w "${WEB_CONCURRENCY:-3}" \
  --threads "${WEB_THREADS:-12}" \
  -b "0.0.0.0:${PORT:-5001}" \
  --timeout "${GUNICORN_TIMEOUT:-300}" \
  app:app
