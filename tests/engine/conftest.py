"""Synthetic clips with known content, and measuring helpers for offline renders."""

import numpy as np
import pytest
import soundfile as sf

from rpghome_engine.core import LEAD_BLOCKS, Engine
from rpghome_engine.devices import OfflineDevice

RATE = 48000
LEAD = LEAD_BLOCKS * 512  # a command takes effect this many samples after the clock


def write_tone(path, freq, seconds, amp=0.25, rate=RATE):
    t = np.arange(round(seconds * rate)) / rate
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32), rate, "FLOAT")


def write_noise(path, seconds, amp=0.25, seed=0, rate=RATE):
    rng = np.random.default_rng(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    x = rng.uniform(-amp, amp, round(seconds * rate)).astype(np.float32)
    sf.write(str(path), x, rate, "FLOAT")


def amp_at(x, freq, t_s, win_s=0.1, rate=RATE):
    """Amplitude of a sine at freq in channel 0, in a window centred at t_s."""
    n = round(win_s * rate)
    a = round(t_s * rate) - n // 2
    seg = x[a : a + n, 0].astype(np.float64)
    t = np.arange(a, a + n) / rate
    return 2 * abs(np.sum(seg * np.exp(-2j * np.pi * freq * t))) / n


class Rig:
    """An engine with an offline device: render(seconds) appends to self.out."""

    def __init__(self, data_dir, seed=1):
        self.engine = Engine(data_dir, seed=seed)
        self.device = OfflineDevice(self.engine)
        self.parts = []

    def render(self, seconds):
        self.parts.append(self.device.render(round(seconds * RATE)))

    @property
    def now(self):
        """Engine clock in seconds."""
        return self.engine.mixer.clock / RATE

    @property
    def out(self):
        return np.concatenate(self.parts)


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def rig(data):
    return Rig(data)
