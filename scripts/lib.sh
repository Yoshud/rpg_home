# Common helpers for the RPG HOME scripts. Source it, do not run it:
#
#   LOG_TAG=start; source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
#
# Compatible with macOS /bin/bash 3.2. On source it resolves the settings the same
# way as src/rpghome/core/config.py and exports them:
#   RPGHOME_ARTIFACTS  data folder   (default ~/rpg_home_artifacts)
#   RPGHOME_STATE      state folder  (default ~/.local/state/rpghome), not inside the data folder
#   RPGHOME_MQTT_HOST  (default 127.0.0.1), RPGHOME_MQTT_PORT (default 1883)
# and sets REPO_ROOT, ARTIFACTS, STATE, LOG_DIR, ENGINE_LOG, ENGINE_LABEL.

LOG_TAG="${LOG_TAG:-$(basename "$0" .sh)}"

log() { printf '[%s] %s\n' "$LOG_TAG" "$*"; }
die() {
  printf '[%s] error: %s\n' "$LOG_TAG" "$*" >&2
  exit 1
}

# replace_all TEXT PATTERN VALUE: literal replacement (no &/backslash magic in any bash).
replace_all() {
  local text=$1 pat=$2 val=$3 out=""
  while [[ $text == *"$pat"* ]]; do
    out+="${text%%"$pat"*}$val"
    text=${text#*"$pat"}
  done
  printf '%s' "$out$text"
}

xml_escape() {
  local s
  s=$(replace_all "$1" '&' '&amp;')
  s=$(replace_all "$s" '<' '&lt;')
  replace_all "$s" '>' '&gt;'
}

# abs_path PATH: like Python's Path(PATH).expanduser().resolve(): leading ~ expanded,
# made absolute, symlinks of the existing part resolved, . and .. removed.
abs_path() {
  local p=$1 rest="" out comp
  local -a comps
  case "$p" in
    "~") p=$HOME ;;
    "~/"*) p="$HOME/${p#"~/"}" ;;
  esac
  [[ $p == /* ]] || p="$PWD/$p"
  while [[ ! -d $p ]]; do
    rest="/${p##*/}$rest"
    p=${p%/*}
    [[ -n $p ]] || p=/
  done
  out=$(cd "$p" && pwd -P)
  IFS=/ read -r -a comps <<<"$rest"
  for comp in ${comps[@]+"${comps[@]}"}; do
    case "$comp" in
      "" | .) ;;
      ..)
        out=${out%/*}
        [[ -n $out ]] || out=/
        ;;
      *) out="${out%/}/$comp" ;;
    esac
  done
  printf '%s\n' "$out"
}

# find_docker: path of the docker binary (PATH, then ~/.orbstack/bin).
find_docker() {
  if command -v docker >/dev/null 2>&1; then
    command -v docker
  elif [[ -x "$HOME/.orbstack/bin/docker" ]]; then
    echo "$HOME/.orbstack/bin/docker"
  else
    return 1
  fi
}

# use_docker: die unless docker is found; put its folder on PATH (OrbStack helpers).
use_docker() {
  DOCKER="$(find_docker)" || die "docker not found on PATH or in ~/.orbstack/bin; install OrbStack"
  PATH="$(dirname "$DOCKER"):$PATH"
  export PATH
}

docker_compose() {
  "$DOCKER" compose -f "$REPO_ROOT/docker-compose.yml" -p rpghome "$@"
}

broker_reachable() {
  nc -z -G 1 127.0.0.1 "$RPGHOME_MQTT_PORT" >/dev/null 2>&1
}

# launchd job of the engine (gui domain of the current user).
LAUNCHD_DOMAIN="gui/$(id -u)"

job_target() { printf '%s/%s\n' "$LAUNCHD_DOMAIN" "$ENGINE_LABEL"; }

job_loaded() { launchctl print "$(job_target)" >/dev/null 2>&1; }

# job_field NAME: a top-level field of `launchctl print` (state, pid, runs, last exit code).
job_field() {
  local out
  out=$(launchctl print "$(job_target)" 2>/dev/null) || return 0
  printf '%s\n' "$out" | awk -v k="$1" '
    !found && index($0, "\t" k " = ") == 1 { print substr($0, length(k) + 5); found = 1 }'
}

# job_last_exit: "last exit code N", else "last signal <value>", else "never exited".
# After a kill launchd prints "last terminating signal = Killed: 9" instead of an exit code.
job_last_exit() {
  local v
  v="$(job_field 'last exit code')"
  if [[ -n $v && $v != "(never exited)" ]]; then
    printf 'last exit code %s\n' "$v"
    return 0
  fi
  v="$(job_field 'last terminating signal')"
  if [[ -n $v ]]; then
    printf 'last signal %s\n' "$v"
    return 0
  fi
  printf 'never exited\n'
}

# wait_unloaded: bootout returns before launchd has fully removed the job.
wait_unloaded() {
  local i
  for i in $(seq 1 60); do
    job_loaded || return 0
    sleep 0.25
  done
  return 1
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACTS="$(abs_path "${RPGHOME_ARTIFACTS:-$HOME/rpg_home_artifacts}")"
STATE="$(abs_path "${RPGHOME_STATE:-$HOME/.local/state/rpghome}")"
if [[ $STATE == "$ARTIFACTS" || $STATE == "${ARTIFACTS%/}/"* ]]; then
  die "RPGHOME_STATE ($STATE) must not be inside the data folder ($ARTIFACTS)"
fi
RPGHOME_MQTT_HOST="${RPGHOME_MQTT_HOST:-127.0.0.1}"
RPGHOME_MQTT_PORT="${RPGHOME_MQTT_PORT:-1883}"
[[ $RPGHOME_MQTT_PORT =~ ^[0-9]+$ ]] || die "RPGHOME_MQTT_PORT is not a number: '$RPGHOME_MQTT_PORT'"
RPGHOME_ARTIFACTS=$ARTIFACTS
RPGHOME_STATE=$STATE
export RPGHOME_ARTIFACTS RPGHOME_STATE RPGHOME_MQTT_HOST RPGHOME_MQTT_PORT

LOG_DIR="$STATE/logs"
ENGINE_LOG="$LOG_DIR/engine.log"
ENGINE_LABEL="${RPGHOME_ENGINE_LABEL:-local.rpghome.engine}"
[[ $ENGINE_LABEL =~ ^[A-Za-z0-9._-]+$ ]] || die "RPGHOME_ENGINE_LABEL may contain only letters, digits, '.', '_' and '-'"
