#!/usr/bin/env bash
set -euo pipefail

# Canonical frontend CI — runnable locally and in GitHub Actions.
# Mirrors the `frontend` job in .github/workflows/ci.yml.
# Usage:
#   ./scripts/ci/frontend.sh

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/frontend"

echo "== frontend: install =="
if [[ ! -d "node_modules" ]]; then
  npm ci
fi

echo "== frontend: lint (eslint) =="
npm run lint

echo "== frontend: typecheck (tsc) =="
npm run typecheck

echo "== frontend: build (next build) =="
npm run build
