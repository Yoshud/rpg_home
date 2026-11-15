"""Pins the engine's buffers in RAM with mlock, so macOS neither compresses nor swaps
out the memory the audio callback reads. This is not mutual exclusion: nothing here is a
thread lock, and the audio thread takes no mutex at all.

Why: under memory pressure macOS compresses or pages out memory that has not been
touched for a while. A page of a clip buffer that was compressed when the callback reads
it is a page fault in the callback, which makes the callback late and the output crackle
(a large local model loaded next to the engine sent the callback from under 4 ms to
29 ms).

First mlockall(MCL_CURRENT | MCL_FUTURE). macOS declares it in <sys/mman.h> but does not
implement it (ENOSYS), so then every clip buffer, planned buffer, mixer scratch buffer
and limiter buffer is pinned (mlock) when it is allocated and unpinned (munlock) when it
is freed. macOS limits wired memory by RLIMIT_MEMLOCK and the vm.user_wire_limit sysctl.
mlock works on whole pages; two buffers can share a page, so pages are
reference-counted and a page is unpinned only when no pinned buffer uses it. Every
failure is a warning, never fatal; after the first failed mlock MemoryPin stops trying
(one warning, not one per buffer).

The audio thread never calls mlock or munlock and never takes the page table's guard (a
mutex used only by the control and loading threads): it allocates nothing (the mixer and
limiter buffers are sized up front), and the buffers it lets go are handed to the control
thread (Mixer.graveyard), so their finalizers, which munlock, run there. Should a
finalizer still run in the audio thread, it only queues the pages for collect() in the
control thread.
"""

import collections
import ctypes
import ctypes.util
import logging
import mmap
import os
import threading
import weakref

import numpy as np

log = logging.getLogger("rpghome.engine")

MCL_CURRENT = 0x1
MCL_FUTURE = 0x2


class NoPin:
    """Pins nothing: offline renders and tests."""

    all_pinned = False
    audio_thread = None

    def pin(self, arr: np.ndarray) -> np.ndarray:
        return arr

    def collect(self) -> None:
        pass

    def summary(self) -> dict:
        return {"mode": "off"}


class MemoryPin:
    def __init__(self, libc=None, get_errno=None, page_size: int | None = None):
        if libc is None:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            for name in ("mlock", "munlock"):
                getattr(libc, name).argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                getattr(libc, name).restype = ctypes.c_int
            libc.mlockall.argtypes = [ctypes.c_int]
            libc.mlockall.restype = ctypes.c_int
        self._libc = libc
        self._errno = get_errno or ctypes.get_errno
        self.page = page_size or mmap.PAGESIZE
        self.all_pinned = False
        self.enabled = True  # False after a failed mlock
        self.error: str | None = None
        self._pages: dict[int, int] = {}  # page index -> number of pinned buffers on it
        self._guard = threading.Lock()
        self.buffers = 0  # buffers currently pinned
        self.audio_thread: int | None = None  # ident of the audio thread, set by the engine
        self._deferred: collections.deque = collections.deque()
        self.deferred_releases = 0  # finalizers that ran in the audio thread (should be 0)

    @property
    def pinned_bytes(self) -> int:
        return len(self._pages) * self.page

    def _strerror(self) -> str:
        e = self._errno()
        return f"{os.strerror(e)} (errno {e})"

    def pin_all(self) -> bool:
        if self._libc.mlockall(MCL_CURRENT | MCL_FUTURE) == 0:
            self.all_pinned = True
            log.info("memory: mlockall locked all current and future memory")
            return True
        log.info(
            "memory: mlockall not available (%s); locking buffers one by one", self._strerror()
        )
        return False

    def pin(self, arr: np.ndarray) -> np.ndarray:
        """mlock the pages of arr until arr is freed; returns arr."""
        if self.all_pinned or not self.enabled or arr.nbytes == 0:
            return arr
        addr = arr.ctypes.data
        p0, p1 = addr // self.page, (addr + arr.nbytes - 1) // self.page + 1
        with self._guard:
            done = []
            for a, b in _runs(p for p in range(p0, p1) if p not in self._pages):
                if self._libc.mlock(a * self.page, (b - a) * self.page) != 0:
                    self.error = self._strerror()
                    for da, db in done:
                        self._libc.munlock(da * self.page, (db - da) * self.page)
                    self.enabled = False
                    log.warning(
                        "memory: mlock of %d bytes failed (%s); the engine's memory is not "
                        "pinned and may be paged out under memory pressure (%d buffers, "
                        "%.1f MB stay pinned)",
                        (b - a) * self.page,
                        self.error,
                        self.buffers,
                        self.pinned_bytes / 1e6,
                    )
                    return arr
                done.append((a, b))
            for p in range(p0, p1):
                self._pages[p] = self._pages.get(p, 0) + 1
            self.buffers += 1
        weakref.finalize(arr, self._release, p0, p1)
        return arr

    def _release(self, p0: int, p1: int) -> None:
        if self.audio_thread is not None and threading.get_ident() == self.audio_thread:
            self.deferred_releases += 1
            self._deferred.append((p0, p1))  # no mutex, no syscall here
            return
        self._release_now(p0, p1)

    def collect(self) -> None:
        """Releases queued by the audio thread (control thread, every cycle)."""
        while self._deferred:
            self._release_now(*self._deferred.popleft())

    def _release_now(self, p0: int, p1: int) -> None:
        with self._guard:
            free = []
            for p in range(p0, p1):
                n = self._pages[p] - 1
                if n:
                    self._pages[p] = n
                else:
                    del self._pages[p]
                    free.append(p)
            for a, b in _runs(free):
                self._libc.munlock(a * self.page, (b - a) * self.page)
            self.buffers -= 1

    def summary(self) -> dict:
        if self.all_pinned:
            return {"mode": "mlockall"}
        return {
            "mode": "mlock" if self.enabled else "failed",
            "buffers": self.buffers,
            "locked_mb": round(self.pinned_bytes / 1e6, 2),
            "error": self.error,
            "deferred_releases": self.deferred_releases,
        }


def _runs(pages):
    """Contiguous [start, end) runs of increasing page indices."""
    start = prev = None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            yield start, prev + 1
            start = prev = p
    if start is not None:
        yield start, prev + 1
