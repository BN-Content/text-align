"""Shared utilities for refine-alignment tools."""

from __future__ import annotations

from pathlib import Path


def _chapter_id_from_path(path: Path) -> str:
    """Extract BBCCC chapter ID from a filename like SBLGNT-OENGB-66-007-manual.json."""
    parts = path.stem.split("-")
    return parts[-3] + parts[-2]
