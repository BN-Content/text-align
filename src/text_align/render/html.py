"""render-alignment: generate per-chapter HTML alignment visualizations.

Produces one HTML file per chapter, showing each target token paired with
its aligned source token(s) and subscript word-position indices.  Optionally
annotates tokens that belong to ACAI entities.

CLI entry point: ``render-alignment``
"""

from __future__ import annotations

import argparse
import datetime
import os
from dataclasses import dataclass
from pathlib import Path

import regex as re
from biblelib.word import BCVID, BCVWPID

from text_align.burrito import AlignmentSet, Manager
from text_align.config import load_config_from_args, require
from text_align.align.acai_common import (
    ACAI_TYPES,
    AcaiEntity,
    build_word_entity_map,
    load_acai_entities,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AlignmentToken:
    """Target-keyed alignment unit: maps target token IDs to text, plus source map."""
    targets: dict[str, str]
    sources: dict[str, str]


# ---------------------------------------------------------------------------
# Source/target text lookup helpers
# ---------------------------------------------------------------------------

def get_source_text(source_id: str, sources: list) -> str:
    for src in sources:
        if src.id == source_id:
            return src.text
    print(f"Could not find source for {source_id}")
    return ""


def get_alignment_sources(source_selectors: list[str], sources: list) -> dict[str, str]:
    return {sel: get_source_text(sel, sources) for sel in sorted(source_selectors)}


def get_alignment_targets(target_selectors: list[str], targets: list) -> dict[str, str]:
    return {t.id: t.text for t in targets if t.id in target_selectors}


def get_sources_with_targets(records: dict) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for bcv_records in records.values():
        for alignment in bcv_records:
            for source_id in alignment.source_selectors:
                result[source_id] = alignment.target_selectors
    return result


# ---------------------------------------------------------------------------
# HTML structure helpers
# ---------------------------------------------------------------------------

def _html_open(is_r2l: bool) -> str:
    if is_r2l:
        return "<html dir='r2l'>\n<style>body {direction: rtl;}</style><head>\n<meta charset=\"utf-8\">\n"
    return "<html>\n<head>\n<meta charset=\"utf-8\">\n"


def start_new_chapter(
    html_out, bcv: BCVWPID, viz_path: Path, is_r2l: bool, iso_date: str
) -> object:
    if html_out is not None:
        html_out.close()
    chapter_file = viz_path / f"{bcv.book_ID}-{bcv.chapter_ID}.html"
    html_out = open(chapter_file, "w", encoding="utf-8")
    usfm_ref = re.sub(r":[0-9]+$", "", bcv.to_usfm())
    html_out.write(_html_open(is_r2l))
    html_out.write(f"<title>{usfm_ref} ({bcv.book_ID}-{bcv.chapter_ID})</title>\n</head>\n<body>\n")
    html_out.write(f"<h1>{usfm_ref}</h1>\n<p><b>Version: {iso_date}</b></p>\n<div class=\"chapter\">\n")
    html_out = start_new_verse(html_out, bcv, is_r2l)
    return html_out


def start_new_verse(html_out, bcv: BCVWPID, is_r2l: bool) -> object:
    usfm = bcv.to_usfm()
    if is_r2l:
        html_out.write(f"<p style='display: block; direction: r2l' dir='r2l'><b>{usfm}:</b>&nbsp;\n")
    else:
        html_out.write(f"<p style='display: block'><b>{usfm}:</b>&nbsp;\n")
    return html_out


def end_verse(html_out) -> None:
    html_out.write("\n</div></p><!-- verse -->\n")


def end_chapter(html_out, level: str = "chapter") -> None:
    end_verse(html_out)
    html_out.write(f"</div><!-- {level} -->\n</body></html>\n")


# ---------------------------------------------------------------------------
# Unit processing
# ---------------------------------------------------------------------------

def process_units(units: list[AlignmentToken], target_id: str, units_in_verse: dict) -> None:
    """Accumulate the unit for *target_id* into *units_in_verse*."""
    for unit in units:
        for tid in unit.targets:
            if target_id in units_in_verse["used_targets"]:
                continue
            if tid != target_id:
                continue
            units_in_verse["used_targets"].append(target_id)
            unit_entry: dict = {
                "target text": unit.targets[tid],
                "sources": [],
                "source_texts": [],
                "source_indexes": [],
            }
            target_bcv = BCVWPID(target_id)
            for source_id, source_text in unit.sources.items():
                source_bcv = BCVWPID(source_id)
                unit_entry["source_texts"].append(source_text)
                unit_entry["sources"].append(source_id)
                if int(source_bcv.book_ID) > 39:
                    # NT: simple word index
                    if source_bcv.to_bcvid == target_bcv.to_bcvid:
                        unit_entry["source_indexes"].append(str(int(source_bcv.word_ID)))
                    else:
                        unit_entry["source_indexes"].append(
                            f"{int(source_bcv.verse_ID)}.{int(source_bcv.word_ID)}"
                        )
                else:
                    # OT: word.part index
                    if source_bcv.to_bcvid == target_bcv.to_bcvid:
                        unit_entry["source_indexes"].append(
                            f"{int(source_bcv.word_ID)}.{int(source_bcv.part_ID)}"
                        )
                    else:
                        unit_entry["source_indexes"].append(
                            f"{int(source_bcv.verse_ID)}.{int(source_bcv.word_ID)}.{int(source_bcv.part_ID)}"
                        )
            if not unit.sources:
                if re.search(r"\w", unit.targets[tid].strip()):
                    unit_entry["source_texts"].append("&bull;")
                else:
                    unit_entry["source_texts"].append("&nbsp;")
            units_in_verse["units"].append(unit_entry)


def get_unused_verse_sources(units_in_verse: dict, verse_sources: list) -> dict[str, str]:
    used_sources: list[str] = []
    for unit in units_in_verse["units"]:
        used_sources.extend(unit["sources"])
    return {src.id: src.text for src in verse_sources if src.id not in used_sources}


def get_source_verse(units: list[dict], fallback: str | None = None) -> str | None:
    for unit in units:
        if unit["sources"]:
            return BCVWPID(unit["sources"][0]).to_bcvid
    return fallback


def write_units_in_verse(
    html_out,
    units_in_verse: dict,
    unused_source_ids: dict[str, str],
    is_r2l: bool,
    acai_entities: dict[str, list[AcaiEntity]] | None = None,
    sources_with_targets: dict | None = None,
    tag_acai: bool = False,
) -> None:
    if acai_entities is None:
        acai_entities = {}
    if sources_with_targets is None:
        sources_with_targets = {}

    cells: list[dict] = []

    for unit in units_in_verse["units"]:
        cell: dict = {"html": [], "sources": list(unit["sources"])}
        cell["html"].append("<div style='display: inline-block; padding: 1 1 1 1'>")

        # target text row
        if not tag_acai or not unit["sources"]:
            cell["html"].append(f"<div>{unit['target text']} </div>")
        else:
            has_acai = any(sid in acai_entities for sid in unit["sources"])
            if has_acai:
                acai_span = "".join(
                    f" {ae.id}"
                    for sid in unit["sources"]
                    for ae in acai_entities.get(sid, [])
                )
                cell["html"].append(
                    f"<div style='background: lightblue'>{unit['target text']}"
                    f"<span style='font-size: 60%; font-family: Arial; line-height: 2; "
                    f"border-radius: 0.35em; text-transform: uppercase; vertical-align: super; "
                    f"margin-left: 0.5rem; direction: l2r' dir='l2r'>{acai_span}</span> </div>"
                )
            else:
                cell["html"].append(f"<div>{unit['target text']} </div>")

        # source row
        if unit["sources"]:
            if unit["sources"][0] in units_in_verse["used_sources"]:
                # back-reference arrow
                indexes = " ".join(unit["source_indexes"])
                arrow = "&rarr;" if is_r2l else "&larr;"
                cell["html"].append(f"<div><sub style='font-size: 60%'>{indexes}</sub>&nbsp;{arrow}</div>")
            else:
                units_in_verse["used_sources"].extend(unit["sources"])
                if unit["source_indexes"]:
                    parts = [
                        f"{unit['source_texts'][i]}<sub style='font-size: 60%'>{unit['source_indexes'][i]}</sub>"
                        for i in range(len(unit["sources"]))
                    ]
                    cell["html"].append(f"<div>{'&nbsp;'.join(parts)}</div>")
                else:
                    cell["html"].append(f"<div>{unit['source_texts'][0]}</div>")
        else:
            cell["html"].append(f"<div style='text-align: center'>{unit['source_texts'][0]}</div>")

        cell["html"].append("</div>")
        cells.append(cell)

    # insert unaligned source tokens in order
    if units_in_verse["used_targets"]:
        target_bcvid = BCVWPID(units_in_verse["used_targets"][0])
    else:
        target_bcvid = None

    for unused_id in sorted(unused_source_ids, reverse=True):
        if unused_id in sources_with_targets:
            continue
        source_bcvid = BCVWPID(unused_id)
        if target_bcvid and target_bcvid.to_bcvid != source_bcvid.to_bcvid:
            continue

        word_idx = int(source_bcvid.word_ID)
        bullet_cell: dict = {"html": [], "sources": [unused_id]}
        bullet_cell["html"].append("<div style='display: inline-block; padding: 1 1 1 1'>")
        bullet_cell["html"].append("<div style='text-align: center'>&bull;</div>")
        if int(source_bcvid.book_ID) > 39:
            bullet_cell["html"].append(
                f"<div>{unused_source_ids[unused_id]}<sub style='font-size: 60%'>{word_idx}</sub></div>"
            )
        else:
            part_idx = int(source_bcvid.part_ID)
            bullet_cell["html"].append(
                f"<div>{unused_source_ids[unused_id]}<sub style='font-size: 60%'>{word_idx}.{part_idx}</sub></div>"
            )
        bullet_cell["html"].append("</div>")

        # insert before the first cell with a numerically-adjacent source
        insert_before = cells[-1] if cells else None
        for cell in cells:
            for used_id in cell["sources"]:
                try:
                    diff = int(used_id) - int(unused_id)
                    bcv_u = BCVWPID(used_id)
                    bcv_n = BCVWPID(unused_id)
                    if diff == 1:
                        insert_before = cell
                        break
                    if int(bcv_u.word_ID) == int(bcv_n.word_ID) and int(bcv_u.part_ID) - int(bcv_n.part_ID) == 1:
                        insert_before = cell
                        break
                except (ValueError, AttributeError):
                    pass
        if insert_before is not None:
            cells.insert(cells.index(insert_before), bullet_cell)
        else:
            cells.append(bullet_cell)

    for cell in cells:
        for line in cell["html"]:
            html_out.write(line)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    config_defaults = load_config_from_args(output_suffix="viz", output_in_exp=False)

    p = argparse.ArgumentParser(
        description="Render alignment data as per-chapter HTML visualizations."
    )
    p.add_argument("--config", metavar="NAME",
                   help="Load defaults from configs/<NAME>.yaml (CLI args override)")
    p.add_argument("--alignment-lang", default=None,
                   help="ISO 639-3 language code for the target translation, e.g. spa")
    p.add_argument("--alignment-edition", default=None,
                   help="Target edition ID, e.g. BONBV")
    p.add_argument("--lang-data-path", default=None, type=Path,
                   help="Root data/ directory for the target language alignment repo")
    p.add_argument("--output-dir", default=None, type=Path,
                   help="Root directory to write HTML output files")
    p.add_argument("--acai-data-dir", default=None, type=Path,
                   help="Path to ACAI root directory (omit to disable ACAI annotations)")
    p.add_argument("--acai-types", nargs="+", default=ACAI_TYPES,
                   help=f"ACAI entity types to load (default: {ACAI_TYPES})")
    p.add_argument("--include-acai-pronominals", action="store_true",
                   help="Include pronominal referents in ACAI entity data")
    p.add_argument("--r2l", action="store_true",
                   help="Target language is right-to-left")
    p.set_defaults(**config_defaults)
    args = p.parse_args()
    require(args, "alignment_lang", "alignment_edition", "lang_data_path", "output_dir")
    return args


def main() -> None:
    args = parse_args()
    iso_date = datetime.datetime.now().isoformat().split("T")[0]
    is_r2l = args.r2l
    tag_acai = args.acai_data_dir is not None

    print("Loading AlignmentSets ...")
    alset_ot = AlignmentSet(
        targetlanguage=args.alignment_lang,
        targetid=args.alignment_edition,
        sourceid="WLCM",
        langdatapath=args.lang_data_path,
    )
    alset_nt = AlignmentSet(
        targetlanguage=args.alignment_lang,
        targetid=args.alignment_edition,
        sourceid="SBLGNT",
        langdatapath=args.lang_data_path,
    )
    mgr_ot = Manager(alset_ot)
    mgr_nt = Manager(alset_nt)

    for mgr in (mgr_ot, mgr_nt):
        corpus = "ot" if mgr.alignmentset.sourceid == "WLCM" else "nt"
        print(f"\nRendering {corpus.upper()} — {mgr.alignmentset.sourceid}")

        acai_word_map: dict = {}
        if tag_acai and args.acai_data_dir is not None:
            raw_entities = load_acai_entities(
                args.acai_data_dir, args.acai_types, corpus,
                include_pronominals=args.include_acai_pronominals,
            )
            acai_word_map = build_word_entity_map(raw_entities)
            print(f"  ACAI word map: {len(acai_word_map)} entries")

        sources_with_targets = get_sources_with_targets(mgr.bcv["records"])

        # build aligned target_id → [AlignmentToken, ...] dict
        alignments: dict[str, list[AlignmentToken]] = {}
        for record_id, record in mgr.bcv["records"].items():
            sources = mgr.bcv["sources"].get(record_id, [])
            targets_source = mgr.bcv["target_sourceverses"].get(record_id)
            if targets_source is None:
                print(f"  No target_sourceverses for {record_id}, skipping")
                continue
            for alignment in record:
                al_sources = get_alignment_sources(alignment.source_selectors, sources)
                al_targets = get_alignment_targets(alignment.target_selectors, targets_source)
                token = AlignmentToken(targets=al_targets, sources=al_sources)
                for tid in token.targets:
                    alignments.setdefault(tid, []).append(token)
            for target in targets_source:
                if target.id not in alignments:
                    alignments[target.id] = [AlignmentToken(targets={target.id: target.text}, sources={})]

        target_ids = sorted(alignments)
        if not target_ids:
            print("  No targets, skipping.")
            continue

        viz_path = args.output_dir / f"{args.alignment_edition}/{mgr.alignmentset.sourceid}-{args.alignment_edition}"
        viz_path.mkdir(parents=True, exist_ok=True)

        html_out = None
        prev_bcvid = ""
        units_in_verse: dict = {"used_sources": [], "used_targets": [], "units": []}

        for target_id in target_ids:
            current_bcvid = BCVWPID(target_id).to_bcvid
            if current_bcvid != prev_bcvid:
                current_bcv = BCVWPID(target_id)
                if prev_bcvid == "":
                    # first verse
                    html_out = start_new_chapter(html_out, current_bcv, viz_path, is_r2l, iso_date)
                    prev_bcvid = current_bcvid
                else:
                    prev_bcv = BCVID(prev_bcvid)
                    src_verse = get_source_verse(units_in_verse["units"], current_bcvid)
                    unused = {}
                    if src_verse in mgr.bcv["sources"]:
                        unused = get_unused_verse_sources(units_in_verse, mgr.bcv["sources"][src_verse])
                    write_units_in_verse(
                        html_out, units_in_verse, unused, is_r2l,
                        acai_entities=acai_word_map, sources_with_targets=sources_with_targets,
                        tag_acai=tag_acai,
                    )

                    if current_bcv.book_ID != prev_bcv.book_ID or current_bcv.chapter_ID != prev_bcv.chapter_ID:
                        # new chapter (also covers new book)
                        end_chapter(html_out)
                        html_out = start_new_chapter(html_out, current_bcv, viz_path, is_r2l, iso_date)
                    else:
                        # new verse in same chapter
                        end_verse(html_out)
                        html_out = start_new_verse(html_out, current_bcv, is_r2l)
                    units_in_verse = {"used_sources": [], "used_targets": [], "units": []}

            process_units(alignments[target_id], target_id, units_in_verse)
            prev_bcvid = current_bcvid

        # flush final verse
        if html_out is not None:
            src_verse = get_source_verse(units_in_verse["units"])
            unused = {}
            if src_verse and src_verse in mgr.bcv["sources"]:
                unused = get_unused_verse_sources(units_in_verse, mgr.bcv["sources"][src_verse])
            write_units_in_verse(
                html_out, units_in_verse, unused, is_r2l,
                acai_entities=acai_word_map, sources_with_targets=sources_with_targets,
                tag_acai=tag_acai,
            )
            end_chapter(html_out, "book")

        print(f"  HTML written to {viz_path}")


if __name__ == "__main__":
    main()
