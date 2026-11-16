"""One engine process at a time: the exclusive flock on <state>/engine.lock."""

import os
import signal
import subprocess
import sys
import time

from rpghome_engine.instance_lock import LOCK_NAME, EngineLock


def test_a_second_holder_is_refused_and_gets_it_after_a_release(tmp_path):
    path = tmp_path / "state" / LOCK_NAME
    first, second = EngineLock(path), EngineLock(path)
    assert first.acquire() is True
    assert path.is_file()
    assert second.acquire() is False  # never waits
    assert first.held and not second.held
    first.release()
    assert second.acquire() is True
    second.release()


def test_the_same_holder_keeping_it_is_not_a_deadlock(tmp_path):
    lock = EngineLock(tmp_path / LOCK_NAME)
    assert lock.acquire() and lock.acquire()  # idempotent
    lock.release()
    lock.release()


def engine_process(env):
    return subprocess.Popen(
        [sys.executable, "-m", "rpghome_engine"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "RPGHOME_AUDIO_DEVICE": "null", "RPGHOME_MQTT_PORT": "1884"},
    )


def engine_log(env):
    path = env / "state" / "logs" / "engine.log"
    return path.read_text() if path.exists() else ""


def wait_until(what, predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_second_engine_process_says_another_engine_is_running_and_exits_0(env):
    first = engine_process(env)
    try:
        wait_until(
            "the first engine to start",
            lambda: f"engine started pid={first.pid}" in engine_log(env),
        )
        second = subprocess.run(
            [sys.executable, "-m", "rpghome_engine"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "RPGHOME_AUDIO_DEVICE": "null", "RPGHOME_MQTT_PORT": "1884"},
        )
        assert second.returncode == 0, second.stderr
        log = engine_log(env)
        assert "another engine is running" in log
        assert f"{LOCK_NAME} is held" in log
        assert first.poll() is None  # the one that holds the lock plays on
    finally:
        first.send_signal(signal.SIGTERM)
        try:
            first.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            first.kill()
    assert first.returncode == 0
