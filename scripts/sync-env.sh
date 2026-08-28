#!/usr/bin/env bash
set -euo pipefail

# sync env files from main worktree into current worktree
# run from any worktree: ./scripts/sync-env.sh

MAIN_ROOT=$(git worktree list | head -n1 | awk '{print $1}')
CURRENT_ROOT=$(git rev-parse --show-toplevel)

if [[ "$MAIN_ROOT" == "$CURRENT_ROOT" ]]; then
  echo "already in main worktree ($MAIN_ROOT) - nothing to sync"
  exit 0
fi

echo "syncing env files from $MAIN_ROOT -> $CURRENT_ROOT"

files=(
  ".env"
  ".deploy.env"
  "backend/.env"
  "frontend/.env.local"
)

for f in "${files[@]}"; do
  src="$MAIN_ROOT/$f"
  dst="$CURRENT_ROOT/$f"
  if [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -v "$src" "$dst"
  else
    echo "skip $f (not found in main)"
  fi
done

echo "done"
