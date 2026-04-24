"""Coverage evaluation for alignment chapter JSON files.

Identifies verses where more than N source tokens appear in neither any record's
source list nor the chapter-level nonEquivalent.source set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from text_align.migrate.alignment_io import load_alignment_json


@dataclass
class CoverageStats:
    verse_id: str
    src_total: int
    src_covered: int
    uncovered_src_ids: list[str] = field(default_factory=list)

    @property
    def uncovered_count(self) -> int:
        return self.src_total - self.src_covered


@dataclass
class VerseRetrySpec:
    verse_id: str
    chapter_id: str  # BBCCC
    uncovered_src_ids: list[str]
    uncovered_count: int


def _chapter_id_from_path(path: Path) -> str:
    """Extract BBCCC chapter ID from a filename like SBLGNT-OENGB-66-007-manual.json.

    Parses from the end so edition names containing hyphens are handled correctly.
    """
    parts = path.stem.split("-")
    # Format: {corpus_id}-{edition}-{BB}-{CCC}-manual
    return parts[-3] + parts[-2]


def find_low_coverage_verses(
    chapter_json_path: Path,
    source_verses: dict[str, list],
    min_unaligned_src: int = 2,
) -> list[VerseRetrySpec]:
    """Return retry specs for verses with more than min_unaligned_src unaligned source tokens.

    A source token is considered covered if it appears in any record's source list
    or in the chapter-level nonEquivalent.source set.
    """
    data = load_alignment_json(chapter_json_path)
    groups = data.get("groups", [])
    if not groups:
        return []

    group = groups[0]
    records: list[dict] = group.get("records", [])
    neq_source: set[str] = set(group.get("meta", {}).get("nonEquivalent", {}).get("source", []))

    # Build covered set per verse from records and NEQ
    covered_by_verse: dict[str, set[str]] = {}
    for rec in records:
        for sid in rec.get("source") or []:
            covered_by_verse.setdefault(sid[:8], set()).add(sid)
    for sid in neq_source:
        covered_by_verse.setdefault(sid[:8], set()).add(sid)

    chapter_id = _chapter_id_from_path(chapter_json_path)
    retry_specs: list[VerseRetrySpec] = []

    for verse_id in sorted(v for v in source_verses if v[:5] == chapter_id):
        all_src_ids = {t.id for t in source_verses[verse_id]}
        covered = covered_by_verse.get(verse_id, set())
        uncovered = sorted(all_src_ids - covered)
        if len(uncovered) > min_unaligned_src:
            retry_specs.append(VerseRetrySpec(
                verse_id=verse_id,
                chapter_id=chapter_id,
                uncovered_src_ids=uncovered,
                uncovered_count=len(uncovered),
            ))

    return retry_specs
