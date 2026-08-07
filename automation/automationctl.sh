#!/usr/bin/env sh
set -eu

resolve_script() {
  script="$1"
  while [ -L "$script" ]; do
    dir="$(CDPATH= cd -- "$(dirname -- "$script")" && pwd)"
    target="$(readlink "$script")"
    case "$target" in
      /*) script="$target" ;;
      *) script="$dir/$target" ;;
    esac
  done
  printf '%s' "$script"
}

script="$(resolve_script "$0")"
script_dir="$(CDPATH= cd -- "$(dirname -- "$script")" && pwd)"
ROOT="${AUTOMATION_ROOT:-$(dirname -- "$script_dir")}"

. "$ROOT/automation/load_llm_campaign_env.sh"

exec python3 "$ROOT/automation/automationctl.py" "$@"
