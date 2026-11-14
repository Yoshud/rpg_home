import os
import subprocess
from pathlib import Path

SETUP = Path(__file__).parents[1] / "scripts" / "setup.sh"


def run_data(
    artifacts: Path | str, tmp_path: Path, **extra_env: str
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        **extra_env,
        "RPGHOME_ARTIFACTS": str(artifacts),
        # Isolate from the user's git config (and its identity).
        "GIT_CONFIG_GLOBAL": str(tmp_path / "no-gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["/bin/bash", str(SETUP), "data"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text() if p.is_file() else "<dir>"
        for p in sorted(root.rglob("*"))
        if ".git" not in p.relative_to(root).parts
    }


def test_data_step_is_idempotent(tmp_path):
    artifacts = tmp_path / "artifacts"
    run_data(artifacts, tmp_path)
    first = snapshot(artifacts)
    head = git(artifacts, "rev-parse", "HEAD")

    run_data(artifacts, tmp_path)
    assert snapshot(artifacts) == first
    assert git(artifacts, "rev-parse", "HEAD") == head
    assert git(artifacts, "rev-list", "--count", "HEAD").strip() == "1"
    assert git(artifacts, "status", "--porcelain") == ""

    for sub in ("scenes", "tiles", "generated", "epidemic"):
        assert (artifacts / sub).is_dir()
    ignore = (artifacts / ".gitignore").read_text().splitlines()
    assert len(ignore) == len(set(ignore))
    for ext in ("wav", "flac", "mp3", "ogg", "opus", "m4a", "aif", "aiff", "aifc"):
        assert f"*.{ext}" in ignore and f"*.{ext.upper()}" in ignore
    for name in ("catalog.sqlite", "catalog.sqlite-wal", "catalog.sqlite-shm"):
        assert name in ignore


def test_data_step_ignores_audio_and_db(tmp_path):
    artifacts = tmp_path / "artifacts"
    run_data(artifacts, tmp_path)
    (artifacts / "epidemic" / "rain.wav").write_bytes(b"x")
    (artifacts / "generated" / "hit.FLAC").write_bytes(b"x")
    (artifacts / "catalog.sqlite").write_bytes(b"x")
    (artifacts / "catalog.sqlite-wal").write_bytes(b"x")
    (artifacts / "catalog.jsonl").write_text("{}\n")
    (artifacts / "scenes" / "tavern.py").write_text("")
    status = git(artifacts, "status", "--porcelain", "--untracked-files=all")
    assert sorted(line[3:] for line in status.splitlines()) == ["catalog.jsonl", "scenes/tavern.py"]


def test_data_step_keeps_existing_repo_and_gitignore(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    git(artifacts, "init", "-q")
    (artifacts / ".gitignore").write_text("*.wav\nmy-own-rule")  # no trailing newline
    git(artifacts, "add", ".gitignore")
    git(artifacts, "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "mine")
    (artifacts / "staged.txt").write_text("user work in progress")
    git(artifacts, "add", "staged.txt")

    run_data(artifacts, tmp_path)
    ignore = (artifacts / ".gitignore").read_text().splitlines()
    assert ignore[:2] == ["*.wav", "my-own-rule"]
    assert ignore.count("*.wav") == 1
    assert git(artifacts, "log", "--format=%s").splitlines() == [
        "Update data folder layout",
        "mine",
    ]
    committed = git(artifacts, "show", "--name-only", "--format=", "HEAD").split()
    assert "staged.txt" not in committed and ".gitignore" in committed
    # The user's staged file stays staged and nothing setup wrote is left uncommitted.
    assert git(artifacts, "status", "--porcelain") == "A  staged.txt\n"

    run_data(artifacts, tmp_path)
    assert len(git(artifacts, "log", "--format=%s").splitlines()) == 2


def test_initial_commit_takes_only_setup_paths(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    git(artifacts, "init", "-q")
    (artifacts / "staged.txt").write_text("x")
    git(artifacts, "add", "staged.txt")
    run_data(artifacts, tmp_path)
    committed = git(artifacts, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == [
        ".gitignore",
        "epidemic/.gitkeep",
        "generated/.gitkeep",
        "scenes/.gitkeep",
        "tiles/.gitkeep",
    ]
    assert git(artifacts, "status", "--porcelain") == "A  staged.txt\n"


def test_tilde_is_expanded(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    run_data("~/data", tmp_path, HOME=str(home))
    assert (home / "data" / "scenes").is_dir()
    assert not (tmp_path / "~").exists()


def test_unknown_step_fails(tmp_path):
    result = subprocess.run(
        ["/bin/bash", str(SETUP), "bogus"],
        env={**os.environ, "RPGHOME_ARTIFACTS": str(tmp_path / "a")},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unknown step" in result.stderr
    assert not (tmp_path / "a").exists()
