"""Tag vocabulary of the clip catalog.

A tag is either `category:value` with a category from CATEGORIES (for example `place:cave`,
`source:water`, `mood:calm`) or a plain free tag (`drip`, `ambience`). Values and free tags are
lowercase words joined by `-`.
"""

import re

from rpghome.core.errors import RpgHomeError

# The fixed categories, the only place they are listed.
CATEGORIES: dict[str, str] = {
    "place": "where the sound happens: cave, tavern, forest, dungeon, city, mountains",
    "source": "what makes the sound: water, rain, thunder, wind, fire, stone, sword, crowd, ...",
    "weather": "rain, storm",
    "mood": "calm, busy, tense, eerie",
    "use": "what it was made for: rpg, film",
    "batch": "the import or generation batch the clip came from",
}

_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class TagError(RpgHomeError, ValueError):
    pass


def slug(text: str) -> str:
    """Free text as a tag value: lowercase words joined by `-` (`Rock & Roll` -> `rock-roll`)."""
    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))


def tag(category: str, value: str) -> str:
    return normalize(f"{category}:{value}")


def normalize(text: str) -> str:
    """Check a tag and return it in canonical form (lowercase, trimmed)."""
    t = text.strip().lower()
    category, sep, value = t.partition(":")
    if sep:
        if category not in CATEGORIES:
            known = ", ".join(sorted(CATEGORIES))
            raise TagError(f"unknown tag category {category!r} in {text!r} (known: {known})")
        if not _WORD.fullmatch(value):
            raise TagError(f"bad tag value in {text!r}: use lowercase words joined by '-'")
        return t
    if not _WORD.fullmatch(t):
        raise TagError(f"bad tag {text!r}: use lowercase words joined by '-', or category:value")
    return t


def matches(query: str, tags: tuple[str, ...] | list[str]) -> bool:
    """`place:cave` matches only itself; a plain `cave` matches the free tag `cave` or the value
    of any category tag (`place:cave`)."""
    q = normalize(query)
    if ":" in q:
        return q in tags
    return any(t == q or t.partition(":")[2] == q for t in tags)
