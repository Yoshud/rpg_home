"""Memory pinning (mlock) against a fake libc (a real mlock may be refused in a sandbox)."""

import gc
import logging
import threading
import time

import numpy as np

from rpghome.models.scene import Bed, ClipRef, Scene
from rpghome_engine.core import Engine
from rpghome_engine.devices import SoundDeviceOutput
from rpghome_engine.memory import MCL_CURRENT, MCL_FUTURE, MemoryPin
from rpghome_engine.mixer import Mixer

from .conftest import Rig, write_tone
from .test_devices import BurstFakeStream, _Status, _Time

ENOSYS, EAGAIN = 78, 35
PAGE = 16384


class FakeLibc:
    def __init__(self, mlockall_ok=False, mlock_ok=True):
        self.mlockall_ok, self.mlock_ok = mlockall_ok, mlock_ok
        self.calls = []
        self.errno = 0
        self.locked: set[int] = set()  # page indices

    def mlockall(self, flags):
        self.calls.append(("mlockall", flags))
        self.errno = ENOSYS
        return 0 if self.mlockall_ok else -1

    def mlock(self, addr, length):
        self.calls.append(("mlock", addr, length))
        assert addr % PAGE == 0 and length % PAGE == 0
        if not self.mlock_ok:
            self.errno = EAGAIN
            return -1
        pages = set(range(addr // PAGE, (addr + length) // PAGE))
        assert not pages & self.locked  # never locks a page twice
        self.locked |= pages
        return 0

    def munlock(self, addr, length):
        self.calls.append(("munlock", addr, length))
        pages = set(range(addr // PAGE, (addr + length) // PAGE))
        assert pages <= self.locked
        self.locked -= pages
        return 0


def memory_pin(libc):
    return MemoryPin(libc=libc, get_errno=lambda: libc.errno, page_size=PAGE)


def covered(libc, arr):
    a = arr.ctypes.data
    return set(range(a // PAGE, (a + arr.nbytes - 1) // PAGE + 1)) <= libc.locked


def test_without_mlockall_clips_and_buffers_are_pinned_and_unpinned(data, caplog):
    libc = FakeLibc()
    m = memory_pin(libc)
    with caplog.at_level(logging.INFO, logger="rpghome.engine"):
        assert m.pin_all() is False
    assert libc.calls[0] == ("mlockall", MCL_CURRENT | MCL_FUTURE)
    assert "mlockall not available" in caplog.text
    rig = Rig(data)
    rig.engine = type(rig.engine)(data, seed=1, memory=m)
    from rpghome_engine.devices import OfflineDevice

    rig.device = OfflineDevice(rig.engine)
    mixer = rig.engine.mixer
    for buf in (mixer._mix, mixer._tmp, mixer.limiter._xw, mixer.limiter._held):
        assert covered(libc, buf)
    before = m.buffers
    write_tone(data / "a.wav", 300, 10)
    write_tone(data / "b.wav", 500, 10)
    scene = Scene(
        name="s",
        bed=Bed(
            clips=(ClipRef(path="a.wav"), ClipRef(path="b.wav")), hold_s=(1, 1), crossfade_s=0.5
        ),
    )
    rig.engine.scene(scene, 0)
    rig.render(0.01)  # the control cycle applies the command
    run = rig.engine._current
    clips = run.bed.clips
    assert all(covered(libc, c.cycle) and covered(libc, c.head) for c in clips)
    events = [d for _, d in mixer.scenes[run.sid].layers["bed"].events]
    assert events and all(covered(libc, d) for d in events)  # what the callback reads
    assert m.buffers == before + 4 + len(events)
    del events
    assert m.summary()["mode"] == "mlock" and m.summary()["locked_mb"] > 0.5
    del clips, run
    rig.render(0.5)
    rig.engine.stop(0.1)
    rig.render(1)  # the run is dropped, its clips freed
    gc.collect()
    assert m.buffers == before
    assert any(c[0] == "munlock" for c in libc.calls)
    for buf in (mixer._mix, mixer.limiter._xw):  # still in use, still pinned
        assert covered(libc, buf)


def test_shared_pages_stay_pinned_until_the_last_buffer_goes():
    libc = FakeLibc()
    m = memory_pin(libc)
    a = m.pin(np.zeros(1000, np.float32))  # small arrays share pages
    b = m.pin(np.zeros(1000, np.float32))
    shared = covered(libc, a) and covered(libc, b)
    assert shared
    del a
    gc.collect()
    assert covered(libc, b)
    del b
    gc.collect()
    assert libc.locked == set() and m.buffers == 0


def test_with_mlockall_nothing_is_pinned_one_by_one():
    libc = FakeLibc(mlockall_ok=True)
    m = memory_pin(libc)
    assert m.pin_all() is True
    m.pin(np.zeros(100000, np.float32))
    assert [c[0] for c in libc.calls] == ["mlockall"]
    assert m.summary() == {"mode": "mlockall"}


def test_a_refused_mlock_is_one_warning_not_an_error(data, caplog):
    libc = FakeLibc(mlock_ok=False)
    m = memory_pin(libc)
    with caplog.at_level(logging.WARNING, logger="rpghome.engine"):
        rig = Rig(data)
        rig.engine = type(rig.engine)(data, seed=1, memory=m)
        write_tone(data / "a.wav", 300, 10)
        rig.engine.scene(Scene(name="s", bed=Bed(clips=(ClipRef(path="a.wav"),))), 0)
    warnings = [r for r in caplog.records if "mlock of" in r.getMessage()]
    assert len(warnings) == 1 and "Resource temporarily unavailable" in warnings[0].getMessage()
    assert m.summary()["mode"] == "failed" and libc.locked == set()
    assert sum(c[0] == "mlock" for c in libc.calls) == 1  # stopped trying


def test_the_audio_thread_never_pins_or_unpins(data):
    """Clips, scratch and planned buffers are pinned, and played buffers unpinned, only
    in other threads; the callback thread (a fake stream's) never calls mlock or munlock."""

    class ThreadLibc(FakeLibc):
        def __init__(self):
            super().__init__()
            self.threads = set()

        def mlock(self, addr, length):
            self.threads.add(threading.get_ident())
            return super().mlock(addr, length)

        def munlock(self, addr, length):
            self.threads.add(threading.get_ident())
            return super().munlock(addr, length)

    libc = ThreadLibc()
    m = memory_pin(libc)
    engine = Engine(data, seed=1, memory=m, horizon_s=0.3, control_period_s=0.05)
    write_tone(data / "a.wav", 300, 10)
    write_tone(data / "b.wav", 500, 10)
    bed = Bed(
        clips=(ClipRef(path="a.wav"), ClipRef(path="b.wav")), hold_s=(0.2, 0.2), crossfade_s=0.1
    )
    engine.scene(Scene(name="s", bed=bed, gain_db=0), 0)

    class LongBurst(BurstFakeStream):
        def _run(self):
            buf = np.zeros((512, 2), dtype=np.float32)
            dac = 1000.0
            while not self._quit.is_set():
                for _ in range(4):
                    self.callback(buf, 512, _Time(dac - 0.03, dac), _Status())
                    dac += 512 / 48000
                time.sleep(4 * 512 / 48000)

    out = SoundDeviceOutput(engine, "fake", stream_factory=LongBurst)
    out.start()
    try:
        time.sleep(1.0)  # clip changes every 0.2 s: many buffers planned, played, dropped
        engine.stop(0.1)
        time.sleep(0.5)
    finally:
        out.close()
    assert m.audio_thread is not None
    assert m.audio_thread not in libc.threads
    assert any(c[0] == "munlock" for c in libc.calls)
    assert m.deferred_releases == 0


def test_large_blocks_are_split_not_regrown(data):
    libc = FakeLibc()
    m = memory_pin(libc)
    mixer = Mixer(48000, max_block=512, pin=m.pin)
    buffers = (mixer._mix, mixer.limiter._xw)
    calls = len(libc.calls)
    out = np.zeros((2000, 2), np.float32)
    mixer.render(out)
    assert mixer.clock == 2000
    assert mixer._mix is buffers[0] and mixer.limiter._xw is buffers[1]
    assert len(libc.calls) == calls  # no mlock / munlock while rendering
