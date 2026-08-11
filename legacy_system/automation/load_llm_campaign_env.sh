#!/usr/bin/env sh
# Shared environment loading for local automation entrypoints.
# Project provider settings load first; automation control settings may override them.

ROOT="${ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
APP_ENV_FILE="${DND_APP_ENV_FILE:-$ROOT/.env}"
ENV_FILE="${LLM_CAMPAIGN_ENV_FILE:-$ROOT/automation/llm_campaign.env}"

load_env_file() {
  if [ -f "$1" ]; then
    set -a
    . "$1"
    set +a
  fi
}

load_env_file "$APP_ENV_FILE"
load_env_file "$ENV_FILE"

if [ ! -f "$ENV_FILE" ]; then
  printf 'dnd-automationctl: automation control env file not found: %s\n' "$ENV_FILE" >&2
  printf 'dnd-automationctl: generate it with: dnd-ensure-host-audit-env --username <owner> --env-file %s\n' "$ENV_FILE" >&2
fi
