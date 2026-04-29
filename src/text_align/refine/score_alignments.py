"""score-alignment: audit alignment quality without running the LLM.

Reads chapter JSON files, scores each verse using the composite penalty scorer,
and writes a TSV report to stdout (or --output). Useful for deciding which
chapters need retry-alignment and for tuning the scoring thresholds.

CLI entry point: score-alignment
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from text_align import ROOT
from text_align.config import load_config_from_args, require

from .retry import discover_chapter_files
from .scoring import ScoringConfig, VerseScore, score_chapter_file
from .source import load_source_verses


_SOURCES_DIR = ROOT / "data" / "sources"

_TSV_FIELDS = [
    "verse_id",
    "composite",
    "signal_1",
    "signal_2",
    "signal_3",
    "signal_4",
    "signal_5",
    "needs_retry",
    "structural_errors",
]


def parse_args() -> argparse.Namespace:
    config_defaults = load_config_from_args(output_suffix="LLM-REFINED")

    p = argparse.ArgumentParser(
        description=(
            "Score alignment quality for chapter JSON files and report per-verse "
            "penalty scores. Does not call the LLM."
        )
    )
    p.add_argument("--config", metavar="NAME",
                   help="Load defaults from configs/<NAME>.yaml (CLI args override)")
    p.add_argument("--alignment-dir", default=None, type=Path,
                   help="Directory containing chapter JSON files to score")
    p.add_argument("--target-language", default=None,
                   help="ISO 639-3 language code, e.g. eng")
    p.add_argument("--target-edition", default=None,
                   help="Target edition ID (used for path derivation only)")
    p.add_argument("--target-tsv-dir", default=None, type=Path,
                   help="Directory containing target TSVs (enables signal 2 scoring)")
    p.add_argument("--sources-dir", default=_SOURCES_DIR, type=Path,
                   help=f"Directory containing SBLGNT.tsv and WLCM.tsv (default: {_SOURCES_DIR})")
    p.add_argument("--corpus", default=None, choices=["ot", "nt"],
                   help="Corpus: 'nt' for SBLGNT, 'ot' for WLCM")
    p.add_argument("--score-retry-threshold", type=float, default=0.25,
                   help="Penalty threshold for needs_retry flag (default: 0.25)")
    p.add_argument("--output", default=None, type=Path,
                   help="Write TSV report to this file (default: stdout)")
    p.add_argument("--flagged-only", action="store_true", default=False,
                   help="Only output verses where needs_retry is True")

    range_group = p.add_mutually_exclusive_group()
    range_group.add_argument("--book", default=None, metavar="BB")
    range_group.add_argument("--book-range", default=None, nargs=2, metavar=("START", "END"))
    range_group.add_argument("--chapter", default=None, metavar="BBCCC")
    range_group.add_argument("--chapter-range", default=None, nargs=2,
                             metavar=("START", "END"))

    p.set_defaults(**config_defaults)
    args = p.parse_args()
    require(args, "alignment_dir", "target_language", "corpus")
    return args


def _filter_chapter_files(chapter_files: list[Path], args: argparse.Namespace) -> list[Path]:
    book = getattr(args, "book", None)
    book_range = getattr(args, "book_range", None)
    chapter = getattr(args, "chapter", None)
    chapter_range = getattr(args, "chapter_range", None)

    if not any([book, book_range, chapter, chapter_range]):
        return chapter_files

    result = []
    for f in chapter_files:
        parts = f.stem.split("-")
        cid = parts[-3] + parts[-2]
        if book:
            if cid[:2] == str(book).zfill(2):
                result.append(f)
        elif book_range:
            start, end = str(book_range[0]).zfill(2), str(book_range[1]).zfill(2)
            if start <= cid[:2] <= end:
                result.append(f)
        elif chapter:
            if cid == str(chapter).zfill(5):
                result.append(f)
        elif chapter_range:
            start, end = str(chapter_range[0]).zfill(5), str(chapter_range[1]).zfill(5)
            if start <= cid <= end:
                result.append(f)
    return result


def main() -> None:
    args = parse_args()
    corpus_id = "SBLGNT" if args.corpus == "nt" else "WLCM"

    chapter_files = discover_chapter_files(args.alignment_dir)
    chapter_files = _filter_chapter_files(chapter_files, args)
    if not chapter_files:
        raise SystemExit("No chapter JSON files found in --alignment-dir.")

    print(f"score-alignment: {args.target_language}", file=sys.stderr)
    print(f"  Alignment dir:   {args.alignment_dir}", file=sys.stderr)
    print(f"  Retry threshold: {args.score_retry_threshold:.2f}", file=sys.stderr)
    print(f"  Chapters:        {len(chapter_files)}", file=sys.stderr)

    print(f"  Loading source tokens ({corpus_id}) ...", file=sys.stderr)
    source_verses = load_source_verses(args.sources_dir, args.corpus)

    target_verses = None
    if args.target_tsv_dir and args.target_edition:
        from text_align.migrate.tsv import process_usfm_tsv
        print(f"  Loading target tokens ({args.target_edition}) ...", file=sys.stderr)
        target_verses = process_usfm_tsv(args.target_tsv_dir, args.target_edition)

    scoring_config = ScoringConfig(retry_threshold=args.score_retry_threshold)

    all_scores: list[VerseScore] = []
    for cf in chapter_files:
        verse_scores = score_chapter_file(
            cf, source_verses, args.target_language, scoring_config,
            target_verses=target_verses,
        )
        all_scores.extend(verse_scores)

    if args.flagged_only:
        all_scores = [vs for vs in all_scores if vs.needs_retry]

    total = len(all_scores)
    flagged = sum(1 for vs in all_scores if vs.needs_retry)
    print(
        f"  Scored {total} verse(s); {flagged} flagged for retry "
        f"({100*flagged/total:.1f}%)" if total else "  No verses scored.",
        file=sys.stderr,
    )

    out_stream = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        writer = csv.DictWriter(out_stream, fieldnames=_TSV_FIELDS, delimiter="\t")
        writer.writeheader()
        for vs in all_scores:
            writer.writerow({
                "verse_id":          vs.verse_id,
                "composite":         f"{vs.composite:.4f}",
                "signal_1":          f"{vs.signal_1:.4f}",
                "signal_2":          f"{vs.signal_2:.4f}",
                "signal_3":          f"{vs.signal_3:.4f}",
                "signal_4":          f"{vs.signal_4:.4f}",
                "signal_5":          f"{vs.signal_5:.4f}",
                "needs_retry":       str(vs.needs_retry),
                "structural_errors": vs.structural_errors,
            })
    finally:
        if args.output:
            out_stream.close()


if __name__ == "__main__":
    main()
