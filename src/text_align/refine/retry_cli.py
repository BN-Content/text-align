"""retry-alignment: re-align verses with too many unaligned source tokens.

After fetch-batch writes chapter JSON files, this command identifies verses
where more than N source tokens are unaligned and re-aligns them from scratch
(blank-slate — no candidates passed to the LLM).

CLI entry point: retry-alignment
"""

from __future__ import annotations

import argparse
from pathlib import Path

from text_align import ROOT
from text_align.config import load_config_from_args, require
from text_align.migrate.tsv import process_usfm_tsv

from .clean import run_clean_pass
from .coverage import VerseRetrySpec, find_low_coverage_verses
from .llm import LLMClient
from .retry import (
    _filter_chapter_files,
    build_retry_chapter_batches,
    discover_chapter_files,
    retry_chapter_sync,
)
from .scoring import ScoringConfig, score_chapter_file
from .source import load_source_verses
from .util import _CORPUS_ID, _chapter_id_from_path


_SOURCES_DIR = ROOT / "data" / "sources"
_JOBS_DIR = ROOT / "jobs"


def parse_args() -> argparse.Namespace:
    config_defaults = load_config_from_args(output_suffix="LLM-REFINED")

    p = argparse.ArgumentParser(
        description=(
            "Re-align verses with too many unaligned source tokens. "
            "Evaluates existing chapter JSON files, flags verses where more than "
            "N source tokens are unaligned, and re-aligns them from scratch."
        )
    )
    p.add_argument("--config", metavar="NAME",
                   help="Load defaults from configs/<NAME>.yaml (CLI args override)")
    p.add_argument("--alignment-dir", default=None, type=Path,
                   help="Directory containing chapter JSON files to evaluate and retry")
    p.add_argument("--target-language", default=None,
                   help="ISO 639-3 language code, e.g. eng")
    p.add_argument("--target-edition", default=None,
                   help="Target edition ID, e.g. OENGB")
    p.add_argument("--target-tsv-dir", default=None, type=Path,
                   help="Directory containing ot_<edition>.tsv and nt_<edition>.tsv")
    p.add_argument("--sources-dir", default=_SOURCES_DIR, type=Path,
                   help=f"Directory containing SBLGNT.tsv and WLCM.tsv (default: {_SOURCES_DIR})")
    p.add_argument("--corpus", default=None, choices=["ot", "nt"],
                   help="Corpus: 'nt' for SBLGNT, 'ot' for WLCM")
    p.add_argument("--llm-provider", default="anthropic",
                   choices=["openai", "anthropic", "google", "openrouter"],
                   help="LLM provider (default: anthropic)")
    p.add_argument("--llm-model", default=None,
                   help="Model name for the chosen provider")
    p.add_argument("--reasoning-effort", default=None,
                   choices=["none", "minimal", "low", "medium", "high"],
                   help="Reasoning effort level")
    p.add_argument("--batch-size", type=int, default=5,
                   help="Verses per LLM call (default: 5)")
    p.add_argument("--max-retries", type=int, default=2,
                   help="Retry attempts on validation failure (default: 2)")
    p.add_argument("--max-api-retries", type=int, default=4,
                   help="Retry attempts on transient API errors with exponential backoff (default: 4)")
    p.add_argument("--temperature", type=float, default=1,
                   help="Sampling temperature (default: 1)")
    p.add_argument("--max-output-tokens", type=int, default=32000,
                   help="Hard cap on response tokens (default: 32000)")
    p.add_argument("--creator", default="text-align",
                   help="Creator string for alignment meta (default: text-align)")
    p.add_argument("--score-retry-threshold", type=float, default=0.25,
                   help="Composite penalty threshold above which a verse is retried (default: 0.25)")
    p.add_argument("--min-unaligned-src", type=int, default=2,
                   help="Also retry verses with N or more unaligned source tokens (default: 2)")
    p.add_argument("--batch-mode", choices=["sync", "async"], default="sync",
                   help="sync: re-align immediately and write results (default); "
                        "async: submit to provider batch API and exit "
                        "(use fetch-batch to retrieve and merge results)")
    p.add_argument("--jobs-dir", default=_JOBS_DIR, type=Path,
                   help=f"Directory for async batch job metadata (default: {_JOBS_DIR})")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Report flagged verses without calling the LLM")

    range_group = p.add_mutually_exclusive_group()
    range_group.add_argument("--verse", default=None, metavar="BBCCCVVV",
                             help="Force-retry a single verse regardless of score, e.g. --verse 41004003")
    range_group.add_argument("--verse-range", default=None, nargs=2, metavar=("START", "END"),
                             help="Force-retry a verse range regardless of score, e.g. --verse-range 41004001 41004020")
    range_group.add_argument("--verse-list", default=None, metavar="VIDS",
                             help="Comma-separated verse IDs to force-retry regardless of score, "
                                  "e.g. --verse-list 62002002,62003010")
    range_group.add_argument("--verse-list-file", default=None, type=Path, metavar="FILE",
                             help="File of verse IDs to force-retry regardless of score "
                                  "(one BBCCCVVV per line; blank lines and # comments ignored)")
    range_group.add_argument("--book", default=None, metavar="BB",
                             help="Limit to a single book, e.g. --book 66")
    range_group.add_argument("--book-range", default=None, nargs=2, metavar=("START", "END"),
                             help="Limit to a book range, e.g. --book-range 65 66")
    range_group.add_argument("--chapter", default=None, metavar="BBCCC",
                             help="Limit to a single chapter, e.g. --chapter 66007")
    range_group.add_argument("--chapter-range", default=None, nargs=2, metavar=("START", "END"),
                             help="Limit to a chapter range, e.g. --chapter-range 66001 66022")

    p.set_defaults(**config_defaults)
    args = p.parse_args()

    # Retry-specific model keys fall back to the refine model keys when absent.
    # This allows a single config to use one model for both passes, or separate
    # configs to specify different models per pass.
    args.llm_provider = getattr(args, "retry_llm_provider", None) or args.llm_provider
    args.llm_model    = getattr(args, "retry_llm_model",    None) or args.llm_model
    args.reasoning_effort = getattr(args, "retry_reasoning_effort", None) or args.reasoning_effort

    require(args, "alignment_dir", "target_language", "target_edition", "target_tsv_dir", "corpus")

    if args.llm_model is None and not args.dry_run:
        raise SystemExit(
            "error: --llm-model is required (or set in --config) unless --dry-run"
        )

    return args


def main() -> None:
    args = parse_args()
    corpus_id = _CORPUS_ID[args.corpus]
    effort_str = f" (reasoning_effort={args.reasoning_effort})" if args.reasoning_effort else ""

    print(f"retry-alignment: {args.target_edition} ({args.target_language})")
    print(f"  Alignment dir:   {args.alignment_dir}")
    print(f"  Retry threshold: score>{args.score_retry_threshold:.2f} or unaligned-src>={args.min_unaligned_src}")
    if not args.dry_run:
        print(f"  Provider:        {args.llm_provider} / {args.llm_model}{effort_str}")
        print(f"  Mode:            {args.batch_mode}")

    # Build forced-verse set from --verse-list or --verse-list-file
    forced_verse_set: frozenset[str] | None = None
    verse_list_arg: str | None = getattr(args, "verse_list", None)
    verse_list_file: Path | None = getattr(args, "verse_list_file", None)
    if verse_list_arg:
        forced_verse_set = frozenset(v.strip() for v in verse_list_arg.split(",") if v.strip())
        print(f"  Force-retry list: {len(forced_verse_set)} verse(s) (--verse-list)")
    elif verse_list_file:
        lines = verse_list_file.read_text().splitlines()
        forced_verse_set = frozenset(
            ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")
        )
        print(f"  Force-retry list: {len(forced_verse_set)} verse(s) from {verse_list_file}")

    # Discover and filter chapter files
    chapter_files = discover_chapter_files(args.alignment_dir)
    chapter_files = _filter_chapter_files(chapter_files, args, forced_verse_set)
    if not chapter_files:
        raise SystemExit("No chapter JSON files found in --alignment-dir.")
    print(f"  Evaluating {len(chapter_files)} chapter file(s) ...")

    # Load source and target tokens (needed for clean pass and scoring)
    print(f"  Loading source tokens ({corpus_id}) ...")
    source_verses = load_source_verses(args.sources_dir, args.corpus)
    print(f"  Loading target tokens ({args.target_edition}) ...")
    target_verses = process_usfm_tsv(args.target_tsv_dir, args.target_edition)

    print("  Cleaning alignment files ...")
    files_changed, dropped, repaired = run_clean_pass(chapter_files, source_verses, target_verses)
    if files_changed:
        print(
            f"  Cleaned {files_changed} file(s): "
            f"{dropped} record(s) dropped, {repaired} record(s) repaired."
        )

    scoring_config = ScoringConfig(retry_threshold=args.score_retry_threshold)

    # Verse-level force-include: these verses are retried regardless of score.
    forced_verse: str | None = getattr(args, "verse", None)
    forced_verse_range: list[str] | None = getattr(args, "verse_range", None)

    def _is_forced(vid: str) -> bool:
        if forced_verse:
            return vid == forced_verse
        if forced_verse_range:
            return forced_verse_range[0] <= vid <= forced_verse_range[1]
        if forced_verse_set:
            return vid in forced_verse_set
        return False

    # Score each chapter file and collect verses that need retry
    retry_specs_by_chapter: dict[str, list[VerseRetrySpec]] = {}
    chapter_paths: dict[str, Path] = {}

    for cf in chapter_files:
        chapter_id = _chapter_id_from_path(cf)
        verse_scores = score_chapter_file(
            cf, source_verses, args.target_language, scoring_config
        )
        coverage_flagged = {
            spec.verse_id
            for spec in find_low_coverage_verses(cf, source_verses, args.min_unaligned_src)
        }
        specs = [
            VerseRetrySpec(
                verse_id=vs.verse_id,
                chapter_id=chapter_id,
                uncovered_src_ids=[],
                uncovered_count=0,
            )
            for vs in verse_scores
            if vs.needs_retry or vs.verse_id in coverage_flagged or _is_forced(vs.verse_id)
        ]
        if specs:
            retry_specs_by_chapter[chapter_id] = specs
            chapter_paths[chapter_id] = cf

    total_flagged = sum(len(s) for s in retry_specs_by_chapter.values())

    if not retry_specs_by_chapter:
        print("\n  No verses flagged — nothing to retry.")
        return

    print(f"\n  {total_flagged} verse(s) flagged across {len(retry_specs_by_chapter)} chapter(s):")
    for chapter_id in sorted(retry_specs_by_chapter):
        for spec in retry_specs_by_chapter[chapter_id]:
            print(f"    {spec.verse_id}")

    if args.dry_run:
        return

    llm_client = LLMClient(
        provider=args.llm_provider,
        model=args.llm_model,
        reasoning_effort=args.reasoning_effort,
        max_api_retries=args.max_api_retries,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
    )

    if args.batch_mode == "async":
        _run_async(
            args, corpus_id, source_verses, target_verses,
            retry_specs_by_chapter, llm_client,
        )
    else:
        _run_sync(
            args, corpus_id, source_verses, target_verses,
            retry_specs_by_chapter, chapter_paths, llm_client,
        )

    if args.llm_provider == "openrouter" and llm_client.session_cost:
        print(f"\nOpenRouter session cost: ${llm_client.session_cost:.4f}")


def _run_sync(
    args: argparse.Namespace,
    corpus_id: str,
    source_verses: dict,
    target_verses: dict,
    retry_specs_by_chapter: dict[str, list[VerseRetrySpec]],
    chapter_paths: dict[str, Path],
    llm_client: LLMClient,
) -> None:
    total_replaced = 0
    total_errors: list[str] = []

    for chapter_id in sorted(retry_specs_by_chapter):
        specs = retry_specs_by_chapter[chapter_id]
        chapter_path = chapter_paths[chapter_id]
        print(f"\n  Chapter {chapter_id}: retrying {len(specs)} verse(s) ...")

        n_replaced, errors = retry_chapter_sync(
            chapter_json_path=chapter_path,
            retry_specs=specs,
            source_verses=source_verses,
            target_verses=target_verses,
            target_language=args.target_language,
            llm_client=llm_client,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
            corpus_id=corpus_id,
            target_edition=args.target_edition,
            creator=args.creator,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            reasoning_effort=args.reasoning_effort,
        )
        print(f"  → {chapter_path.name}: {n_replaced} verse(s) replaced")

        if errors:
            print(f"    {len(errors)} validation error(s):")
            for err in errors[:5]:
                print(f"      {err}")
            if len(errors) > 5:
                print(f"      ... and {len(errors) - 5} more")

        total_replaced += n_replaced
        total_errors.extend(errors)

    print(
        f"\n  Total: {total_replaced} verse(s) replaced "
        f"across {len(retry_specs_by_chapter)} chapter(s)"
    )
    if total_errors:
        print(f"  {len(total_errors)} total validation error(s)")


def _run_async(
    args: argparse.Namespace,
    corpus_id: str,
    source_verses: dict,
    target_verses: dict,
    retry_specs_by_chapter: dict[str, list[VerseRetrySpec]],
    llm_client: LLMClient,
) -> None:
    from .async_batch import submit_batch_job

    chapter_batches = build_retry_chapter_batches(
        retry_specs_by_chapter=retry_specs_by_chapter,
        source_verses=source_verses,
        target_verses=target_verses,
        target_language=args.target_language,
        batch_size=args.batch_size,
        corpus_id=corpus_id,
    )

    print(f"\n  Submitting {len(chapter_batches)} request(s) to {args.llm_provider} batch API ...")

    job_metadata_base = {
        "job_type": "retry",
        "target_edition": args.target_edition,
        "target_language": args.target_language,
        "corpus": args.corpus,
        "corpus_id": corpus_id,
        "output_dir": str(args.alignment_dir),
        "creator": args.creator,
        "sources_dir": str(args.sources_dir),
        "target_tsv_dir": str(args.target_tsv_dir),
    }

    job_id, meta_path = submit_batch_job(
        provider=args.llm_provider,
        model=args.llm_model,
        reasoning_effort=args.reasoning_effort,
        chapter_batches=chapter_batches,
        jobs_dir=args.jobs_dir,
        job_metadata_base=job_metadata_base,
        temperature=llm_client.temperature,
        max_output_tokens=llm_client.max_output_tokens,
    )

    print(f"  Submitted: {job_id}")
    print(f"  Job metadata: {meta_path}")
    print(f"  Retrieve and merge results with: fetch-batch {meta_path}")


if __name__ == "__main__":
    main()
