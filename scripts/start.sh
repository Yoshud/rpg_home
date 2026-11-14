#!/bin/bash
# Starts RPG HOME: the broker (docker compose) and the engine (launchd job).
# Safe to run again: whatever already runs is left alone.
set -euo pipefail

LOG_TAG=start
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PLIST="$STATE/launchd/$ENGINE_LABEL.plist"
ENGINE_CHECK_SECONDS="${RPGHOME_ENGINE_CHECK_SECONDS:-2}"
BROKER_WAIT_SECONDS="${RPGHOME_BROKER_WAIT_SECONDS:-15}"

# render_plist OUT: fill the template with absolute, XML-escaped values.
render_plist() {
  local text key val
  text="$(cat "$REPO_ROOT/scripts/launchd/engine.plist.in")"
  for key in LABEL ENGINE_SH REPO ARTIFACTS STATE MQTT_HOST MQTT_PORT LOG; do
    case "$key" in
      LABEL) val=$ENGINE_LABEL ;;
      ENGINE_SH) val="$REPO_ROOT/scripts/engine.sh" ;;
      REPO) val=$REPO_ROOT ;;
      ARTIFACTS) val=$ARTIFACTS ;;
      STATE) val=$STATE ;;
      MQTT_HOST) val=$RPGHOME_MQTT_HOST ;;
      MQTT_PORT) val=$RPGHOME_MQTT_PORT ;;
      LOG) val=$ENGINE_LOG ;;
    esac
    text="$(replace_all "$text" "@$key@" "$(xml_escape "$val")")"
  done
  printf '%s\n' "$text" >"$1"
  plutil -lint -s "$1" || die "generated plist is invalid: $1"
}

start_broker() {
  local i
  use_docker
  "$DOCKER" info >/dev/null 2>&1 || die "Docker daemon is not running; start OrbStack"
  docker_compose up -d
  for i in $(seq 1 $((BROKER_WAIT_SECONDS * 2))); do
    if broker_reachable; then
      log "broker: running on 127.0.0.1:$RPGHOME_MQTT_PORT"
      return 0
    fi
    sleep 0.5
  done
  die "broker not reachable on 127.0.0.1:$RPGHOME_MQTT_PORT (check: docker logs rpghome-mosquitto-1)"
}

bootstrap() {
  launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST" || die "launchctl bootstrap failed for $PLIST"
}

start_engine() {
  mkdir -p "$LOG_DIR" "$STATE/launchd"
  render_plist "$PLIST.new"
  if ! job_loaded; then
    mv "$PLIST.new" "$PLIST"
    bootstrap
    log "engine: loaded $ENGINE_LABEL"
  elif ! cmp -s "$PLIST.new" "$PLIST"; then
    launchctl bootout "$(job_target)" || true
    wait_unloaded || die "engine: $ENGINE_LABEL did not unload"
    mv "$PLIST.new" "$PLIST"
    bootstrap
    log "engine: plist changed, reloaded $ENGINE_LABEL"
  else
    rm -f "$PLIST.new"
    if [[ "$(job_field state)" == running ]]; then
      log "engine: already running (pid $(job_field pid)), nothing to do"
      return 0
    fi
    launchctl kickstart "$(job_target)" || die "launchctl kickstart failed"
    log "engine: was loaded but not running, kickstarted"
  fi
}

# check_engine: running, and still the same process after ENGINE_CHECK_SECONDS.
check_engine() {
  local i pid=""
  for i in 1 2 3 4 5 6 7 8; do # give launchd up to 2 s to spawn it
    if [[ "$(job_field state)" == running ]]; then
      pid="$(job_field pid)"
      break
    fi
    sleep 0.25
  done
  if [[ -n $pid ]]; then
    sleep "$ENGINE_CHECK_SECONDS"
    [[ "$(job_field state)" == running && "$(job_field pid)" == "$pid" ]] || pid=""
  fi
  if [[ -z $pid ]]; then
    log "engine: NOT running (state: $(job_field state), $(job_last_exit))"
    if [[ -f $ENGINE_LOG ]]; then
      log "last lines of $ENGINE_LOG:"
      tail -n 20 "$ENGINE_LOG" >&2
    fi
    die "engine failed to start"
  fi
  log "engine: running (pid $pid)"
}

start_broker
start_engine
check_engine
log "engine log: $ENGINE_LOG"
log "status: scripts/status.sh   stop: scripts/stop.sh"
