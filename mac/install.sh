#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source="$repo_root/automation/codex_issue_launcher/codex_issue_launcher.py"
destination_dir="$HOME/Library/Application Support/CodexIssueLauncher/bin"
destination="$destination_dir/codex_issue_launcher.py"

test -f "$source"
mkdir -p "$destination_dir"
install -m 755 "$source" "$destination"
python3 -m py_compile "$destination"
printf 'Installed %s\n' "$destination"
