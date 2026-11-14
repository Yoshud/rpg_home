"""Paths and settings, resolved from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path

from rpghome.core.errors import RpgHomeError

DEFAULT_ARTIFACTS = "~/rpg_home_artifacts"
DEFAULT_STATE = "~/.local/state/rpghome"
DEFAULT_MQTT_HOST = "127.0.0.1"
DEFAULT_MQTT_PORT = 1883


class ConfigError(RpgHomeError):
    pass


@dataclass(frozen=True)
class Settings:
    artifacts_dir: Path
    state_dir: Path
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = DEFAULT_MQTT_PORT

    @property
    def log_dir(self) -> Path:
        """Log files of native processes (engine.log)."""
        return self.state_dir / "logs"


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    artifacts = _path(env.get("RPGHOME_ARTIFACTS") or DEFAULT_ARTIFACTS)
    state = _path(env.get("RPGHOME_STATE") or DEFAULT_STATE)
    if state.is_relative_to(artifacts):
        raise ConfigError(
            f"RPGHOME_STATE ({state}) must not be inside the data folder ({artifacts})"
        )
    port = env.get("RPGHOME_MQTT_PORT") or str(DEFAULT_MQTT_PORT)
    try:
        mqtt_port = int(port)
    except ValueError:
        raise ConfigError(f"RPGHOME_MQTT_PORT is not a number: {port!r}") from None
    return Settings(
        artifacts_dir=artifacts,
        state_dir=state,
        mqtt_host=env.get("RPGHOME_MQTT_HOST") or DEFAULT_MQTT_HOST,
        mqtt_port=mqtt_port,
    )
