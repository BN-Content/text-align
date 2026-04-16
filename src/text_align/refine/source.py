"""Load SBLGNT / WLCM source TSVs into verse-keyed token lists for refine-alignment."""

from pathlib import Path

from biblelib.word import BCVWPID

from text_align.burrito.source import Source, SourceReader


def load_source_verses(sources_dir: Path | str, corpus: str) -> dict[str, list[Source]]:
    """Load a source TSV and return a dict of BCV ID → ordered list of Source tokens.

    Uses the same BCV key format as ``process_usfm_tsv`` so source and target
    verse dicts can be looked up with the same key.

    Args:
        sources_dir: Directory containing SBLGNT.tsv and WLCM.tsv.
        corpus: ``"nt"`` for SBLGNT, ``"ot"`` for WLCM.
    """
    sources_dir = Path(sources_dir)
    filename = "SBLGNT.tsv" if corpus == "nt" else "WLCM.tsv"
    reader = SourceReader(sources_dir / filename)

    verses: dict[str, list[Source]] = {}
    for token in reader.values():
        bcv = BCVWPID(token.id).to_bcvid
        verses.setdefault(bcv, []).append(token)

    # Ensure tokens are in canonical word-position order within each verse
    return {bcv: sorted(tokens, key=lambda t: t.id) for bcv, tokens in verses.items()}
