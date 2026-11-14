"""Placeholder engine: logs its settings and waits for SIGTERM / SIGINT.

Run by scripts/engine.sh under launchd. The engine task replaces it.
"""

import logging
import os
import signal
import sys

from rpghome.core.config import ConfigError, load_settings

log = logging.getLogger("rpghome.engine")


def main() -> int:
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    stop_signals = {signal.SIGTERM, signal.SIGINT}
    # Block first, then wait: no signal can slip in between.
    signal.pthread_sigmask(signal.SIG_BLOCK, stop_signals)
    try:
        s = load_settings()
    except ConfigError as exc:
        log.error("invalid settings: %s", exc)
        return 1
    log.info(
        "engine stub started pid=%d data=%s state=%s mqtt=%s:%d",
        os.getpid(),
        s.artifacts_dir,
        s.state_dir,
        s.mqtt_host,
        s.mqtt_port,
    )
    sig = signal.sigwait(stop_signals)
    log.info("received %s, stopping", signal.Signals(sig).name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
