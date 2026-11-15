"""What pulls blocks out of the engine: the sound card, a real-time fake, or an offline
renderer. All of them call Engine.render_block the same way the device callback does."""

import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from rpghome_engine.clips import EngineError
from rpghome_engine.core import CHANNELS, Engine

log = logging.getLogger("rpghome.engine")


class ControlThread:
    """Runs engine.control_cycle() every control period, and at once after a command."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="engine-control", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.engine.control_cycle()
                self.engine.wait_for_command(self.engine.control_period_s)
        except BaseException as exc:
            log.exception("control thread failed")
            self.error = exc

    def close(self) -> None:
        self._stop.set()
        self.engine.wake()
        self._thread.join(timeout=5)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


class AudioOutputError(EngineError):
    """The output does not play: the stream would not start, stopped, or never calls back."""


class DeviceNotFoundError(EngineError):
    """RPGHOME_AUDIO_DEVICE names no output device (a configuration error)."""


def output_devices(sd) -> list[str]:
    return [
        f"{i}: {d['name']}" for i, d in enumerate(sd.query_devices()) if d["max_output_channels"]
    ]


def _find_output(sd, device: str | int | None) -> dict:
    """The device to play on.

    - It matches a device that has no outputs (a microphone): a configuration error,
      plugging anything in will not fix it (DeviceNotFoundError, exit 0).
    - It matches nothing: unplugged now, or a typo; the two look the same (PortAudio
      lists only present devices). AudioOutputError, exit 1: launchd retries every 5 s,
      so the engine starts when the device appears, and a typo repeats the same clear
      line (with the devices present) every 5 s.
    """
    try:
        return sd.query_devices(device, kind="output")
    except (ValueError, sd.PortAudioError) as exc:
        if device is None:  # no default output at all: a device may appear, retry later
            raise AudioOutputError(f"no default output device: {exc}") from None
        devices = "; ".join(output_devices(sd)) or "none"
        if _input_only(sd, device):
            raise DeviceNotFoundError(
                f"RPGHOME_AUDIO_DEVICE={device!r} has no outputs; output devices: {devices}"
            ) from None
        raise AudioOutputError(
            f"output device RPGHOME_AUDIO_DEVICE={device!r} not found (unplugged, or a typo?); "
            f"output devices now: {devices}"
        ) from None


def _input_only(sd, device: str | int) -> bool:
    try:
        found = sd.query_devices()
    except Exception:
        return False
    if isinstance(device, int):
        return 0 <= device < len(found) and not found[device]["max_output_channels"]
    matches = [d for d in found if device.lower() in d["name"].lower()]
    return bool(matches) and not any(d["max_output_channels"] for d in matches)


class Watchdog:
    """Fails when the audio clock (samples rendered) stops moving for `stall_s`, counted
    from start() and from every later progress. An active stream that never calls back
    (CoreAudio does this in clamshell mode or when a device disappears) is silence with
    zero counters; this turns it into an error."""

    def __init__(self, engine: Engine, stall_s: float = 2.0):
        self.engine = engine
        self.stall_s = stall_s
        self._clock = -1
        self._since = 0.0

    def start(self) -> None:
        self._clock = self.engine.mixer.clock
        self._since = time.monotonic()

    def check(self) -> None:
        clock, now = self.engine.mixer.clock, time.monotonic()
        if clock != self._clock:
            self._clock, self._since = clock, now
        elif now - self._since >= self.stall_s:
            what = "no audio callback since the start" if clock == 0 else "audio callbacks stopped"
            raise AudioOutputError(
                f"{what}: the audio clock has not moved for {now - self._since:.1f} s "
                f"(at {clock / self.engine.rate:.1f} s rendered); is the output device off "
                "(lid closed?) or gone?"
            )


class _RealTimeOutput:
    """Shared by the real-time outputs: control thread, watchdog and check()."""

    def __init__(self, engine: Engine, stall_s: float):
        self.engine = engine
        self.control = ControlThread(engine)
        self.watchdog = Watchdog(engine, stall_s)

    def check(self) -> None:
        """Raise AudioOutputError when the output does not play (call about once a second)."""
        if not self.control.alive:
            raise AudioOutputError(f"control thread stopped: {self.control.error!r}")
        self._check_stream()
        self.watchdog.check()

    def _check_stream(self) -> None:
        pass


class SoundDeviceOutput(_RealTimeOutput):
    """The default (or a named) output device through sounddevice / CoreAudio.

    The first start can fail with a PortAudio error (seen in clamshell mode); start()
    closes the stream, opens a new one and tries again, `retries` times, then raises
    AudioOutputError, so the process exits non-zero and launchd tries again later.
    """

    def __init__(
        self,
        engine: Engine,
        device: str | int | None = None,
        latency: float | str = 0.05,
        *,
        retries: int = 3,
        retry_delay_s: float = 0.5,
        stall_s: float = 2.0,
        stream_factory=None,
    ):
        import sounddevice as sd  # imported here: offline use needs no PortAudio

        super().__init__(engine, stall_s)
        self._sd = sd
        self.latency = latency
        self.retries = retries
        self.retry_delay_s = retry_delay_s
        self.stream = None
        if stream_factory is None:
            self.description = _find_output(sd, device)["name"]
            self._factory = lambda: sd.OutputStream(
                samplerate=engine.rate,
                blocksize=engine.blocksize,
                channels=CHANNELS,
                dtype="float32",
                device=device,
                latency=latency,
                callback=self._callback,
            )
        else:  # tests: a fake stream, called back through self._callback
            self.description = f"fake stream {device!r}"
            self._factory = lambda: stream_factory(self._callback)

    def _callback(self, outdata, frames, time_info, status) -> None:
        if status.output_underflow:
            self.engine.underruns += 1
        self.engine.render_block_rt(outdata, time_info)

    def start(self) -> None:
        sys.setswitchinterval(0.001)  # the callback waits less for the GIL
        self.engine.control_cycle()  # material is ready before the first callback
        self.control.start()
        attempts = 1 + self.retries
        for attempt in range(1, attempts + 1):
            try:
                self.stream = self._factory()
                self.stream.start()
                break
            except self._sd.PortAudioError as exc:
                log.warning(
                    "output stream start failed (attempt %d of %d): %s", attempt, attempts, exc
                )
                self._close_stream()
                if attempt == attempts:
                    raise AudioOutputError(
                        f"output stream did not start after {attempts} attempts: {exc}"
                    ) from None
                time.sleep(self.retry_delay_s)
        log.info(
            "output stream started (attempt %d, latency %.1f ms, requested %s)",
            attempt,
            self.stream.latency * 1e3,
            f"{self.latency * 1e3:g} ms" if isinstance(self.latency, float) else self.latency,
        )
        self.watchdog.start()

    def _check_stream(self) -> None:
        if self.stream is None or not self.stream.active:
            raise AudioOutputError("output stream stopped")

    def _close_stream(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.close()
        except Exception as exc:
            log.warning("closing the output stream failed: %s", exc)
        self.stream = None

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
            except Exception as exc:
                log.warning("stopping the output stream failed: %s", exc)
        self._close_stream()
        self.control.close()


class _TimeInfo:
    __slots__ = ("currentTime", "outputBufferDacTime")


class NullOutput(_RealTimeOutput):
    """A real-time fake device: a thread pulls blocks at the device rate and drops them.
    Used where there is no sound card (tests, sandboxes): RPGHOME_AUDIO_DEVICE=null.
    With stalled=True it never pulls a block, like a stream that never calls back
    (RPGHOME_AUDIO_DEVICE=null-stalled, to test the watchdog)."""

    LATENCY_S = 0.02

    def __init__(self, engine: Engine, *, stalled: bool = False, stall_s: float = 2.0):
        super().__init__(engine, stall_s)
        self.stalled = stalled
        self.description = "null device (real time, no sound)" + (", stalled" if stalled else "")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="engine-null-device", daemon=True)

    def _run(self) -> None:
        e = self.engine
        buf = np.zeros((e.blocksize, CHANNELS), dtype=np.float32)
        period = e.blocksize / e.rate
        next_t = time.perf_counter()
        if self.stalled:
            self._stop.wait()
        when = _TimeInfo()  # reused: a DAC timeline like PortAudio's, 20 ms ahead
        while not self._stop.is_set():
            when.currentTime = time.perf_counter()
            when.outputBufferDacTime = next_t + self.LATENCY_S
            e.render_block_rt(buf, when)
            next_t += period
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.1:
                e.underruns += 1
                next_t = time.perf_counter()

    def start(self) -> None:
        sys.setswitchinterval(0.001)
        self.engine.control_cycle()
        self.control.start()
        self._thread.start()
        self.watchdog.start()

    def _check_stream(self) -> None:
        if not self._thread.is_alive():
            raise AudioOutputError("null device thread stopped")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.control.close()


class OfflineDevice:
    """No device and no threads: pulls blocks as fast as possible and runs the control
    cycle every control period of rendered time (and right away when a command waits),
    so a render depends only on the seed and the commands."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self._period = round(engine.control_period_s * engine.rate)
        self._next_control = engine.mixer.clock

    def blocks(self, frames: int):
        """Yield rendered blocks, `frames` samples in total."""
        e = self.engine
        buf = np.zeros((e.blocksize, CHANNELS), dtype=np.float32)
        left = frames
        while left > 0:
            if e.mixer.clock >= self._next_control or e.has_commands:
                e.control_cycle()
                self._next_control = e.mixer.clock + self._period
            n = min(left, e.blocksize)
            e.render_block(buf[:n])
            left -= n
            yield buf[:n]

    def render(self, frames: int) -> np.ndarray:
        out = np.empty((frames, CHANNELS), dtype=np.float32)
        pos = 0
        for block in self.blocks(frames):
            out[pos : pos + len(block)] = block
            pos += len(block)
        return out

    def render_to_file(self, path: Path, frames: int, should_stop=None) -> None:
        """Write `frames` samples to a 24-bit WAV, block by block (constant memory).
        should_stop() is polled about every 10 s of audio; a true result ends early."""
        with sf.SoundFile(
            str(path), "w", samplerate=self.engine.rate, channels=CHANNELS, subtype="PCM_24"
        ) as f:
            for i, block in enumerate(self.blocks(frames)):
                f.write(block)
                if should_stop is not None and i % 1000 == 999 and should_stop():
                    log.info("render stopped early at %d samples", self.engine.mixer.clock)
                    break
