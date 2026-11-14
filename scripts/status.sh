#!/bin/bash
# Shows the state of the broker and the engine.
# Exit code 0 only when both run.
set -euo pipefail

LOG_TAG=status
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ok=1

if ! DOCKER="$(find_docker)"; then
  container="docker not found"
elif ! "$DOCKER" info >/dev/null 2>&1; then
  container="Docker daemon not running"
else
  PATH="$(dirname "$DOCKER"):$PATH"
  container="$(docker_compose ps --all --format '{{.State}}' mosquitto 2>/dev/null | head -n 1)" || true
  container="${container:-missing}"
fi
if broker_reachable; then
  port="reachable"
else
  port="NOT reachable"
fi
echo "broker: container $container, 127.0.0.1:$RPGHOME_MQTT_PORT $port"
[[ $container == running && $port == reachable ]] || ok=0

if job_loaded; then
  state="$(job_field state)"
  echo "engine: $ENGINE_LABEL loaded, state ${state:-unknown}, pid $(job_field pid), $(job_last_exit), runs $(job_field runs)"
  [[ $state == running ]] || ok=0
else
  echo "engine: $ENGINE_LABEL not loaded"
  ok=0
fi
echo "engine log: $ENGINE_LOG"

[[ $ok == 1 ]]
