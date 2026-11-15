"""The engine process: `python -m rpghome_engine` modes and exit codes."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest
import soundfile as sf

from .conftest import write_tone


def engine(*args, **env):
    return subprocess.run(
        [sys.executable, "-m", "rpghome_engine", *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def engine_log(env_dir):
    path = env_dir / "state" / "logs" / "engine.log"
    return path.read_text() if path.exists() else ""


def write_scene(env_dir, clips=("t.wav",)):
    data = env_dir / "artifacts"
    write_tone(data / "t.wav", 440, 3)
    path = env_dir / "scene.json"
    bed = {"clips": [{"path": c} for c in clips], "hold_s": [2, 2], "crossfade_s": 1,
           "loop_crossfade_s": 0.5}  # fmt: skip
    path.write_text(json.dumps({"name": "test", "bed": bed}))
    return path


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_no_arguments_waits_and_stops_cleanly_on_signal(env, sig):
    proc = subprocess.Popen(
        [sys.executable, "-m", "rpghome_engine"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "RPGHOME_AUDIO_DEVICE": "null", "RPGHOME_MQTT_PORT": "1884"},
    )
    try:
        deadline = time.monotonic() + 15
        while f"engine started pid={proc.pid}" not in engine_log(env):
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "no start line in engine.log"
            time.sleep(0.05)
        time.sleep(0.3)
        proc.send_signal(sig)
        out, _ = proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 0, out
    log = engine_log(env)
    assert f"data={env.resolve() / 'artifacts'}" in log
    assert f"received {sig.name}, stopping" in log
    assert "engine stopped" in log
    assert out == ""  # not a TTY: the log goes only to the file


def test_invalid_settings_exit_0_with_a_log_line(env):
    result = engine(RPGHOME_MQTT_PORT="nope")
    assert result.returncode == 0
    assert "configuration error" in result.stderr
    assert "RPGHOME_MQTT_PORT is not a number" in result.stderr


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda d: d / "missing.json", "cannot read scene file"),
        (lambda d: (d / "x.json").write_text("{nope") and d / "x.json", "Invalid JSON"),
        (lambda d: (d / "y.json").write_text('{"name": ""}') and d / "y.json", "invalid scene"),
        (
            lambda d: (
                (d / "z.json").write_text(
                    '{"name": "s", "bed": {"clips": [{"path": "t.wav"}], "hold_s": [10, 5]}}'
                )
                and d / "z.json"
            ),
            "z.json: bed.hold_s: Value error, hold_s must satisfy 0 < min <= max",
        ),
        (lambda d: write_scene(d, ("t.wav", "gone.wav")), "clip not found: gone.wav"),
    ],
)
def test_bad_scene_on_the_command_line_exit_0_with_a_log_line(env, make, message):
    report = env / "report.json"
    result = engine("--scene", str(make(env)), "--report", str(report),
                    RPGHOME_AUDIO_DEVICE="null")  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert engine_log(env).count("configuration error") == 1
    assert message in engine_log(env)
    assert message in json.loads(report.read_text())["error"]


def test_bad_arguments_exit_0(env):
    result = engine("--offline", str(env / "o.wav"))
    assert result.returncode == 0
    assert "--offline needs --seconds" in result.stderr


def test_offline_renders_the_right_length(env):
    out, report = env / "out" / "o.wav", env / "r.json"
    result = engine("--scene", str(write_scene(env)), "--offline", str(out),
                    "--seconds", "2.5", "--seed", "4", "--report", str(report))  # fmt: skip
    assert result.returncode == 0, result.stderr
    info = sf.info(str(out))
    assert (info.frames, info.channels, info.samplerate) == (120000, 2, 48000)
    assert "engine started offline" in result.stderr  # --offline also logs to stderr
    r = json.loads(report.read_text())
    assert r["mode"] == "offline" and r["seed"] == 4
    assert r["status"]["scene"] == "test"
    assert r["measurements"]["planning_cycles"] > 10
    assert r["measurements"]["starved_blocks"] == 0
    assert r["bed_timeline"][0]["clip"] == "t.wav"
    assert r["spot_timeline"] == []  # the scene has no spots


def test_seconds_on_the_null_device_writes_a_report(env):
    report = env / "r.json"
    result = engine("--scene", str(write_scene(env)), "--seconds", "1", "--report", str(report),
                    RPGHOME_AUDIO_DEVICE="null")  # fmt: skip
    assert result.returncode == 0, result.stderr
    r = json.loads(report.read_text())
    assert r["mode"] == "device" and r["valid"] is True and r["invalid_reason"] is None
    assert set(r["measurements"]) >= {"late_callbacks", "gaps", "max_gap_ms", "min_headroom_ms"}
    assert r["degraded"] == bool(r["degraded_reason"])
    assert r["memory"]["mode"] in ("mlockall", "mlock", "failed")
    assert r["status"]["scene"] == "test" and r["status"]["clip"] == "t.wav"
    assert r["measurements"]["callback_ms"]["count"] > 50
    assert 0.9 < r["seconds_rendered"] < 2.5


def test_crash_exits_non_zero(env):
    code = (
        "import sys, rpghome_engine.__main__ as m\n"
        "def boom(*a): raise RuntimeError('boom')\n"
        "m.Engine.control_cycle = boom\n"
        "sys.exit(m.main(['--seconds', '1']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "RPGHOME_AUDIO_DEVICE": "null"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "engine crashed" in engine_log(env) and "RuntimeError: boom" in engine_log(env)


@pytest.mark.parametrize("device", ["no such device xyz", "999"])
def test_missing_named_device_exits_1_so_launchd_retries(env, device):
    """Unplugged now or a typo look the same: exit 1 with one clear line, every 5 s."""
    result = engine("--seconds", "1", RPGHOME_AUDIO_DEVICE=device)
    assert result.returncode == 1
    log = engine_log(env)
    assert "no audio output: is the lid closed or the output device missing?" in log
    assert f"RPGHOME_AUDIO_DEVICE={device!r}".replace("'999'", "999") in log
    assert "not found (unplugged, or a typo?); output devices now:" in log
    assert "Traceback" not in log
    failure = json.loads((env / "state" / "engine-audio-failure.json").read_text())
    assert "not found" in failure["reason"]


def test_audio_failure_record_is_removed_once_output_plays(env):
    state = env / "state"
    state.mkdir(parents=True)
    (state / "engine-audio-failure.json").write_text('{"time": "t", "reason": "r"}\n')
    result = engine("--seconds", "1.5", RPGHOME_AUDIO_DEVICE="null")
    assert result.returncode == 0, result.stderr
    assert not (state / "engine-audio-failure.json").exists()
    assert "audio output plays again: removed" in engine_log(env)


@pytest.mark.parametrize("value", ["fast", "0", "-1", "5"])
def test_bad_latency_is_a_configuration_error(env, value):
    result = engine("--seconds", "1", RPGHOME_AUDIO_DEVICE="null", RPGHOME_AUDIO_LATENCY=value)
    assert result.returncode == 0
    assert "RPGHOME_AUDIO_LATENCY must be seconds" in engine_log(env)


def test_audio_latency_values():
    from rpghome_engine.__main__ import DEFAULT_LATENCY_S, audio_latency

    assert audio_latency(None) == audio_latency("") == DEFAULT_LATENCY_S == 0.05
    assert audio_latency("0.08") == 0.08
    assert audio_latency(" HIGH ") == "high" and audio_latency("low") == "low"


def test_output_that_never_calls_back_exits_non_zero_with_an_invalid_report(env):
    report = env / "r.json"
    result = engine("--scene", str(write_scene(env)), "--seconds", "30", "--report", str(report),
                    RPGHOME_AUDIO_DEVICE="null-stalled")  # fmt: skip
    assert result.returncode == 1
    log = engine_log(env)
    assert "no audio output: is the lid closed or the output device missing?" in log
    assert "no audio callback since the start" in log
    assert "Traceback" not in log
    failure = json.loads((env / "state" / "engine-audio-failure.json").read_text())
    assert "audio clock has not moved" in failure["reason"] and failure["time"]
    r = json.loads(report.read_text())
    assert r["valid"] is False and "audio clock has not moved" in r["invalid_reason"]
    assert r["measurements"]["callbacks"] == 0 and r["seconds_rendered"] == 0
    assert r["wall_s"] < 10  # the watchdog, not --seconds, ended it


def test_report_without_any_callback_is_invalid(env):
    """Shorter than the watchdog: exit 0, but the report says nothing played."""
    report = env / "r.json"
    result = engine("--seconds", "0.5", "--report", str(report),
                    RPGHOME_AUDIO_DEVICE="null-stalled")  # fmt: skip
    assert result.returncode == 0, result.stderr
    r = json.loads(report.read_text())
    assert r["valid"] is False and "never ran" in r["invalid_reason"]
    assert r["status"]["seconds_rendered"] == 0


def test_unusable_state_folder_is_a_configuration_error(env, tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    result = engine(RPGHOME_STATE=str(blocker / "state"), RPGHOME_AUDIO_DEVICE="null")
    assert result.returncode == 0
    lines = [line for line in result.stderr.splitlines() if line]
    assert len(lines) == 1, result.stderr
    assert "configuration error, not starting: cannot write the engine log" in lines[0]
