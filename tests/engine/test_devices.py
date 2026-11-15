"""SoundDeviceOutput against fake streams: start retries and the audio-clock watchdog."""

import logging
import threading
import time

import numpy as np
import pytest
import sounddevice as sd

from rpghome_engine.core import Engine
from rpghome_engine.devices import AudioOutputError, NullOutput, SoundDeviceOutput


class _Status:
    output_underflow = False


class FakeStream:
    """Like sd.OutputStream: start() may fail; once started it calls back from a thread
    (unless `calls_back` is false, like CoreAudio with the speakers off)."""

    def __init__(self, callback, fail=False, calls_back=True, stop_after=None):
        self.callback, self.fail, self.calls_back = callback, fail, calls_back
        self.stop_after = stop_after  # stop calling back after this many blocks
        self.active = False
        self.closed = False
        self.latency = 0.02
        self._quit = threading.Event()

    def start(self):
        if self.fail:
            raise sd.PortAudioError("Error starting stream: Internal PortAudio error", -9986)
        self.active = True
        if self.calls_back:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        buf = np.zeros((512, 2), dtype=np.float32)
        n = 0
        while not self._quit.is_set() and (self.stop_after is None or n < self.stop_after):
            self.callback(buf, 512, None, _Status())
            n += 1
            time.sleep(512 / 48000)

    def stop(self):
        self._quit.set()
        self.active = False

    def close(self):
        self.stop()
        self.closed = True


class Factory:
    def __init__(self, failures, **kw):
        self.failures, self.kw, self.streams = failures, kw, []

    def __call__(self, callback):
        s = FakeStream(callback, fail=len(self.streams) < self.failures, **self.kw)
        self.streams.append(s)
        return s


def output(tmp_path, factory, **kw):
    return SoundDeviceOutput(
        Engine(tmp_path), "fake", stream_factory=factory, retry_delay_s=0.01, **kw
    )


def test_a_failed_start_is_retried_with_a_new_stream(tmp_path, caplog):
    factory = Factory(failures=2)
    out = output(tmp_path, factory)
    with caplog.at_level(logging.WARNING, logger="rpghome.engine"):
        out.start()
    try:
        assert len(factory.streams) == 3
        assert [s.closed for s in factory.streams] == [True, True, False]
        warnings = [r for r in caplog.records if "start failed" in r.getMessage()]
        assert [r.levelno for r in warnings] == [logging.WARNING] * 2
        assert "attempt 1 of 4" in warnings[0].getMessage()
        time.sleep(0.3)
        out.check()
        assert out.engine.mixer.clock > 0
    finally:
        out.close()


def test_start_gives_up_after_the_retries(tmp_path):
    factory = Factory(failures=99)
    out = output(tmp_path, factory)
    with pytest.raises(AudioOutputError, match="did not start after 4 attempts"):
        out.start()
    assert len(factory.streams) == 4 and all(s.closed for s in factory.streams)
    out.close()


def test_an_active_stream_that_never_calls_back_is_an_error(tmp_path):
    out = output(tmp_path, Factory(0, calls_back=False), stall_s=0.2)
    out.start()
    try:
        out.check()  # not yet
        time.sleep(0.3)
        assert out.stream.active
        with pytest.raises(AudioOutputError, match="no audio callback since the start"):
            out.check()
    finally:
        out.close()


def test_callbacks_that_stop_mid_run_are_an_error(tmp_path):
    out = output(tmp_path, Factory(0, stop_after=20), stall_s=0.2)
    out.start()
    try:
        deadline = time.monotonic() + 5
        with pytest.raises(AudioOutputError, match="audio callbacks stopped"):
            while time.monotonic() < deadline:
                out.check()
                time.sleep(0.05)
        assert out.engine.mixer.clock == 20 * 512
    finally:
        out.close()


def test_null_device_stalled_trips_the_watchdog(tmp_path):
    out = NullOutput(Engine(tmp_path), stalled=True, stall_s=0.2)
    out.start()
    try:
        time.sleep(0.3)
        with pytest.raises(AudioOutputError):
            out.check()
    finally:
        out.close()


class _Time:
    def __init__(self, current, dac):
        self.currentTime, self.outputBufferDacTime = current, dac


BLOCK_S = 512 / 48000


class BurstFakeStream(FakeStream):
    """Like PortAudio on CoreAudio at 50 ms latency: one host buffer of 4 blocks every
    4 block periods, its 4 callbacks back to back. The DAC time advances one block per
    callback (advance=True) or repeats within the burst. `lose` drops that many blocks
    from the DAC timeline before burst 5; `late` gives burst 7 only 1 ms headroom."""

    def __init__(self, callback, advance=True, lose=0, late=False):
        super().__init__(callback)
        self.advance, self.lose, self.late = advance, lose, late

    def _run(self):
        buf = np.zeros((512, 2), dtype=np.float32)
        dac0 = 1000.0
        for burst in range(12):
            if self._quit.is_set():
                return
            if burst == 5:
                dac0 += self.lose * BLOCK_S
            now = dac0 - (0.001 if self.late and burst == 7 else 0.03)
            for k in range(4):
                dac = dac0 + (k * BLOCK_S if self.advance else 0.0)
                self.callback(buf, 512, _Time(now, dac), _Status())
            dac0 += 4 * BLOCK_S
            time.sleep(4 * BLOCK_S)


def run_stream(engine, make, blocks=48):
    out = SoundDeviceOutput(engine, "fake", stream_factory=make, retry_delay_s=0.01)
    out.start()
    try:
        deadline = time.monotonic() + 5
        while engine.mixer.clock < blocks * 512 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        out.close()
    return engine.status()


@pytest.mark.parametrize("advance", [True, False])
def test_bursts_of_callbacks_are_not_gaps(tmp_path, advance):
    s = run_stream(Engine(tmp_path), lambda cb: BurstFakeStream(cb, advance=advance))
    assert s["seconds_rendered"] == pytest.approx(48 * BLOCK_S)
    assert s["gaps"] == 0 and s["late_callbacks"] == 0
    assert s["max_gap_ms"] < 0.01
    assert s["min_headroom_ms"] == pytest.approx(30.0, abs=0.01)


@pytest.mark.parametrize("advance", [True, False])
def test_a_lost_block_is_a_gap(tmp_path, advance):
    s = run_stream(Engine(tmp_path), lambda cb: BurstFakeStream(cb, advance=advance, lose=1))
    assert s["gaps"] == 1 and s["late_callbacks"] == 0
    assert s["max_gap_ms"] == pytest.approx(BLOCK_S * 1e3, abs=0.01)


def test_low_headroom_is_a_late_callback(tmp_path):
    s = run_stream(Engine(tmp_path), lambda cb: BurstFakeStream(cb, late=True))
    assert s["late_callbacks"] == 1  # the first callback of burst 7: 1 ms before the DAC
    assert s["gaps"] == 0
    assert s["min_headroom_ms"] == pytest.approx(1.0, abs=0.01)
    assert s["underruns"] == 0  # what PortAudio reported: nothing


def test_slow_callbacks_are_counted_but_are_not_late(tmp_path):
    engine = Engine(tmp_path)
    render = engine.mixer.render
    calls = {"n": 0}

    def slow_render(out):
        calls["n"] += 1
        if calls["n"] in (5, 6, 7):
            time.sleep(0.02)  # longer than the 10.7 ms block period
        render(out)

    engine.mixer.render = slow_render
    s = run_stream(engine, lambda cb: BurstFakeStream(cb))
    assert s["slow_callbacks"] == 3
    assert s["late_callbacks"] == 0 and s["gaps"] == 0


def test_latency_is_passed_to_the_output_stream(tmp_path, monkeypatch):
    seen = {}

    class Stream(FakeStream):
        def __init__(self, **kw):
            seen.update(kw)
            super().__init__(kw["callback"])

    monkeypatch.setattr(sd, "OutputStream", Stream)
    monkeypatch.setattr(sd, "query_devices", lambda *a, **k: {"name": "Fake speakers"})
    out = SoundDeviceOutput(Engine(tmp_path), None, 0.08)
    out.start()
    out.close()
    assert seen["latency"] == 0.08 and seen["blocksize"] == 512 and seen["samplerate"] == 48000
    assert out.description == "Fake speakers"


class FakeSd:
    """Just enough of sounddevice for _find_output."""

    PortAudioError = sd.PortAudioError

    def __init__(self, devices):
        self.devices = devices

    def query_devices(self, device=None, kind=None):
        if device is None and kind is None:
            return self.devices
        if isinstance(device, int):
            if not 0 <= device < len(self.devices):
                raise sd.PortAudioError(f"Error querying device {device}")
            d = self.devices[device]
        else:
            found = [d for d in self.devices if device.lower() in d["name"].lower()]
            found = [d for d in found if d["max_output_channels"]]
            if not found:
                raise ValueError(f"No output device matching {device!r}")
            d = found[0]
        if not d["max_output_channels"]:
            raise ValueError(f"Not an output device: {d['name']!r}")
        return d


def test_named_devices_missing_input_only_or_present():
    from rpghome_engine.devices import DeviceNotFoundError, _find_output

    fake = FakeSd(
        [
            {"name": "Mikrofon (MacBook Pro)", "max_output_channels": 0},
            {"name": "Speakers (MacBook Pro)", "max_output_channels": 2},
        ]
    )
    assert _find_output(fake, "speakers")["name"] == "Speakers (MacBook Pro)"
    with pytest.raises(AudioOutputError, match=r"not found .*output devices now: 1: Speakers"):
        _find_output(fake, "USB Interface")  # unplugged (or a typo): retry
    with pytest.raises(AudioOutputError, match="not found"):
        _find_output(fake, 7)
    with pytest.raises(DeviceNotFoundError, match="has no outputs"):
        _find_output(fake, "Mikrofon")  # exists, but it is a microphone
    with pytest.raises(DeviceNotFoundError, match="has no outputs"):
        _find_output(fake, 0)
