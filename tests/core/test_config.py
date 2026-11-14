from pathlib import Path

import pytest

from rpghome.core.config import ConfigError, load_settings


def test_defaults():
    s = load_settings({})
    assert s.artifacts_dir == Path("~/rpg_home_artifacts").expanduser()
    assert s.state_dir == Path("~/.local/state/rpghome").expanduser()
    assert (s.mqtt_host, s.mqtt_port) == ("127.0.0.1", 1883)


def test_env_overrides(tmp_path):
    s = load_settings(
        {"RPGHOME_ARTIFACTS": str(tmp_path / "a"), "RPGHOME_STATE": str(tmp_path / "s")}
    )
    assert s.artifacts_dir == tmp_path.resolve() / "a"
    assert s.log_dir == tmp_path.resolve() / "s" / "logs"


def test_state_inside_data_folder_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(
            {"RPGHOME_ARTIFACTS": str(tmp_path), "RPGHOME_STATE": str(tmp_path / "state")}
        )


def test_bad_port(tmp_path):
    with pytest.raises(ConfigError):
        load_settings({"RPGHOME_ARTIFACTS": str(tmp_path), "RPGHOME_MQTT_PORT": "x"})
