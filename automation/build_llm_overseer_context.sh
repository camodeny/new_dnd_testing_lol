#!/usr/bin/env zsh
set -euo pipefail

ROOT="/Users/cpendergrass/Programming/new_dnd_testing_lol"
ENV_FILE="${LLM_CAMPAIGN_ENV_FILE:-$ROOT/automation/llm_campaign.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

exec python3 "$ROOT/automation/build_llm_overseer_context.py" "$@"
