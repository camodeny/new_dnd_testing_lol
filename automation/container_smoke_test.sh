#!/usr/bin/env sh
set -eu

# Image-level smoke test for the installed dnd-automationctl wrapper.
#
# Verifies that the in-container /usr/local/bin/dnd-automationctl symlink
# resolves the real application root, works when invoked from outside the
# repository directory, and prints a clear diagnostic when the automation
# control env file is missing.
#
# Usage:
#   sh automation/container_smoke_test.sh
#   DND_IMAGE_TAG=dnd-app:some-tag sh automation/container_smoke_test.sh
#   DND_IMAGE_TAG=... DND_RUN_ID=40 sh automation/container_smoke_test.sh --live-app

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
IMAGE_TAG="${DND_IMAGE_TAG:-dnd-app:issue-102-smoke}"
DOCKER="${DOCKER:-docker}"

if [ "${DND_IMAGE_TAG:-}" ]; then
  echo "Using prebuilt image: $IMAGE_TAG"
else
  echo "Building image $IMAGE_TAG ..."
  "$DOCKER" build -t "$IMAGE_TAG" "$REPO_ROOT"
fi

echo "== Installed command works from outside the repository directory =="
"$DOCKER" run --rm --entrypoint sh "$IMAGE_TAG" -c \
  'cd /tmp && dnd-automationctl --help' \
  | grep -q 'usage:'
echo "OK"

echo "== Installed command does not fail on missing control env file =="
OUTPUT="$("$DOCKER" run --rm -e LLM_CAMPAIGN_ENV_FILE=/app/data/automation/llm_campaign.env \
  --entrypoint sh "$IMAGE_TAG" -c 'cd /tmp && dnd-automationctl --help 2>&1 || true')"
echo "$OUTPUT" | grep -q 'automation control env file not found'
echo "$OUTPUT" | grep -q 'dnd-ensure-host-audit-env'
echo "OK"

if [ "${1:-}" = "--live-app" ]; then
  CONTAINER="${DND_APP_CONTAINER:-new_dnd_testing_lol-app-1}"
  : "${DND_RUN_ID:?DND_RUN_ID is required with --live-app}"
  echo "== Runbook command against deployed app container $CONTAINER =="
  docker exec "$CONTAINER" \
    dnd-automationctl run status \
    --run-id "$DND_RUN_ID" \
    --pretty
  echo "OK"
fi

echo "All container smoke checks passed."
