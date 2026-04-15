"""Alignment JSON I/O utilities for alignment migration."""

import json
from pathlib import Path
from typing import Any

import regex as re


def load_alignment_json(path: Path) -> dict[str, Any]:
    """Load an alignment JSON file and return the parsed dict."""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_alignment_json(alignment: dict[str, Any], path: Path) -> None:
    """Write alignment dict as JSON, one record per line, to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    json_string = json.dumps(alignment, ensure_ascii=False)
    # put "records" on its own line, then one record per line
    json_string = re.sub(r'"records"', r'\n"records"', json_string)
    json_string = re.sub(r"\}\}, ", "}},\n", json_string)
    path.write_text(json_string, encoding="utf-8")


def create_new_alignments(
    alignments: dict[str, Any],
    remap_target_ids: dict[str, str],
    corpus: str,
    edition: str,
    creator: str = "text-align",
) -> dict[str, Any]:
    """Build a new alignment JSON object by remapping target token IDs.

    *alignments* is the source alignment dict (parsed JSON).
    *remap_target_ids* maps old target IDs → new target IDs.
    *corpus* is the source corpus ID (e.g. ``"SBLGNT"`` or ``"WLCM"``).
    *edition* is the target edition ID (e.g. ``"NIrV"``).
    """
    used_new_targets: list[str] = []
    new_alignment: dict[str, Any] = {
        "documents": [
            {"docid": corpus, "scheme": "BCVWP"},
            {"docid": edition, "scheme": "BCVWP"},
        ],
        "meta": {
            "conformsTo": "0.4",
            "creator": creator,
        },
        "roles": ["source", "target"],
        "type": "translation",
        "records": [],
    }
    for alignment in alignments["records"]:
        if not alignment["source"] or not alignment["target"]:
            continue
        remapped_ids: list[str] = []
        for target_id in alignment["target"]:
            if target_id in remap_target_ids:
                new_id = remap_target_ids[target_id]
                if new_id not in used_new_targets:
                    remapped_ids.append(new_id)
                    used_new_targets.append(new_id)
        target_ids = sorted(set(remapped_ids))
        if not target_ids:
            continue
        meta = alignment["meta"]
        new_meta: dict[str, Any] = {"id": meta["id"]}
        if "source" in meta:
            new_meta["source"] = meta["source"]
        # handle old 'process' key (pre-0.2.1 data) as well as current 'origin'
        if "process" in meta:
            new_meta["origin"] = meta["process"]
        elif "origin" in meta:
            new_meta["origin"] = meta["origin"]
        new_meta["status"] = "created"
        new_alignment["records"].append({
            "source": alignment["source"],
            "target": target_ids,
            "meta": new_meta,
        })
    return new_alignment
