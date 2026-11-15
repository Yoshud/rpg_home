import logging
import logging.handlers
import sys

import pytest

from rpghome.core.config import ConfigError
from rpghome.core.logs import LOG_BACKUPS, LOG_MAX_BYTES, LOGGER, add_stderr, setup_logging


@pytest.fixture(autouse=True)
def restore_logger():
    logger = logging.getLogger(LOGGER)
    handlers, level = list(logger.handlers), logger.level
    yield logger
    for h in list(logger.handlers):
        logger.removeHandler(h)
        if h not in handlers:
            h.close()
    for h in handlers:
        logger.addHandler(h)
    logger.setLevel(level)


def _files(logger):
    return [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]


def _stderr(logger):
    return [h for h in logger.handlers if getattr(h, "stream", None) is sys.stderr]


def test_logs_to_the_named_file_rotated(tmp_path, restore_logger):
    setup_logging(tmp_path / "logs", "app.log", to_stderr=False)
    logging.getLogger("rpghome.api").info("hello")
    (fh,) = _files(restore_logger)
    assert (fh.maxBytes, fh.backupCount) == (LOG_MAX_BYTES, LOG_BACKUPS) == (5 * 1024**2, 2)
    assert not _stderr(restore_logger)
    text = (tmp_path / "logs" / "app.log").read_text()
    assert text.endswith(" INFO rpghome.api: hello\n")


def test_a_second_call_replaces_the_handlers(tmp_path, restore_logger):
    setup_logging(tmp_path, "a.log", to_stderr=True)
    setup_logging(tmp_path, "b.log", to_stderr=True)
    add_stderr()
    (fh,) = _files(restore_logger)
    assert fh.baseFilename.endswith("b.log")
    assert len(_stderr(restore_logger)) == 1


def test_no_log_folder_logs_to_stderr(restore_logger):
    setup_logging(None, "engine.log", to_stderr=False)
    assert not _files(restore_logger) and len(_stderr(restore_logger)) == 1


def test_unwritable_log_folder_is_a_config_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(ConfigError, match=r"cannot write the engine log in .*file"):
        setup_logging(blocker / "logs", "engine.log", to_stderr=False)
