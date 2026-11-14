#!/usr/bin/env bash
# First-time setup for RPG HOME. Safe to run any number of times.
#
#   scripts/setup.sh            # all steps
#   scripts/setup.sh data       # one step (python | deps | docker | data)
#
# Each step is one function so it maps one-to-one onto an Ansible task later.
# Environment: RPGHOME_ARTIFACTS (data folder, default ~/rpg_home_artifacts),
#              RPGHOME_SKIP_PULL=1 (do not run `docker compose pull`).
set -euo pipefail

LOG_TAG=setup
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DATA_SUBDIRS=(scenes tiles generated epidemic)
DATA_GITIGNORE=(
  "# audio (never in git; epidemic/ clips must never be shared)"
  "*.wav" "*.WAV" "*.flac" "*.FLAC" "*.mp3" "*.MP3" "*.ogg" "*.OGG"
  "*.m4a" "*.M4A" "*.aif" "*.AIF" "*.aiff" "*.AIFF" "*.aifc" "*.AIFC" "*.opus" "*.OPUS"
  "# clip catalog database (catalog.jsonl export is tracked)"
  "catalog.sqlite" "catalog.sqlite-wal" "catalog.sqlite-shm"
)

step_python() {
  command -v uv >/dev/null 2>&1 || die "uv not found; install it (brew install uv)"
  uv python install 3.13
  log "python: ok"
}

step_deps() {
  (cd "$REPO_ROOT" && uv sync)
  log "deps: ok"
}

step_docker() {
  local docker
  docker="$(find_docker)" || die "docker not found on PATH or in ~/.orbstack/bin; install OrbStack"
  "$docker" compose version >/dev/null 2>&1 || die "docker compose is not available"
  "$docker" info >/dev/null 2>&1 || die "Docker daemon is not running; start OrbStack"
  if [[ "${RPGHOME_SKIP_PULL:-0}" != "1" ]]; then
    "$docker" compose -f "$REPO_ROOT/docker-compose.yml" pull
  fi
  log "docker: ok ($docker)"
}

step_data() {
  local dir line
  local -a paths=(.gitignore) ident=()
  mkdir -p "$ARTIFACTS"
  for dir in "${DATA_SUBDIRS[@]}"; do
    mkdir -p "$ARTIFACTS/$dir"
    # .gitkeep keeps an otherwise empty folder in git.
    [[ -n "$(ls -A "$ARTIFACTS/$dir")" ]] || : >"$ARTIFACTS/$dir/.gitkeep"
    [[ ! -e "$ARTIFACTS/$dir/.gitkeep" ]] || paths+=("$dir/.gitkeep")
  done

  if ! git -C "$ARTIFACTS" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || [[ "$(git -C "$ARTIFACTS" rev-parse --show-toplevel)" != "$(cd "$ARTIFACTS" && pwd -P)" ]]; then
    git -C "$ARTIFACTS" init --quiet
    log "data: git init $ARTIFACTS"
  fi

  touch "$ARTIFACTS/.gitignore"
  # Do not glue our first line onto a last line without a trailing newline.
  [[ ! -s "$ARTIFACTS/.gitignore" || -z "$(tail -c 1 "$ARTIFACTS/.gitignore")" ]] \
    || printf '\n' >>"$ARTIFACTS/.gitignore"
  for line in "${DATA_GITIGNORE[@]}"; do
    grep -qxF -- "$line" "$ARTIFACTS/.gitignore" || printf '%s\n' "$line" >>"$ARTIFACTS/.gitignore"
  done

  # Commit only the paths written here, only if they changed; anything else the
  # user has staged stays staged.
  if [[ -n "$(git -C "$ARTIFACTS" status --porcelain -- "${paths[@]}")" ]]; then
    local message="Update data folder layout"
    git -C "$ARTIFACTS" rev-parse --verify --quiet HEAD >/dev/null \
      || message="Initial data folder layout"
    git -C "$ARTIFACTS" config user.email >/dev/null || ident+=(-c user.email=rpghome@localhost)
    git -C "$ARTIFACTS" config user.name >/dev/null || ident+=(-c user.name=rpghome)
    git -C "$ARTIFACTS" add -- "${paths[@]}"
    git -C "$ARTIFACTS" ${ident[@]+"${ident[@]}"} commit --quiet -m "$message" --only -- "${paths[@]}"
    log "data: committed: $message"
  fi
  log "data: ok ($ARTIFACTS)"
}

main() {
  local steps=("$@") step
  [[ ${#steps[@]} -gt 0 ]] || steps=(python deps docker data)
  for step in "${steps[@]}"; do
    case "$step" in
      python | deps | docker | data) "step_$step" ;;
      *) die "unknown step: $step (expected: python, deps, docker, data)" ;;
    esac
  done
}

main "$@"
