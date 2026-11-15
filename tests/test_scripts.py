"""start.sh, stop.sh, status.sh and engine.sh against fake docker / launchctl / nc."""

import os
import plistlib
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
SCRIPTS = REPO / "scripts"

# Each fake appends its arguments to $FAKE_DIR/calls.log and keeps state in $FAKE_DIR.
FAKES = {
    "docker": r"""
echo "docker $*" >>"$FAKE_DIR/calls.log"
if [[ $1 == info ]]; then
  [[ ! -f $FAKE_DIR/docker_down ]] || { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
  echo 29.0.0; exit 0
fi
[[ $1 == compose ]] || exit 2
case "$6" in  # docker compose -f FILE -p PROJECT <sub>
  up) touch "$FAKE_DIR/broker_up" ;;
  down) rm -f "$FAKE_DIR/broker_up" ;;
  ps) [[ ! -f $FAKE_DIR/broker_up ]] || echo running ;;
esac
""",
    "nc": r"""
echo "nc $*" >>"$FAKE_DIR/calls.log"
[[ -f $FAKE_DIR/broker_up ]]
""",
    "caffeinate": r"""
echo "caffeinate $*" >>"$FAKE_DIR/calls.log"
""",
    "launchctl": r"""
echo "launchctl $*" >>"$FAKE_DIR/calls.log"
d=$FAKE_DIR
case "$1" in
  print)
    [[ -f $d/job_loaded ]] || { echo "Could not find service" >&2; exit 113; }
    state=$(cat "$d/job_state")
    printf '%s = {\n\tactive count = 1\n\tendpoints = {\n\t\tstate = active\n\t}\n' "$2"
    printf '\tpath = %s\n\tstate = %s\n' "$(cat "$d/job_plist")" "$state"
    [[ $state != running ]] || printf '\tpid = 4242\n'
    printf '\truns = %s\n' "$(cat "$d/job_runs")"
    # After a kill launchd shows the signal instead of an exit code.
    if [[ -f $d/job_signal ]]; then
      printf '\tlast terminating signal = %s\n' "$(cat "$d/job_signal")"
    else
      printf '\tlast exit code = %s\n' "$(cat "$d/job_exit")"
    fi
    printf '}\n'
    ;;
  bootstrap)
    [[ ! -f $d/job_loaded ]] || { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }
    touch "$d/job_loaded"
    echo "$3" >"$d/job_plist"
    echo 1 >"$d/job_runs"
    if [[ -f $d/crash ]]; then
      echo "not running" >"$d/job_state"; echo 1 >"$d/job_exit"
    else
      echo running >"$d/job_state"; echo "(never exited)" >"$d/job_exit"
    fi
    ;;
  bootout)
    [[ -f $d/job_loaded ]] || { echo "Boot-out failed: 3: No such process" >&2; exit 3; }
    rm -f "$d/job_loaded"
    ;;
  kickstart)
    [[ -f $d/job_loaded ]] || exit 113
    echo running >"$d/job_state"
    echo $(($(cat "$d/job_runs") + 1)) >"$d/job_runs"
    ;;
  *) exit 64 ;;
esac
""",
}


class Fakes:
    def __init__(self, root: Path):
        self.dir = root / "fake"
        self.bin = root / "bin"
        self.dir.mkdir()
        self.bin.mkdir()
        for name, body in FAKES.items():
            path = self.bin / name
            path.write_text("#!/bin/bash\n" + body)
            path.chmod(0o755)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(root / "home"),
            "FAKE_DIR": str(self.dir),
            "RPGHOME_ARTIFACTS": str(root / "artifacts"),
            "RPGHOME_STATE": str(root / "state"),
            "RPGHOME_ENGINE_LABEL": "local.rpghome.engine-test",
            "RPGHOME_ENGINE_CHECK_SECONDS": "0",
        }

    def run(self, script: str, *args: str, scripts: Path = SCRIPTS, **env: str):
        return subprocess.run(
            ["/bin/bash", str(scripts / script), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self, prefix: str = "") -> list[str]:
        log = self.dir / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.startswith(prefix)]

    def launchctl(self, verb: str) -> list[str]:
        return self.calls(f"launchctl {verb} ")

    def clear(self):
        (self.dir / "calls.log").unlink(missing_ok=True)

    def set(self, name: str, value: str | None = ""):
        path = self.dir / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value + "\n")

    @property
    def state(self) -> Path:
        return Path(self.env["RPGHOME_STATE"])

    @property
    def plist(self) -> Path:
        return self.state / "launchd" / "local.rpghome.engine-test.plist"


@pytest.fixture
def fakes(tmp_path):
    return Fakes(tmp_path)


def ok(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def test_start_twice_bootstraps_once(fakes):
    out = ok(fakes.run("start.sh"))
    uid = os.getuid()
    assert fakes.launchctl("bootstrap") == [f"launchctl bootstrap gui/{uid} {fakes.plist}"]
    assert any(c.split()[-4:] == ["-p", "rpghome", "up", "-d"] for c in fakes.calls("docker"))
    assert "engine: running (pid 4242)" in out
    assert str(fakes.state / "logs" / "engine.log") in out
    assert "scripts/stop.sh" in out

    fakes.clear()
    out = ok(fakes.run("start.sh"))
    assert "already running (pid 4242), nothing to do" in out
    for verb in ("bootstrap", "bootout", "kickstart"):
        assert fakes.launchctl(verb) == []
    assert not fakes.plist.with_suffix(".plist.new").exists()


def test_start_kickstarts_loaded_but_stopped_job(fakes):
    ok(fakes.run("start.sh"))
    fakes.set("job_state", "not running")
    fakes.clear()
    out = ok(fakes.run("start.sh"))
    uid = os.getuid()
    assert fakes.launchctl("kickstart") == [
        f"launchctl kickstart gui/{uid}/local.rpghome.engine-test"
    ]
    assert fakes.launchctl("bootstrap") == []
    assert "kickstarted" in out


def test_start_reloads_changed_plist(fakes):
    ok(fakes.run("start.sh"))
    fakes.clear()
    out = ok(fakes.run("start.sh", RPGHOME_MQTT_HOST="mqtt.local"))
    verbs = [c.split()[1] for c in fakes.calls("launchctl") if "print" not in c]
    assert verbs == ["bootout", "bootstrap"]
    assert "plist changed" in out
    env = plistlib.loads(fakes.plist.read_bytes())["EnvironmentVariables"]
    assert env["RPGHOME_MQTT_HOST"] == "mqtt.local"


def test_start_fails_when_engine_dies(fakes):
    fakes.set("crash")
    log = fakes.state / "logs" / "engine.log"
    log.parent.mkdir(parents=True)
    log.write_text("ERROR rpghome.engine: engine crashed\n")
    (log.parent / "engine.stdout.log").write_text("Traceback ...\nRuntimeError: no audio device\n")
    result = fakes.run("start.sh")
    assert result.returncode != 0
    assert "engine failed to start" in result.stderr
    assert "engine crashed" in result.stderr
    assert "no audio device" in result.stderr
    assert f"last lines of {log.parent / 'engine.stdout.log'}:" in result.stdout
    assert "state: not running, last exit code 1)" in result.stdout


def test_start_fails_when_broker_never_answers(fakes):
    (fakes.bin / "nc").write_text("#!/bin/bash\nexit 1\n")
    result = fakes.run("start.sh", RPGHOME_BROKER_WAIT_SECONDS="1")
    assert result.returncode != 0
    assert "broker not reachable on 127.0.0.1:1883" in result.stderr
    assert fakes.launchctl("bootstrap") == []


def test_stop_with_and_without_loaded_job(fakes):
    ok(fakes.run("start.sh"))
    fakes.clear()
    out = ok(fakes.run("stop.sh"))
    uid = os.getuid()
    assert fakes.launchctl("bootout") == [f"launchctl bootout gui/{uid}/local.rpghome.engine-test"]
    assert fakes.calls("docker compose")[-1].split()[-3:] == ["-p", "rpghome", "down"]
    assert all("-v" not in c.split() and "--volumes" not in c for c in fakes.calls("docker"))
    assert "engine: stopped" in out

    fakes.clear()
    out = ok(fakes.run("stop.sh"))
    assert fakes.launchctl("bootout") == []
    assert "engine: not loaded" in out
    assert fakes.calls("docker compose")[-1].split()[-3:] == ["-p", "rpghome", "down"]


def test_stop_without_docker(fakes):
    ok(fakes.run("start.sh"))
    fakes.set("docker_down")
    fakes.clear()
    out = ok(fakes.run("stop.sh"))
    assert len(fakes.launchctl("bootout")) == 1  # the engine part still runs
    assert "engine: stopped" in out
    assert "broker: not running (Docker daemon is not running)" in out
    assert fakes.calls("docker compose") == []

    (fakes.bin / "docker").unlink()
    out = ok(fakes.run("stop.sh"))
    assert "engine: not loaded" in out
    assert "broker: not running (docker not found)" in out


def test_start_still_fails_without_docker(fakes):
    fakes.set("docker_down")
    result = fakes.run("start.sh")
    assert result.returncode != 0
    assert "Docker daemon is not running" in result.stderr


def test_last_signal_after_kill(fakes):
    ok(fakes.run("start.sh"))
    fakes.set("job_signal", "Killed: 9")
    fakes.set("job_state", "spawn scheduled")  # launchd throttles the restart
    result = fakes.run("status.sh")
    assert result.returncode == 1
    assert "state spawn scheduled," in result.stdout
    assert "last signal Killed: 9, runs 1" in result.stdout

    fakes.set("job_state", "running")
    fakes.set("job_runs", "2")
    out = ok(fakes.run("status.sh"))
    assert "state running, pid 4242, last signal Killed: 9, runs 2" in out
    assert "last exit code" not in out

    fakes.set("crash")  # the failure message of start.sh shows the signal too
    fakes.set("job_state", "not running")
    (fakes.bin / "launchctl").write_text(
        (fakes.bin / "launchctl")
        .read_text()
        .replace("  kickstart)\n", "  kickstart)\n    exit 0\n")
    )
    result = fakes.run("start.sh")
    assert result.returncode != 0
    assert "(state: not running, last signal Killed: 9)" in result.stdout


def test_status_exit_codes(fakes):
    result = fakes.run("status.sh")
    assert result.returncode == 1
    assert "broker: container missing, 127.0.0.1:1883 NOT reachable" in result.stdout
    assert "engine: local.rpghome.engine-test not loaded" in result.stdout

    ok(fakes.run("start.sh"))
    out = ok(fakes.run("status.sh"))
    assert "broker: container running, 127.0.0.1:1883 reachable" in out
    assert "state running, pid 4242, never exited, runs 1" in out
    assert f"engine log: {fakes.state / 'logs' / 'engine.log'}" in out
    assert f"engine stdout/stderr: {fakes.state / 'logs' / 'engine.stdout.log'}" in out

    fakes.set("job_state", "not running")
    assert fakes.run("status.sh").returncode == 1
    fakes.set("job_state", "running")
    fakes.set("broker_up", None)
    assert fakes.run("status.sh").returncode == 1
    fakes.set("broker_up")
    fakes.set("docker_down")
    result = fakes.run("status.sh")
    assert result.returncode == 1
    assert "Docker daemon not running" in result.stdout


def test_status_shows_the_last_audio_failure(fakes):
    ok(fakes.run("start.sh"))
    assert "last audio failure" not in fakes.run("status.sh").stdout
    failure = fakes.state / "engine-audio-failure.json"
    failure.write_text(
        '{"time": "2026-11-15T10:00:00+00:00", "pid": 7, '
        '"reason": "no audio callback since the start: \\"lid\\" closed?"}\n'
    )
    out = fakes.run("status.sh").stdout
    assert "engine: last audio failure at 2026-11-15T10:00:00+00:00: no audio callback" in out
    assert '"lid" closed?' in out


def test_plist_is_valid_with_special_characters(fakes, tmp_path):
    state = tmp_path / "state dir & <more>"
    ok(fakes.run("start.sh", RPGHOME_STATE=str(state)))
    plist = state / "launchd" / "local.rpghome.engine-test.plist"
    subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True)
    data = plistlib.loads(plist.read_bytes())
    root = tmp_path.resolve()
    assert data["Label"] == "local.rpghome.engine-test"
    assert data["ProgramArguments"] == [str(SCRIPTS / "engine.sh")]
    assert data["WorkingDirectory"] == str(REPO)
    assert data["EnvironmentVariables"] == {
        "RPGHOME_ARTIFACTS": str(root / "artifacts"),
        "RPGHOME_STATE": str(root / "state dir & <more>"),
        "RPGHOME_MQTT_HOST": "127.0.0.1",
        "RPGHOME_MQTT_PORT": "1883",
    }
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["ThrottleInterval"] == 5
    assert data["ExitTimeOut"] == 10
    assert data["ProcessType"] == "Interactive"
    log = str(root / "state dir & <more>" / "logs" / "engine.stdout.log")
    assert data["StandardOutPath"] == data["StandardErrorPath"] == log


def test_default_label(fakes):
    env = {k: v for k, v in fakes.env.items() if k != "RPGHOME_ENGINE_LABEL"}
    fakes.env = env
    ok(fakes.run("start.sh"))
    plist = fakes.state / "launchd" / "local.rpghome.engine.plist"
    assert plistlib.loads(plist.read_bytes())["Label"] == "local.rpghome.engine"


@pytest.mark.parametrize("script", ["setup.sh", "start.sh", "stop.sh", "status.sh", "engine.sh"])
def test_state_inside_data_folder_is_refused(fakes, tmp_path, script):
    args = ["data"] if script == "setup.sh" else []
    result = fakes.run(script, *args, RPGHOME_STATE=str(tmp_path / "artifacts" / "state"))
    assert result.returncode != 0
    assert "must not be inside the data folder" in result.stderr
    assert fakes.calls() == []
    assert not (tmp_path / "artifacts").exists()


def test_engine_sh_without_venv(fakes, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(SCRIPTS, repo / "scripts")
    result = fakes.run("engine.sh", scripts=repo / "scripts")
    assert result.returncode != 0
    assert f"{repo}/.venv/bin/python not found; run scripts/setup.sh deps" in result.stderr
    assert fakes.calls() == []


def test_engine_sh_execs_engine(fakes):
    """engine.sh keeps its PID (exec), so launchd's SIGTERM reaches the engine."""
    log = fakes.state / "logs" / "engine.log"
    proc = subprocess.Popen(
        ["/bin/bash", str(SCRIPTS / "engine.sh")],
        env={**fakes.env, "RPGHOME_AUDIO_DEVICE": "null"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while f"engine started pid={proc.pid}" not in (log.read_text() if log.exists() else ""):
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "engine did not log its start"
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 0
    assert "received SIGTERM, stopping" in log.read_text()
    assert fakes.calls("caffeinate") == [f"caffeinate -i -w {proc.pid}"]
