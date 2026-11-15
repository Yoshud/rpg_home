"""A clip in the catalog: one audio file in the data folder and what we know about it.

Pydantic v2 models, as in `scene.py`: frozen, extra fields forbidden, JSON through
`model_dump(mode="json")` and `model_validate`. Floats are kept to 3 digits (loudness and gain
are already in 0.1 dB steps), tags and pools are normalized and sorted, so equal clips compare
equal and export the same bytes. Invalid values raise pydantic's `ValidationError`.
"""

import re
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from rpghome.core.errors import RpgHomeError
from rpghome.models import tags as vocabulary

FLOAT_DIGITS = 3


class ClipError(RpgHomeError, ValueError):
    pass


def _rounded(x: float) -> float:
    return round(x, FLOAT_DIGITS) + 0.0  # + 0.0 turns -0.0 into 0.0


Float = Annotated[float, Field(allow_inf_nan=False), AfterValidator(_rounded)]


class Kind(StrEnum):
    BED = "bed"  # continuous background, looped by the engine with crossfades
    SPOT = "spot"  # short random sound over a bed (a drop, a stone)
    FX = "fx"  # one-shot effect triggered by hand (a sword hit)
    MUSIC = "music"


class Source(StrEnum):
    MOSS = "moss"
    EPIDEMIC = "epidemic"
    OTHER = "other"


class License(StrEnum):
    SHAREABLE = "shareable"  # generated clips
    NEVER_SHARE = "never-share"  # Epidemic Sound: never share or publish


class Perspective(StrEnum):
    CLOSE = "close"
    MEDIUM = "medium"
    FAR = "far"


class Status(StrEnum):
    REVIEW = "review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MossDetails(_Model):
    prompt: str
    seed: int
    steps: int
    cfg: Float
    condition: str  # e.g. "default", "cfg3", "neg", or the prompt variant "short" / "detailed"
    negative_prompt: str = ""


class EpidemicDetails(_Model):
    id: str
    title: str


class Loudness(_Model):
    """EBU R128 numbers of the file as it is (see infra/ffmpeg.py). LUFS / dBTP, 0.1 dB steps."""

    integrated_lufs: Float | None  # for information
    bed_lufs: Float | None  # 20th percentile of momentary (400 ms) loudness: the level of a bed
    max_short_term_lufs: Float | None  # loudest 3 s window: the level of an fx or spot
    true_peak_dbtp: Float | None


class AudioInfo(_Model):
    """Stream info of a file (ffprobe)."""

    duration_s: Float
    channels: int
    sample_rate: int


# The fields of a clip that the user may edit; an import sets them, a re-import refreshes only
# the ones the user left as imported.
USER_FIELDS = (
    "kind",
    "tags",
    "pools",
    "perspective",
    "dry",
    "loopable",
    "rating",
    "notes",
    "status",
)

Rating = Annotated[int, Field(ge=1, le=5)]  # 1-5


def _tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({vocabulary.normalize(t) for t in tags}))


def _pools(pools: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({check_pool_name(p) for p in pools}))


# Canonical order, so equal clips compare and export the same.
Tags = Annotated[tuple[str, ...], AfterValidator(_tags)]
Pools = Annotated[tuple[str, ...], AfterValidator(_pools)]


class ImportedFields(_Model):
    kind: Kind
    tags: Tags
    pools: Pools
    perspective: Perspective | None
    dry: bool | None
    loopable: bool
    rating: Rating | None
    notes: str
    status: Status


class Clip(_Model):
    id: str  # derived from the source, so a re-import finds the same clip
    path: str  # relative to the data folder
    kind: Kind
    title: str = ""
    source: Source = Source.OTHER
    details: MossDetails | EpidemicDetails | None = None
    license: License = License.NEVER_SHARE
    tags: Tags = ()
    pools: Pools = ()
    perspective: Perspective | None = None  # None: unknown
    dry: bool | None = None  # True: no acoustics of its own; None: unknown
    loudness: Loudness | None = None
    # Brings the clip to its kind's target level; the file itself is untouched.
    gain_db: Float | None = None
    loopable: bool = False
    duration_s: Float | None = None
    channels: int | None = None
    sample_rate: int | None = None
    rating: Rating | None = None
    notes: str = ""
    status: Status = Status.REVIEW
    # SHA-256 of the file in the data folder as it was imported; a different file means it was
    # changed there, and a re-import leaves it alone.
    sha256: str | None = None
    # The values of the user-editable fields as the import set them; a catalog value that
    # differs from its imported one was edited by the user (None: unknown, all count as edited).
    imported: ImportedFields | None = None
    # Usage state, not catalog content: the export leaves it out. Aware (UTC or an offset):
    # a naive one is refused here, not at the database flush.
    last_used: AwareDatetime | None = None

    @field_validator("id")
    @classmethod
    def _has_id(cls, clip_id: str) -> str:
        if not clip_id:
            raise ValueError("a clip needs an id")
        return clip_id

    @field_validator("path")
    @classmethod
    def _relative(cls, path: str) -> str:
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"clip path must be relative to the data folder: {path!r}")
        return path

    @model_validator(mode="after")
    def _source_rules(self) -> Self:
        if self.source is Source.MOSS and not isinstance(self.details, MossDetails | None):
            raise ValueError("a MOSS clip takes MossDetails")
        if self.source is Source.EPIDEMIC and not isinstance(self.details, EpidemicDetails | None):
            raise ValueError("an Epidemic clip takes EpidemicDetails")
        if self.source is Source.EPIDEMIC and self.license is not License.NEVER_SHARE:
            raise ValueError("Epidemic clips are never-share")
        return self

    def changed(self, **changes: Any) -> Self:
        """A copy with `changes`, checked like a new clip: the merged data is validated, so
        changes may hold plain values (`status="accepted"`) and are normalized like the rest."""
        unknown = sorted(set(changes) - set(type(self).model_fields))
        if unknown:
            raise ClipError(f"unknown clip field(s): {', '.join(unknown)}")
        return self.model_validate({**self.model_dump(), **changes})

    def user_fields(self) -> ImportedFields:
        """The clip's current user-editable values."""
        return ImportedFields(**{f: getattr(self, f) for f in USER_FIELDS})

    def search_text(self) -> str:
        """What a text search looks at: title, prompt (or Epidemic title), notes and tags."""
        parts = [self.title, self.notes, *self.tags]
        if isinstance(self.details, MossDetails):
            parts.append(self.details.prompt)
        elif isinstance(self.details, EpidemicDetails):
            parts.append(self.details.title)
        return "\n".join(parts).casefold()


def check_pool_name(name: str) -> str:
    """Pool names are lowercase words joined by `.` or `-`, e.g. `cave.bed`, `storm.rain`."""
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", name):
        raise ClipError(f"bad pool name {name!r}: use lowercase words joined by '.' or '-'")
    return name


def license_for(source: Source) -> License:
    return License.SHAREABLE if source is Source.MOSS else License.NEVER_SHARE


def status_for(rating: int | None) -> Status:
    """Rating 3 or more: accepted; anything else (lower or none): review."""
    return Status.ACCEPTED if rating is not None and rating >= 3 else Status.REVIEW
