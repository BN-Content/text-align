"""refine-alignment: LLM-assisted Stage 2 alignment refinement.

Takes automated alignment candidates (ACAI, SIM-MIGRATED, DIFF-MIGRATED) for a
target edition, presents each verse to an LLM with source and target tokens, and
produces a refined alignment JSON applying alignment-principles guidelines.

CLI entry point: refine-alignment
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from biblelib.word import BCVWPID

from text_align import ROOT
from text_align.config import load_config_from_args, require
from text_align.migrate.alignment_io import load_alignment_json, write_alignment_json
from text_align.migrate.tsv import process_usfm_tsv

from .llm import LLMClient
from .prompt import build_batch_message, build_system_prompt, detect_phenomena
from .source import load_source_verses


ALIGNMENT_SOURCE_TYPES = ["ACAI", "SIM-MIGRATED", "DIFF-MIGRATED"]

_SOURCES_DIR = ROOT / "data" / "sources"


# ---------------------------------------------------------------------------
# Candidate loading
# ---------------------------------------------------------------------------

def load_candidate_records(path: Path) -> dict[str, list[dict]]:
    """Load an alignment JSON and return records grouped by verse BCV ID.

    Handles both flat (``alignment["records"]``) and SB 0.4 grouped
    (``alignment["groups"][*]["records"]``) formats transparently.
    """
    data = load_alignment_json(path)
    if "records" in data:
        raw_records = data["records"]
    elif "groups" in data:
        raw_records = []
        for group in data.get("groups", []):
            raw_records.extend(group.get("records", []))
    else:
        return {}

    verses: dict[str, list[dict]] = {}
    for rec in raw_records:
        src_ids = rec.get("source") or []
        if not src_ids:
            continue
        verse_id = BCVWPID(src_ids[0]).to_bcvid
        verses.setdefault(verse_id, []).append({
            "source": src_ids,
            "target": rec.get("target") or [],
        })
    return verses


# ---------------------------------------------------------------------------
# Output building
# ---------------------------------------------------------------------------

def build_output_alignment(
    records: list[dict],
    corpus: str,
    edition: str,
    creator: str,
) -> dict[str, Any]:
    """Build an SB 0.4 groups alignment structure from LLM-refined records.

    NEQ records (``meta.rel == "NEQ"``) are separated from regular records and
    their token IDs are written into ``meta.nonEquivalent`` at the group level.
    The output file contains no ``meta.rel`` fields.
    """
    neq_source: list[str] = []
    neq_target: list[str] = []
    regular: list[dict] = []

    for rec in records:
        meta = rec.get("meta") or {}
        if meta.get("rel") == "NEQ":
            neq_source.extend(rec.get("source") or [])
            neq_target.extend(rec.get("target") or [])
        else:
            clean_meta: dict = {}
            secondary = meta.get("secondary") or {}
            sec_src = secondary.get("source") or []
            sec_tgt = secondary.get("target") or []
            if sec_src or sec_tgt:
                clean_meta["secondary"] = {}
                if sec_src:
                    clean_meta["secondary"]["source"] = sec_src
                if sec_tgt:
                    clean_meta["secondary"]["target"] = sec_tgt
            if meta.get("is_idiom"):
                clean_meta["is_idiom"] = True

            out_rec: dict = {
                "source": rec.get("source") or [],
                "target": rec.get("target") or [],
            }
            if clean_meta:
                out_rec["meta"] = clean_meta
            regular.append(out_rec)

    group_meta: dict = {"creator": creator, "conformsTo": "0.4"}
    if neq_source or neq_target:
        non_equiv: dict = {}
        if neq_source:
            non_equiv["source"] = neq_source
        if neq_target:
            non_equiv["target"] = neq_target
        group_meta["nonEquivalent"] = non_equiv

    return {
        "format": "alignment",
        "version": "0.4",
        "groups": [{
            "type": "translation",
            "meta": group_meta,
            "documents": [
                {"scheme": "BCVWP", "docid": corpus},
                {"scheme": "BCVWP", "docid": edition},
            ],
            "roles": ["source", "target"],
            "records": regular,
        }],
    }


# ---------------------------------------------------------------------------
# Per-corpus processing
# ---------------------------------------------------------------------------

def process_corpus(
    corpus: str,
    target_edition: str,
    target_language: str,
    target_tsv_dir: Path,
    exp_dir: Path,
    output_dir: Path,
    sources_dir: Path,
    alignment_sources: list[str],
    llm_client: LLMClient,
    batch_size: int,
    max_retries: int,
    creator: str,
    single_verse: str | None = None,
) -> None:
    """Process one corpus (``"nt"`` or ``"ot"``) and write its output JSON."""
    corpus_id = "SBLGNT" if corpus == "nt" else "WLCM"
    print(f"\n--- {corpus.upper()} ({corpus_id}) ---")

    print(f"Loading source tokens ({corpus_id}) ...")
    source_verses = load_source_verses(sources_dir, corpus)

    print(f"Loading target tokens ({target_edition}) ...")
    target_verses = process_usfm_tsv(target_tsv_dir, target_edition)

    # Load candidate alignments for each requested source type
    candidates_by_type: dict[str, dict[str, list[dict]]] = {}
    for src_type in alignment_sources:
        path = exp_dir / src_type / f"{corpus_id}-{target_edition}-manual.json"
        if path.exists():
            print(f"Loading candidates: {path.name}")
            candidates_by_type[src_type] = load_candidate_records(path)
        else:
            print(f"Candidate file not found, skipping: {path}")

    if not candidates_by_type:
        print("No candidate files found — skipping corpus.")
        return

    # Universe of verse IDs: present in candidates AND in source tokens
    candidate_ids: set[str] = set()
    for recs in candidates_by_type.values():
        candidate_ids.update(recs.keys())
    verse_ids = sorted(candidate_ids & set(source_verses.keys()))

    if single_verse:
        verse_ids = [single_verse] if single_verse in verse_ids else []
        if not verse_ids:
            print(f"Verse {single_verse} not found in candidate set — skipping.")
            return

    total_batches = (len(verse_ids) + batch_size - 1) // batch_size
    print(f"Processing {len(verse_ids)} verses in {total_batches} batch(es) ...")

    all_records: list[dict] = []
    all_errors: list[str] = []

    for batch_num, batch_start in enumerate(range(0, len(verse_ids), batch_size), 1):
        batch_ids = verse_ids[batch_start:batch_start + batch_size]

        verse_batch = []
        verse_source_ids: dict[str, set[str]] = {}
        verse_target_ids: dict[str, set[str]] = {}

        for verse_id in batch_ids:
            src_tokens = source_verses.get(verse_id, [])
            tgt_verse  = target_verses.get(verse_id)
            tgt_tokens = list(tgt_verse.words.values()) if tgt_verse else []

            cands = {
                src_type: recs[verse_id]
                for src_type, recs in candidates_by_type.items()
                if verse_id in recs
            }
            verse_source_ids[verse_id] = {t.id for t in src_tokens}
            verse_target_ids[verse_id] = {t.id for t in tgt_tokens}
            verse_batch.append((verse_id, src_tokens, tgt_tokens, cands))

        all_src = [t for _, src, _, _ in verse_batch for t in src]
        phenomena   = detect_phenomena(all_src)
        system_msg  = build_system_prompt(phenomena, target_language)
        user_msg    = build_batch_message(verse_batch, target_language)

        results, errors = llm_client.call_batch(
            system_prompt=system_msg,
            user_message=user_msg,
            verse_source_ids=verse_source_ids,
            verse_target_ids=verse_target_ids,
            max_retries=max_retries,
        )

        n_records = sum(len(r) for r in results.values())
        status = f"{len(results)}/{len(batch_ids)} verses, {n_records} records"
        if errors:
            status += f", {len(errors)} error(s)"
        print(f"  Batch {batch_num}/{total_batches}: {status}")

        for recs in results.values():
            all_records.extend(recs)
        all_errors.extend(errors)

    # Write output
    output   = build_output_alignment(all_records, corpus_id, target_edition, creator)
    group    = output["groups"][0]
    n_reg    = len(group["records"])
    neq      = group["meta"].get("nonEquivalent") or {}
    n_neq_s  = len(neq.get("source", []))
    n_neq_t  = len(neq.get("target", []))

    out_path = output_dir / f"{corpus_id}-{target_edition}-manual.json"
    write_alignment_json(output, out_path)

    print(f"\n  → {out_path}")
    print(f"     {n_reg} records | "
          f"NEQ source: {n_neq_s} | NEQ target: {n_neq_t}")
    if all_errors:
        print(f"     {len(all_errors)} unresolved validation error(s):")
        for err in all_errors[:10]:
            print(f"       {err}")
        if len(all_errors) > 10:
            print(f"       ... and {len(all_errors) - 10} more")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    # Pre-parse --output-suffix so load_config_from_args can derive output_dir
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--output-suffix", default="LLM-REFINED")
    pre_args, _ = pre.parse_known_args()

    config_defaults = load_config_from_args(output_suffix=pre_args.output_suffix)

    p = argparse.ArgumentParser(
        description=(
            "Refine alignment candidates with an LLM, applying alignment-principles "
            "guidelines (primary/secondary, idiom flags, NEQ)."
        )
    )
    p.add_argument("--config", metavar="NAME",
                   help="Load defaults from configs/<NAME>.yaml (CLI args override)")
    p.add_argument("--target-language", default=None,
                   help="ISO 639-3 language code, e.g. eng")
    p.add_argument("--target-edition", default=None,
                   help="Target edition ID, e.g. NIV11")
    p.add_argument("--target-tsv-dir", default=None, type=Path,
                   help="Directory containing ot_<edition>.tsv and nt_<edition>.tsv")
    p.add_argument("--output-dir", default=None, type=Path,
                   help="Directory to write refined alignment JSON files")
    p.add_argument("--output-suffix", default="LLM-REFINED",
                   help="Output subdirectory name under exp/ (default: LLM-REFINED)")
    p.add_argument("--sources-dir", default=_SOURCES_DIR, type=Path,
                   help="Directory containing SBLGNT.tsv and WLCM.tsv "
                        f"(default: {_SOURCES_DIR})")
    p.add_argument("--alignment-sources", default=None, nargs="+",
                   choices=ALIGNMENT_SOURCE_TYPES,
                   help=f"Candidate types to load (default: all — "
                        f"{', '.join(ALIGNMENT_SOURCE_TYPES)})")
    p.add_argument("--corpora", default=["ot", "nt"], nargs="+", choices=["ot", "nt"],
                   help="Corpora to process (default: ot nt)")
    p.add_argument("--llm-provider", default="openai", choices=["openai", "anthropic"],
                   help="LLM provider (default: openai)")
    p.add_argument("--llm-model", default="gpt-5.4-mini",
                   help="Model name for the chosen provider (default: gpt-5.4-mini)")
    p.add_argument("--batch-size", type=int, default=5,
                   help="Verses per LLM call (default: 5)")
    p.add_argument("--max-retries", type=int, default=2,
                   help="Retry attempts on validation failure (default: 2)")
    p.add_argument("--verse", default=None, metavar="BCV",
                   help="Process a single verse BCV for testing, e.g. 41004003")
    p.add_argument("--creator", default="text-align",
                   help="Creator string for alignment meta (default: text-align)")

    p.set_defaults(**config_defaults)
    args = p.parse_args()
    require(args, "target_language", "target_edition", "target_tsv_dir", "output_dir")

    if args.alignment_sources is None:
        args.alignment_sources = ALIGNMENT_SOURCE_TYPES

    return args


def main() -> None:
    args = parse_args()

    # output_dir = exp_dir / output_suffix; recover exp_dir for candidate lookup
    exp_dir = args.output_dir.parent

    print(f"refine-alignment: {args.target_edition} ({args.target_language})")
    print(f"  Provider:  {args.llm_provider} / {args.llm_model}")
    print(f"  Sources:   {', '.join(args.alignment_sources)}")
    print(f"  Output:    {args.output_dir}")
    if args.verse:
        print(f"  Verse:     {args.verse} (single-verse mode)")

    llm_client = LLMClient(provider=args.llm_provider, model=args.llm_model)

    for corpus in args.corpora:
        process_corpus(
            corpus=corpus,
            target_edition=args.target_edition,
            target_language=args.target_language,
            target_tsv_dir=args.target_tsv_dir,
            exp_dir=exp_dir,
            output_dir=args.output_dir,
            sources_dir=args.sources_dir,
            alignment_sources=args.alignment_sources,
            llm_client=llm_client,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
            creator=args.creator,
            single_verse=args.verse,
        )


if __name__ == "__main__":
    main()
