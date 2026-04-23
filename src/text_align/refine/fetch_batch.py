"""fetch-batch: retrieve and write results from an async refine-alignment batch job.

Usage:
    fetch-batch <job-metadata-file>          # fetch once; error if not complete
    fetch-batch <job-metadata-file> --poll   # print status and exit
    fetch-batch <job-metadata-file> --wait   # block until done (with sleep loop)

CLI entry point: fetch-batch
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from text_align.migrate.alignment_io import write_alignment_json
from text_align.migrate.tsv import process_usfm_tsv

from .async_batch import load_job_metadata, retrieve_google
from .refine import build_output_alignment
from .source import load_source_verses

_GOOGLE_SUCCEEDED = "JOB_STATE_SUCCEEDED"
_GOOGLE_FAILED = "JOB_STATE_FAILED"
_GOOGLE_CANCELLED = "JOB_STATE_CANCELLED"
_GOOGLE_TERMINAL = {_GOOGLE_SUCCEEDED, _GOOGLE_FAILED, _GOOGLE_CANCELLED}


def _fetch_google(job_meta: dict, poll_only: bool, wait: bool, wait_interval: int) -> None:
    from google import genai

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    job_name = job_meta["job_name"]

    job = client.batches.get(name=job_name)
    state = job.state.name

    if poll_only:
        print(f"Job {job_name}: {state}")
        return

    if state not in _GOOGLE_TERMINAL:
        if wait:
            print(f"  Job {job_name}: {state} — waiting ...")
            while state not in _GOOGLE_TERMINAL:
                time.sleep(wait_interval)
                job = client.batches.get(name=job_name)
                state = job.state.name
                print(f"  Job {job_name}: {state}")
        else:
            raise SystemExit(
                f"Job {job_name} is not complete (state: {state}). "
                f"Use --wait to block or --poll to check status."
            )

    if state in {_GOOGLE_FAILED, _GOOGLE_CANCELLED}:
        raise SystemExit(f"Job {job_name} ended with state: {state}")

    print(f"Job {job_name}: {state} — retrieving results ...")

    # Re-load source/target tokens for validation
    sources_dir = Path(job_meta["sources_dir"])
    target_tsv_dir = Path(job_meta["target_tsv_dir"])
    target_edition = job_meta["target_edition"]
    corpus = job_meta["corpus"]

    print(f"  Loading source tokens ({job_meta['corpus_id']}) ...")
    source_verses = load_source_verses(sources_dir, corpus)
    print(f"  Loading target tokens ({target_edition}) ...")
    target_verses = process_usfm_tsv(target_tsv_dir, target_edition)

    # Build verse ID sets for every verse covered by the job
    all_verse_ids: set[str] = set()
    for req in job_meta["requests"]:
        all_verse_ids.update(req["verse_ids"])

    verse_source_ids = {
        vid: {t.id for t in source_verses.get(vid, [])}
        for vid in all_verse_ids
    }
    verse_target_ids = {
        vid: {t.id for t in (
            list(target_verses[vid].words.values()) if vid in target_verses else []
        )}
        for vid in all_verse_ids
    }

    chapter_results, errors, san_details = retrieve_google(
        genai_client=client,
        job_name=job_name,
        requests_meta=job_meta["requests"],
        verse_source_ids=verse_source_ids,
        verse_target_ids=verse_target_ids,
    )

    # Write chapter output files
    output_dir = Path(job_meta["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    corpus_id = job_meta["corpus_id"]
    creator = job_meta.get("creator", "text-align")
    llm_provider = job_meta.get("provider")
    llm_model = job_meta.get("model")
    reasoning_effort = job_meta.get("reasoning_effort")

    total_records = 0
    for chapter_id in sorted(chapter_results):
        verse_results = chapter_results[chapter_id]
        records = [rec for recs in verse_results.values() for rec in recs]

        book_id = chapter_id[:2]
        chap_num = chapter_id[2:]
        out_path = output_dir / f"{corpus_id}-{target_edition}-{book_id}-{chap_num}-manual.json"

        output = build_output_alignment(
            records, corpus_id, target_edition, creator,
            llm_provider=llm_provider,
            llm_model=llm_model,
            reasoning_effort=reasoning_effort,
        )
        write_alignment_json(output, out_path)
        n_neq = sum(1 for r in records if (r.get("meta") or {}).get("rel") == "NEQ")
        print(f"  → {out_path.name}  ({len(records)} records, {n_neq} NEQ)")
        total_records += len(records)

    n_chapters = len(chapter_results)
    print(f"\n  {total_records} records across {n_chapters} chapter(s) written to {output_dir}")

    if san_details:
        print(f"  {len(san_details)} record(s) sanitized:")
        for detail in san_details[:20]:
            print(f"    {detail}")
        if len(san_details) > 20:
            print(f"    ... and {len(san_details) - 20} more")

    if errors:
        print(f"  {len(errors)} validation error(s):")
        for err in errors[:20]:
            print(f"    {err}")
        if len(errors) > 20:
            print(f"    ... and {len(errors) - 20} more")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Retrieve and write results from an async refine-alignment batch job."
    )
    p.add_argument("job_file", type=Path,
                   help="Path to the job metadata JSON file written by refine-alignment "
                        "(e.g. jobs/google/batches_abc123.json)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--poll", action="store_true",
                      help="Print job status and exit (no-op if not yet done)")
    mode.add_argument("--wait", action="store_true",
                      help="Block until the job is complete, polling periodically")
    p.add_argument("--wait-interval", type=int, default=60, metavar="SECONDS",
                   help="Seconds between status checks when --wait is active (default: 60)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.job_file.exists():
        raise SystemExit(f"Job metadata file not found: {args.job_file}")

    job_meta = load_job_metadata(args.job_file)
    provider = job_meta.get("provider", "")

    if provider == "google":
        _fetch_google(job_meta, poll_only=args.poll, wait=args.wait, wait_interval=args.wait_interval)
    else:
        raise SystemExit(
            f"Provider {provider!r} batch retrieval is not yet implemented. "
            f"Only 'google' is currently supported."
        )


if __name__ == "__main__":
    main()
