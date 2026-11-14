import os
import signal
import subprocess
import sys

import pytest


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_stub_logs_and_stops_on_signal(env, sig):
    proc = subprocess.Popen(
        [sys.executable, "-m", "rpghome.engine"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "RPGHOME_MQTT_PORT": "1884"},
    )
    try:
        first = proc.stdout.readline()
        assert f"pid={proc.pid}" in first
        assert str(env.resolve() / "artifacts") in first
        assert "mqtt=127.0.0.1:1884" in first
        proc.send_signal(sig)
        out, _ = proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 0
    assert f"received {sig.name}, stopping" in out


def test_stub_rejects_invalid_settings(env):
    result = subprocess.run(
        [sys.executable, "-m", "rpghome.engine"],
        env={**os.environ, "RPGHOME_MQTT_PORT": "nope"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "invalid settings" in result.stdout
