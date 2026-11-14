#!/bin/bash
# Runs the audio engine in the foreground. launchd starts it (see start.sh);
# it can also be run by hand to debug. Stop it with Ctrl-C / SIGTERM.
#
# The engine is exec'd, so it keeps this script's PID: launchd tracks that PID
# and its SIGTERM reaches the engine directly. Never background the engine here.
set -euo pipefail

# launchd starts jobs with only /usr/bin:/bin:/usr/sbin:/sbin.
PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
export PATH

LOG_TAG=engine
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x $PYTHON ]] || die "$PYTHON not found; run scripts/setup.sh deps"

# Keep the Mac from idle-sleeping while the engine runs (-w: until this PID exits).
caffeinate -i -w $$ &

cd "$REPO_ROOT"
exec "$PYTHON" -m rpghome.engine
