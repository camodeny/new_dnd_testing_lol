#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${LLM_CAMPAIGN_ENV_FILE:-$ROOT/automation/llm_campaign.env}"

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

exec python3 "$ROOT/automation/run_llm_campaign_orchestrator.py" "$@"
