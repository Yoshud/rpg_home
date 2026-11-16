"""One engine process at a time: an exclusive `fcntl.flock` on `<state>/engine.lock`.

The lock lives in the state folder, so running `scripts/engine.sh` by hand next to the
launchd job says "another engine is running" and exits instead of playing over it. The file
stays in place: deleting a lock file races with a process that is about to take it.
"""

import contextlib
import fcntl
import os
from pathlib import Path

LOCK_NAME = "engine.lock"


class EngineLock:
    """`acquire()` never waits: it returns False when another engine holds the lock."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 - held: the lock lives in this fd
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        except OSError:
            fh.close()
            raise
        with contextlib.suppress(OSError):  # the pid line only helps a human looking at it
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            self._fh.close()  # closing the fd releases the flock
        self._fh = None
