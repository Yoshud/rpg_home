"""Logging setup shared by the processes (the engine, later the app server): the "rpghome"
logger, which every module's logger (rpghome.engine, ...) sits under, writes to a rotated
file in the log folder and, when asked, to stderr."""

import logging
import logging.handlers
import sys
from pathlib import Path

from rpghome.core.config import ConfigError

LOGGER = "rpghome"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 2  # the log + 2 rotated files


def setup_logging(log_dir: Path | None, file_name: str, to_stderr: bool) -> None:
    """Log INFO and above to log_dir/file_name (5 MB, 3 files) and, with to_stderr or no
    log_dir, to stderr. Replaces the handlers of an earlier call. Raises ConfigError
    when the log file cannot be written."""
    logger = logging.getLogger(LOGGER)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(LOG_FORMAT)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / file_name, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS
            )
        except OSError as exc:
            what = Path(file_name).stem
            raise ConfigError(f"cannot write the {what} log in {log_dir}: {exc}") from None
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    if to_stderr or log_dir is None:
        add_stderr()


def add_stderr() -> None:
    """Also log to stderr (once)."""
    logger = logging.getLogger(LOGGER)
    if any(getattr(h, "stream", None) is sys.stderr for h in logger.handlers):
        return
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(sh)
