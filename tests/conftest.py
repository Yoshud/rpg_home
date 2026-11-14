import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the data and state folders at temporary directories."""
    monkeypatch.setenv("RPGHOME_ARTIFACTS", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RPGHOME_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("RPGHOME_MQTT_HOST", raising=False)
    monkeypatch.delenv("RPGHOME_MQTT_PORT", raising=False)
    return tmp_path
