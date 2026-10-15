#!/usr/bin/env bash
# Optional dev helper (not part of the framework): copy the working tree from an authoring machine (e.g. Windows
# Git Bash) to the Docker jump box. Settings from the environment or .sync.env (gitignored):
#   SYNC_HOST=user@jumpbox   SYNC_DIR=ocp-upi (relative to the remote home)   SYNC_SSH_OPTS="-i ~/.ssh/key"
# Never copied: .state/ (runtime state), clusters/vault.yml (lives on the jump box). tar never deletes, so after
# removing or renaming a file here, delete it on the jump box too.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
[[ -f $ROOT/.sync.env ]] && source "$ROOT/.sync.env"
: "${SYNC_HOST:?set SYNC_HOST=user@host (env or .sync.env)}"
SYNC_DIR=${SYNC_DIR:-ocp-upi}
# shellcheck disable=SC2086
tar -C "$ROOT" -czf - \
  --exclude=./.git --exclude=./.state --exclude=./.sync.env --exclude=./clusters/vault.yml \
  --exclude='./platforms/*/.terraform' . \
  | ssh ${SYNC_SSH_OPTS:-} "$SYNC_HOST" "mkdir -p '$SYNC_DIR' && tar -xzf - -C '$SYNC_DIR'"
echo "synced to $SYNC_HOST:$SYNC_DIR"
