#!/bin/bash
# Stops RPG HOME: the engine launchd job, then the broker (the volume is kept).
# Safe to run when nothing runs.
set -euo pipefail

LOG_TAG=stop
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if job_loaded; then
  # launchd sends SIGTERM, then SIGKILL after ExitTimeOut.
  launchctl bootout "$(job_target)" || true
  wait_unloaded || die "engine: $ENGINE_LABEL is still loaded"
  log "engine: stopped ($ENGINE_LABEL unloaded)"
else
  log "engine: not loaded"
fi

# Without Docker the broker cannot be running: nothing to stop.
if ! DOCKER="$(find_docker)"; then
  log "broker: not running (docker not found)"
  exit 0
fi
PATH="$(dirname "$DOCKER"):$PATH"
export PATH
if ! "$DOCKER" info >/dev/null 2>&1; then
  log "broker: not running (Docker daemon is not running)"
  exit 0
fi
docker_compose down
log "broker: stopped"
