import warnings
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from rpghome.models import tags
from rpghome.models.clip import (
    Clip,
    ClipError,
    EpidemicDetails,
    Kind,
    License,
    Loudness,
    MossDetails,
    Source,
    Status,
    license_for,
    status_for,
)


def test_tags_normalize_and_match():
    assert tags.normalize(" Place:Cave ") == "place:cave"
    assert tags.normalize("drip") == "drip"
    assert tags.slug("Restaurant & Bar") == "restaurant-bar"
    for bad in ("colour:red", "place:", "two words", "place:a_b"):
        with pytest.raises(tags.TagError):
            tags.normalize(bad)
    have = ("place:cave", "drip")
    assert tags.matches("place:cave", have)
    assert tags.matches("cave", have)
    assert tags.matches("drip", have)
    assert not tags.matches("source:cave", have)


def test_clip_canonical_and_checked():
    clip = Clip(
        id="x",
        path="a/b.wav",
        kind=Kind.BED,
        tags=("drip", "Place:Cave", "drip"),
        pools=("cave.bed", "a"),
    )
    assert clip.tags == ("drip", "place:cave")
    assert clip.pools == ("a", "cave.bed")
    for kwargs in (
        {"path": "/abs.wav"},
        {"path": "../out.wav"},
        {"rating": 6},
        {"rating": 0},
        {"pools": ("Bad Name",)},
        {"source": Source.EPIDEMIC, "license": License.SHAREABLE},
        {"source": Source.MOSS, "details": EpidemicDetails(id="i", title="t")},
    ):
        with pytest.raises(ValidationError):
            Clip(**{"id": "x", "path": "a.wav", "kind": Kind.FX, **kwargs})


def test_license_and_status_rules():
    assert license_for(Source.MOSS) is License.SHAREABLE
    assert license_for(Source.EPIDEMIC) is License.NEVER_SHARE
    assert status_for(3) is Status.ACCEPTED
    assert status_for(5) is Status.ACCEPTED
    assert status_for(2) is Status.REVIEW
    assert status_for(None) is Status.REVIEW


def _full_clip() -> Clip:
    return Clip(
        id="moss:x",
        path="generated/x.wav",
        kind=Kind.BED,
        source=Source.MOSS,
        license=License.SHAREABLE,
        details=MossDetails(prompt="drops", seed=1, steps=100, cfg=4, condition="default"),
        loudness=Loudness(
            integrated_lufs=-30.04449,
            bed_lufs=-31.2,
            max_short_term_lufs=None,
            true_peak_dbtp=-0.0001,
        ),
        tags=("place:cave",),
        rating=4,
        last_used=datetime(2026, 11, 15, 20, 0, tzinfo=UTC),
    )


def test_floats_are_kept_to_three_digits():
    clip = _full_clip()
    assert clip.loudness.integrated_lufs == -30.044
    assert clip.loudness.true_peak_dbtp == 0.0 and str(clip.loudness.true_peak_dbtp) == "0.0"
    assert clip.details.cfg == 4.0 and isinstance(clip.details.cfg, float)


def test_json_round_trip():
    clip = _full_clip()
    data = clip.model_dump(mode="json")
    assert data["kind"] == "bed" and data["tags"] == ["place:cave"]
    assert data["details"]["prompt"] == "drops"
    assert Clip.model_validate(data) == clip
    assert Clip.model_validate_json(clip.model_dump_json()) == clip
    epidemic = Clip(
        id="epidemic:a",
        path="epidemic/a.wav",
        kind=Kind.FX,
        source=Source.EPIDEMIC,
        details=EpidemicDetails(id="a", title="t"),
    )
    again = Clip.model_validate(epidemic.model_dump(mode="json"))
    assert isinstance(again.details, EpidemicDetails) and again == epidemic
    with pytest.raises(ValidationError):
        Clip.model_validate({**data, "colour": "red"})  # extra fields are refused


def test_changed_validates_and_normalizes():
    clip = _full_clip()
    edited = clip.changed(rating=2, tags=("Drip", "place:cave"))
    assert edited.rating == 2 and edited.tags == ("drip", "place:cave")
    assert clip.rating == 4  # frozen: the original is untouched
    with pytest.raises(ValidationError):
        clip.changed(rating=9)
    with pytest.raises(ClipError, match="ratng"):
        clip.changed(ratng=2)
    with pytest.raises(ValidationError):
        clip.rating = 3  # type: ignore[misc]
    assert edited.user_fields().rating == 2
    assert "drops" in clip.search_text()


def test_changed_with_plain_enum_values_warns_nothing():
    """`changed(status="accepted")` must not put a raw string into an enum field (a pydantic
    serializer UserWarning): the merged data is validated instead."""
    clip = _full_clip()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning fails the test
        edited = clip.changed(status="accepted", kind="spot")
    assert edited.status is Status.ACCEPTED and edited.kind is Kind.SPOT


def test_last_used_must_be_aware():
    """A datetime without a time zone is refused by validation, not later at the flush."""
    naive = datetime(2026, 11, 15, 20, 0)
    with pytest.raises(ValidationError, match="aware"):
        Clip(id="x", path="a.wav", kind=Kind.FX, last_used=naive)
    with pytest.raises(ValidationError, match="aware"):
        _full_clip().changed(last_used=naive)
    with pytest.raises(ValidationError, match="aware"):
        Clip.model_validate(
            {"id": "x", "path": "a.wav", "kind": "fx", "last_used": "2026-11-15T20:00:00"}
        )
    assert _full_clip().changed(last_used=naive.replace(tzinfo=UTC)).last_used.tzinfo is UTC
