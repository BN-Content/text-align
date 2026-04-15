"""Dataclasses for alignment migration (target tokens and verse containers)."""

import dataclasses
from dataclasses import dataclass


def y_or_n(value: str) -> bool:
    """Convert a 'y'/'n' (or truthy/falsy) string to bool."""
    return value == "y"


@dataclass
class MigrateTarget:
    """A translation target token as read from a kathairo TSV for migration.

    Covers both the diff-migrate (which has range columns) and sim-migrate
    (which omits them) use cases — range fields default to empty string.
    """

    id: str
    source_verse: str
    text: str
    id_range_end: str = ""
    source_verse_range_end: str = ""
    skip_space_after: bool = False
    exclude: bool = False
    required: bool = False


@dataclass
class MigrateVerse:
    """A verse container: BCV metadata plus an ordered dict of MigrateTarget tokens."""

    id: str
    book: str
    chapter: str
    verse: str
    usfm: str
    # str key is the token ID (for stable sort / order)
    words: dict[str, MigrateTarget] = dataclasses.field(default_factory=dict)
