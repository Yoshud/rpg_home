"""Engine commands: the JSON on `rpghome/cmd/<command>`, and the events that answer them.

Pydantic v2 models in the style of `models/scene.py`: frozen, extra fields forbidden (so
typos do not pass silently), strict finite numbers. `parse_command` builds the command a
topic names and raises `CommandError` with the field paths of everything it refuses, as in
`transition_s: ...` or `scene.bed.hold_s: ...`. `id` (optional, a string) is echoed back in
the event so a sender can match the answer to its message.

`scene` carries the scene **data** (`models/scene.py`): resolving a scene name to data is
the app server's job, the engine never reads scene files. `overlay` and `fx` are part of
the schema, so a sender can already send them; the engine answers them with an error.
"""

from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from rpghome.core.errors import RpgHomeError
from rpghome.models.scene import MAX_GAIN_DB, MIN_GAIN_DB, Scene

#: the spot rate `set density` accepts - the same range the engine takes
DENSITY_RANGE = (0.1, 10.0)

Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Text = Annotated[str, Field(strict=True)]
Flag = Annotated[bool, Field(strict=True)]
#: what `set` can change (the engine checks the ranges again)
LevelName = Literal["master_gain_db", "bed_gain_db", "density"]


class CommandError(RpgHomeError, ValueError):
    """A command message the engine will not act on."""

    @classmethod
    def from_validation(cls, command: str, exc: ValidationError) -> "CommandError":
        """One line per error, each with its field path: `transition_s: ...`."""
        lines = []
        for err in exc.errors():
            path = ".".join(str(p) for p in err["loc"])
            lines.append(f"{path}: {err['msg']}" if path else err["msg"])
        return cls(f"{command} command: " + "; ".join(lines))


def _seconds(value: float | None, name: str) -> float | None:
    if value is not None and not 0 <= value < 3600:
        raise ValueError(f"{name} must be within 0..3600 s, got {value}")
    return value


def _name(value: str) -> str:
    if not value.strip():
        raise ValueError("name must be a non-empty string")
    return value


def _message_id(value: str | None) -> str | None:
    """The optional `id`, echoed back in the event."""
    if value is not None and not value.strip():
        raise ValueError("id must be a non-empty string")
    return value


class _Command(BaseModel):
    """What every command message may carry: the optional `id`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Text | None = None

    @field_validator("id")
    @classmethod
    def _non_empty_id(cls, value: str | None) -> str | None:
        return _message_id(value)


class SceneCommand(_Command):
    command: ClassVar[str] = "scene"

    scene: Scene  # the scene data; a bad one names its field, as in scene.bed.hold_s: ...
    transition_s: Number | None = None  # the engine's own default then

    @field_validator("transition_s")
    @classmethod
    def _transition_range(cls, value: float | None) -> float | None:
        return _seconds(value, "transition_s")


class StopCommand(_Command):
    command: ClassVar[str] = "stop"

    fade_s: Number | None = None

    @field_validator("fade_s")
    @classmethod
    def _fade_range(cls, value: float | None) -> float | None:
        return _seconds(value, "fade_s")


class SetCommand(_Command):
    command: ClassVar[str] = "set"

    name: LevelName
    value: Number

    @field_validator("value")
    @classmethod
    def _value_for_name(cls, value: float, info: ValidationInfo) -> float:
        name = info.data.get("name")  # name is validated first; when it is bad, its error is enough
        if name == "density":
            lo, hi = DENSITY_RANGE
            if not lo <= value <= hi:
                raise ValueError(f"density must be within {lo:g}..{hi:g}, got {value}")
        elif name is not None and not MIN_GAIN_DB <= value <= MAX_GAIN_DB:
            raise ValueError(
                f"{name} must be within {MIN_GAIN_DB:g}..{MAX_GAIN_DB:g} dB, got {value}"
            )
        return value


class OverlayCommand(_Command):
    """In the schema; this engine answers it with an error."""

    command: ClassVar[str] = "overlay"

    name: Text
    on: Flag = True
    fade_s: Number | None = None

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        return _name(value)

    @field_validator("fade_s")
    @classmethod
    def _fade_range(cls, value: float | None) -> float | None:
        return _seconds(value, "fade_s")


class FxCommand(_Command):
    """In the schema; this engine answers it with an error."""

    command: ClassVar[str] = "fx"

    name: Text
    distance_m: Number | None = None

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        return _name(value)

    @field_validator("distance_m")
    @classmethod
    def _distance(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError(f"distance_m must be >= 0, got {value}")
        return value


Command = SceneCommand | StopCommand | SetCommand | OverlayCommand | FxCommand

_BY_NAME: dict[str, type[_Command]] = {
    SceneCommand.command: SceneCommand,
    StopCommand.command: StopCommand,
    SetCommand.command: SetCommand,
    OverlayCommand.command: OverlayCommand,
    FxCommand.command: FxCommand,
}


def parse_command(command: str, data: Any) -> Command:
    """Build the command a `rpghome/cmd/<command>` message carries. CommandError names the field."""
    cls = _BY_NAME.get(command)
    if cls is None:
        raise CommandError(f"unknown command {command!r}; known: {', '.join(sorted(_BY_NAME))}")
    if not isinstance(data, dict):
        raise CommandError(f"{command} command: must be a JSON object, got {type(data).__name__}")
    try:
        return cls.model_validate(data)
    except ValidationError as exc:
        raise CommandError.from_validation(command, exc) from None


def ok_event(command: str, msg_id: str | None = None) -> dict:
    """The engine accepted the command (what plays then follows in `rpghome/state`)."""
    event: dict[str, Any] = {"type": "ok", "command": command}
    if msg_id is not None:
        event["id"] = msg_id
    return event


def error_event(command: str, error: str, msg_id: str | None = None) -> dict:
    """The engine did not act on the command; it keeps playing what it played."""
    event: dict[str, Any] = {"type": "error", "command": command, "error": error}
    if msg_id is not None:
        event["id"] = msg_id
    return event
