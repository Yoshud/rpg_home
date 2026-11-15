"""Scene data as the engine receives it: clip paths relative to the data folder, levels
already resolved (the catalog computes each clip's gain_db).

Pydantic v2 models: frozen, extra fields forbidden (so typos do not pass silently), JSON
through `model_validate` / `model_validate_json` and `model_dump(mode="json")`. Numbers
are strict (no strings or booleans) and finite; ranges such as hold_s are two-element
lists in JSON. `scene_from_data` / `scene_from_json` raise `SceneError` with the field
paths of everything that is wrong.

Later layers (overlays) become new optional fields of `Scene`, each with
its own model here.
"""

from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from rpghome.core.errors import RpgHomeError

MIN_GAIN_DB = -90.0
MAX_GAIN_DB = 24.0

Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
GainDb = Annotated[Number, Field(ge=MIN_GAIN_DB, le=MAX_GAIN_DB)]
Text = Annotated[str, Field(strict=True)]
Flag = Annotated[bool, Field(strict=True)]

SOURCES = ("moss", "epidemic", "other")
PERSPECTIVES = {"close": 1.0, "medium": 5.0, "far": 20.0}  # metres; unknown = close
MAX_DISTANCE_M = 1000.0

Source = Literal["moss", "epidemic", "other"]
Distance = Annotated[Number, Field(gt=0, le=MAX_DISTANCE_M)]


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
    """One audio file, relative to the data folder, with the gain that matches its level.

    source, perspective and dry come from the catalog: who made the clip (moss, epidemic,
    other), how far the recording sounds (close, medium, far, or metres) and whether it is
    a dry recording. Spots use source for their weights and perspective for distance.
    In JSON, source, perspective and dry are left out when they are not set.
    """

    path: Text
    gain_db: GainDb = 0.0
    source: Source | None = None
    perspective: Literal["close", "medium", "far"] | Distance | None = None
    dry: Flag = False

    @field_validator("path")
    @classmethod
    def _relative(cls, path: str) -> str:
        if not path.strip():
            raise ValueError("clip path must be a non-empty string")
        p = PurePosixPath(path)
        if p.is_absolute() or ".." in p.parts or "\\" in path:
            raise ValueError(f"clip path must be relative to the data folder: {path!r}")
        return path

    @field_validator("perspective", mode="wrap")
    @classmethod
    def _one_perspective_error(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        """One error for the union (not one per member)."""
        try:
            return handler(value)
        except ValidationError:
            raise ValueError(
                f"perspective must be {', '.join(PERSPECTIVES)} or metres within "
                f"0..{MAX_DISTANCE_M:g}, got {value!r}"
            ) from None

    @model_serializer(mode="wrap")
    def _skip_unset_catalog_fields(self, handler: SerializerFunctionWrapHandler) -> dict:
        data = handler(self)
        for key, default in (("source", None), ("perspective", None), ("dry", False)):
            if data.get(key, default) == default:
                data.pop(key, None)
        return data

    @property
    def distance_m(self) -> float:
        """The distance the recording sounds from: close, medium, far = 1, 5, 20 m."""
        if isinstance(self.perspective, str):
            return PERSPECTIVES[self.perspective]
        return 1.0 if self.perspective is None else self.perspective


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


class Spots(_Model):
    """A layer of random spot sounds (drips, stones), each one a separate event.

    Intervals between events are exponential with mean mean_interval_s (default: the
    middle of interval_s), clamped to interval_s, then divided by the scene's density.
    Each event picks a clip by source_weights (a source not listed weighs 1; with no
    weights every sound is equally likely), never the sound just played, and draws its
    gain from gain_db, a pitch shift within +-pitch_semitones and a pan within +-pan.
    distance_m (None: each clip at its own distance) moves the clips further away, never
    closer. slice cuts each clip into single drops when the scene is set.
    """

    name: Text
    clips: Annotated[tuple[ClipRef, ...], Field(min_length=1)]
    interval_s: tuple[Number, Number]
    mean_interval_s: Annotated[Number, Field(gt=0)]
    gain_db: tuple[GainDb, GainDb] = (0.0, 0.0)
    pitch_semitones: Annotated[Number, Field(ge=0, le=24)] = 0.0
    pan: Annotated[Number, Field(ge=0, le=1)] = 0.8
    distance_m: Distance | None = None
    source_weights: dict[Source, Annotated[Number, Field(ge=0)]] = {}
    slice: Flag = False

    @model_validator(mode="before")
    @classmethod
    def _default_mean(cls, data: Any) -> Any:
        """mean_interval_s defaults to the middle of interval_s (once that is two numbers;
        otherwise interval_s reports the error)."""
        if not isinstance(data, dict) or data.get("mean_interval_s") is not None:
            return data
        interval = data.get("interval_s")
        if (
            isinstance(interval, list | tuple)
            and len(interval) == 2
            and all(isinstance(v, int | float) and not isinstance(v, bool) for v in interval)
        ):
            return {**data, "mean_interval_s": (interval[0] + interval[1]) / 2}
        return {**data, "mean_interval_s": 1.0}

    @field_validator("name")
    @classmethod
    def _non_empty(cls, name: str) -> str:
        if not name.strip():
            raise ValueError("spots name must be a non-empty string")
        return name

    @field_validator("interval_s")
    @classmethod
    def _interval_order(cls, interval: tuple[float, float]) -> tuple[float, float]:
        lo, hi = interval
        if not 0 < lo <= hi:
            raise ValueError(f"interval_s must satisfy 0 < min <= max, got [{lo}, {hi}]")
        return interval

    @field_validator("gain_db")
    @classmethod
    def _gain_order(cls, gain: tuple[float, float]) -> tuple[float, float]:
        lo, hi = gain
        if lo > hi:
            raise ValueError(f"gain_db must satisfy min <= max, got [{lo}, {hi}]")
        return gain

    @model_validator(mode="after")
    def _some_weight(self) -> "Spots":
        if not any(self.weight(c) > 0 for c in self.clips):
            raise ValueError("every clip has source weight 0")
        return self

    def weight(self, clip: ClipRef) -> float:
        """The weight of the clip's source (other when it has none)."""
        return self.source_weights.get(clip.source or "other", 1.0)

    def __hash__(self) -> int:  # source_weights is a dict
        return hash((self.name, self.clips, self.interval_s))


class Reverb(_Model):
    """The scene's synthetic reverb for spots: stereo noise decaying by 60 dB over decay_s,
    darkened, generated from seed. wet_db is the wet level against the direct sound."""

    decay_s: Annotated[Number, Field(ge=0.1, le=10)] = 2.2
    wet_db: GainDb = -10.0
    seed: Annotated[int, Field(strict=True, ge=0)] = 3


class Scene(_Model):
    """What the engine plays: an optional bed, spot layers and the spots' reverb (None:
    spots play dry). All layers are optional; a scene with none is silence."""

    name: Text
    bed: Bed | None = None
    gain_db: GainDb = 0.0
    spots: tuple[Spots, ...] = ()
    reverb: Reverb | None = None
    # Later tasks add optional layers here: overlays.

    @field_validator("name")
    @classmethod
    def _non_empty(cls, name: str) -> str:
        if not name.strip():
            raise ValueError("scene name must be a non-empty string")
        return name

    @field_validator("spots")
    @classmethod
    def _unique_names(cls, spots: tuple[Spots, ...]) -> tuple[Spots, ...]:
        names = [s.name for s in spots]
        if len(set(names)) != len(names):
            raise ValueError(f"spots names must be unique, got {names}")
        return spots


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
