#!/usr/bin/env sh
set -eu

mkdir -p /app/data

exec gunicorn \
  -w "${WEB_CONCURRENCY:-2}" \
  -b "0.0.0.0:${PORT:-5001}" \
  --timeout "${GUNICORN_TIMEOUT:-300}" \
  app:app
