"""Scene data as the engine receives it: clip paths relative to the data folder, levels
already resolved (the catalog computes each clip's gain_db).

Pydantic v2 models: frozen, extra fields forbidden (so typos do not pass silently), JSON
through `model_validate` / `model_validate_json` and `model_dump(mode="json")`. Numbers
are strict (no strings or booleans) and finite; ranges such as hold_s are two-element
lists in JSON. `scene_from_data` / `scene_from_json` raise `SceneError` with the field
paths of everything that is wrong.

Later layers (spots, reverb, overlays) become new optional fields of `Scene`, each with
its own model here.
"""

from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rpghome.core.errors import RpgHomeError

MIN_GAIN_DB = -90.0
MAX_GAIN_DB = 24.0

Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
GainDb = Annotated[Number, Field(ge=MIN_GAIN_DB, le=MAX_GAIN_DB)]
Text = Annotated[str, Field(strict=True)]


class SceneError(RpgHomeError, ValueError):
    """Invalid scene data."""

    @classmethod
    def from_validation(cls, exc: ValidationError) -> "SceneError":
        """One line per error, each with its field path: `bed.hold_s: ...`."""
        lines = []
        for err in exc.errors():
            path = ".".join(str(p) for p in err["loc"])
            lines.append(f"{path}: {err['msg']}" if path else err["msg"])
        return cls("; ".join(lines))


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ClipRef(_Model):
    """One audio file, relative to the data folder, with the gain that matches its level."""

    path: Text
    gain_db: GainDb = 0.0

    @field_validator("path")
    @classmethod
    def _relative(cls, path: str) -> str:
        if not path.strip():
            raise ValueError("clip path must be a non-empty string")
        p = PurePosixPath(path)
        if p.is_absolute() or ".." in p.parts or "\\" in path:
            raise ValueError(f"clip path must be relative to the data folder: {path!r}")
        return path


class Bed(_Model):
    """The ambient bed: one clip of the pool at a time, looped end to start with an
    equal-power crossfade of loop_crossfade_s. Each clip is held for a time drawn from
    hold_s (from the start of its fade-in to the start of its fade-out), then crossfades
    over crossfade_s into another clip of the pool."""

    clips: Annotated[tuple[ClipRef, ...], Field(min_length=1)]
    hold_s: tuple[Number, Number] = (150.0, 210.0)
    crossfade_s: Number = 35.0
    loop_crossfade_s: Annotated[Number, Field(ge=0)] = 4.0
    gain_db: GainDb = 0.0

    @field_validator("hold_s")
    @classmethod
    def _hold_order(cls, hold: tuple[float, float]) -> tuple[float, float]:
        lo, hi = hold
        if not 0 < lo <= hi:
            raise ValueError(f"hold_s must satisfy 0 < min <= max, got [{lo}, {hi}]")
        return hold

    @model_validator(mode="after")
    def _crossfade_fits(self) -> "Bed":
        lo = self.hold_s[0]
        if not 0 <= self.crossfade_s <= lo:
            raise ValueError(
                f"crossfade_s must be within 0..hold_s min ({lo}), got {self.crossfade_s}"
            )
        return self


class Scene(_Model):
    """What the engine plays. bed may be None (silence until later layers exist)."""

    name: Text
    bed: Bed | None = None
    gain_db: GainDb = 0.0
    # Later tasks add optional layers here: spots, reverb, overlays.

    @field_validator("name")
    @classmethod
    def _non_empty(cls, name: str) -> str:
        if not name.strip():
            raise ValueError("scene name must be a non-empty string")
        return name


def scene_from_data(data: Any) -> Scene:
    """Scene.model_validate, raising SceneError."""
    try:
        return Scene.model_validate(data)
    except ValidationError as exc:
        raise SceneError.from_validation(exc) from None


def scene_from_json(text: str | bytes) -> Scene:
    """Scene.model_validate_json, raising SceneError (also for text that is not JSON)."""
    try:
        return Scene.model_validate_json(text)
    except ValidationError as exc:
        raise SceneError.from_validation(exc) from None
