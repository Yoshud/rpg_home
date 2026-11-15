import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the data and state folders at temporary directories."""
    monkeypatch.setenv("RPGHOME_ARTIFACTS", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RPGHOME_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("RPGHOME_MQTT_HOST", raising=False)
    monkeypatch.delenv("RPGHOME_MQTT_PORT", raising=False)
    return tmp_path


@pytest.fixture
def sine():
    """write_sine(path, seconds, amplitude=0.1, ...): a synthetic WAV (tests/audio.py)."""
    from audio import write_sine

    return write_sine


@pytest.fixture
def sound_gen(tmp_path):
    """A miniature sound-gen folder (tests/audio.py) with synthetic clips."""
    from audio import make_sound_gen

    return make_sound_gen(tmp_path / "sound-gen")


@pytest.fixture
def needs_ffmpeg():
    from rpghome.infra import ffmpeg

    try:
        ffmpeg.tool("ffmpeg")
        ffmpeg.tool("ffprobe")
    except ffmpeg.FfmpegError as exc:
        pytest.skip(str(exc))
