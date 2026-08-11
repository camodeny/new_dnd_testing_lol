#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
. "$ROOT/automation/load_llm_campaign_env.sh"

exec python3 "$ROOT/automation/build_llm_overseer_context.py" "$@"
