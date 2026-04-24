"""Core infrastructure for language-aware prompt assembly.

Block architecture
------------------
BASE_BLOCK is always included.  Conditional blocks are included only when the
corresponding phenomenon is detected in the verse batch by detect_phenomena().
Forced co-inclusions are applied regardless of direct detection (e.g. PASSIVE
always pulls in IMPERSONAL).

Language configs are registered via register_language() and looked up by ISO
639-3 code.  English ("eng") is registered automatically when this package is
imported.
"""

from dataclasses import dataclass, field

from text_align.burrito.source import Source
from text_align.migrate.models import MigrateTarget


# ---------------------------------------------------------------------------
# Language config
# ---------------------------------------------------------------------------

@dataclass
class LanguagePromptConfig:
    """All prompt content and assembly rules for one target language."""

    language_code: str
    base_block: str
    conditional_blocks: dict[str, str]
    block_order: list[str]
    forced_inclusions: dict[str, set[str]] = field(default_factory=dict)


_LANGUAGE_REGISTRY: dict[str, LanguagePromptConfig] = {}


def register_language(config: LanguagePromptConfig) -> None:
    _LANGUAGE_REGISTRY[config.language_code] = config


def get_language_config(language_code: str) -> LanguagePromptConfig:
    if language_code in _LANGUAGE_REGISTRY:
        return _LANGUAGE_REGISTRY[language_code]
    fallback = _LANGUAGE_REGISTRY.get("eng")
    if fallback is None:
        raise KeyError(
            f"No prompt config registered for '{language_code}' and no 'eng' fallback."
        )
    return fallback


# ---------------------------------------------------------------------------
# Phenomenon detection (Greek NT source tokens)
# ---------------------------------------------------------------------------

_IMPERSONAL_FORMS: frozenset[str] = frozenset({
    "δεῖ", "ἔξεστιν", "ἔξεστι", "πρέπει", "συμφέρει", "δοκεῖ",
})

_NEGATION_FORMS: frozenset[str] = frozenset({
    "οὐ", "οὐκ", "οὐχ", "οὐχί",
    "μή", "μήτε",
    "οὐδέ", "μηδέ",
    "οὐκέτι", "μηκέτι",
    "οὔπω", "μήπω",
    "οὔτε",
})


def detect_phenomena(source_tokens: list[Source]) -> set[str]:
    """Scan source token POS/morph fields and return a set of phenomenon tags.

    Tags correspond to keys in a LanguagePromptConfig's conditional_blocks.
    Forced co-inclusions are NOT applied here — that happens in
    build_system_prompt().
    """
    tags: set[str] = set()

    for t in source_tokens:
        morph = t.morph or ""
        text = t.text or ""
        lemma = t.lemma or ""

        if t.pos == "verb" and "-" in morph:
            tam = morph.split("-")[1]
            if len(tam) >= 3:
                voice = tam[-2]
                mood = tam[-1]
                if voice == "P":
                    tags.add("PASSIVE")
                if mood == "P":
                    tags.add("PARTICIPLE")
                if mood == "N":
                    tags.add("INFINITIVE")
                if tam[0] in "IXY":  # imperfect, perfect, pluperfect
                    tags.add("VERBAL_ASPECT")

        if t.pos in ("adj", "adv"):
            if morph.endswith("-C") or morph.endswith("-S"):
                tags.add("COMPARATIVE")

        if text == "ἵνα":
            tags.add("HINA")
        if text in ("εἰ", "ἐάν"):
            tags.add("CONDITIONAL")
        if text == "ὅτι":
            tags.add("HOTI")
        if lemma == "αὐτός" or text in (
            "αὐτός", "αὐτοῦ", "αὐτῷ", "αὐτόν",
            "αὐτή", "αὐτῆς", "αὐτῇ", "αὐτήν",
            "αὐτό", "αὐτοί", "αὐτῶν", "αὐτοῖς",
            "αὐτούς", "αὐταί", "αὐτάς",
        ):
            tags.add("AUTOS")
        if text in _IMPERSONAL_FORMS:
            tags.add("IMPERSONAL")
        if text in _NEGATION_FORMS:
            tags.add("NEGATION")

    return tags


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_system_prompt(phenomena: set[str], target_language: str = "eng") -> str:
    """Assemble the system prompt from the base block plus relevant conditional blocks.

    Args:
        phenomena: Tags returned by detect_phenomena().
        target_language: ISO 639-3 code (e.g. "eng").  Used to look up the
            registered LanguagePromptConfig.
    """
    config = get_language_config(target_language)

    expanded: set[str] = set(phenomena)
    for tag, forced in config.forced_inclusions.items():
        if tag in expanded:
            expanded |= forced

    blocks = [config.base_block]

    if expanded:
        active = [t for t in config.block_order if t in expanded]
        notice = (
            "The following constructions were identified in this verse batch. "
            "Specific guidelines for each are included below: "
            + ", ".join(active) + "."
        )
        blocks.append(notice)

    for tag in config.block_order:
        if tag in expanded:
            blocks.append(config.conditional_blocks[tag])

    return "\n\n---\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Token maps: local sequential numbers ↔ full token IDs
# ---------------------------------------------------------------------------

def build_verse_token_maps(
    source_tokens: list[Source],
    target_tokens: list[MigrateTarget],
) -> tuple[dict[int, str], dict[int, str]]:
    """Build local-number → full-ID maps for one verse (1-based).

    Returns (source_map, target_map).  Caller can build inverse maps with
    {v: k for k, v in source_map.items()} when needed for candidate formatting.
    """
    source_map = {i + 1: t.id for i, t in enumerate(source_tokens)}
    target_map = {i + 1: t.id for i, t in enumerate(target_tokens)}
    return source_map, target_map


def reverse_map_records(
    records: list[dict],
    source_map: dict[int, str],
    target_map: dict[int, str],
) -> tuple[list[dict], list[str]]:
    """Convert local integer token numbers in LLM records back to full token IDs.

    Returns (mapped_records, error_messages).  Records with unmappable numbers
    are included with whatever could be mapped; callers should inspect errors.
    """
    errors: list[str] = []
    mapped: list[dict] = []

    def _lookup(nums: list, id_map: dict[int, str], side: str, label: str) -> list[str]:
        result: list[str] = []
        for n in nums:
            try:
                key = int(n)
            except (TypeError, ValueError):
                errors.append(f"{label}: {side} value {n!r} is not an integer")
                continue
            full_id = id_map.get(key)
            if full_id is None:
                errors.append(
                    f"{label}: {side} token #{key} out of range "
                    f"(verse has {len(id_map)} {side} token(s))"
                )
                continue
            result.append(full_id)
        return result

    for i, rec in enumerate(records):
        label = f"record {i + 1}"
        new_rec = dict(rec)
        new_rec["source"] = _lookup(rec.get("source") or [], source_map, "source", label)
        new_rec["target"] = _lookup(rec.get("target") or [], target_map, "target", label)

        meta = rec.get("meta") or {}
        secondary = meta.get("secondary") or {}
        if secondary:
            sec_src = _lookup(secondary.get("source") or [], source_map, "secondary.source", label)
            sec_tgt = _lookup(secondary.get("target") or [], target_map, "secondary.target", label)
            new_secondary: dict = {}
            if sec_src:
                new_secondary["source"] = sec_src
            if sec_tgt:
                new_secondary["target"] = sec_tgt
            new_meta = {k: v for k, v in meta.items() if k != "secondary"}
            if new_secondary:
                new_meta["secondary"] = new_secondary
            new_rec["meta"] = new_meta

        mapped.append(new_rec)

    return mapped, errors


# ---------------------------------------------------------------------------
# Per-verse message formatting
# ---------------------------------------------------------------------------

def _format_source_token(num: int, token: Source) -> str:
    morph = token.morph or ""
    return f"  {num}  {token.text}  {morph}"


def format_verse_block(
    verse_id: str,
    source_tokens: list[Source],
    target_tokens: list[MigrateTarget],
    candidates: dict[str, list[dict]],
    target_language: str,
) -> tuple[str, dict[int, str], dict[int, str]]:
    """Format one verse block using local sequential token numbers.

    Returns (block_text, source_map, target_map) where the maps convert
    local 1-based integers back to full token IDs.
    """
    source_map, target_map = build_verse_token_maps(source_tokens, target_tokens)
    source_inv = {v: k for k, v in source_map.items()}
    target_inv = {v: k for k, v in target_map.items()}

    lines: list[str] = []
    lines.append(f"--- VERSE {verse_id} ---")
    lines.append("")

    lines.append("SOURCE TOKENS (SBLGNT):")
    for num, token in zip(range(1, len(source_tokens) + 1), source_tokens):
        lines.append(_format_source_token(num, token))
    lines.append("")

    lines.append("TARGET TOKENS:")
    for num, t in zip(range(1, len(target_tokens) + 1), target_tokens):
        lines.append(f"  {num}  {t.text!r}")
    lines.append("")

    lines.append("ALIGNMENT CANDIDATES:")
    if not candidates:
        lines.append("  (none)")
    else:
        for source_type, records in candidates.items():
            lines.append(f"\n[{source_type}]")
            for rec in records:
                src_nums = [
                    str(source_inv[sid])
                    for sid in rec.get("source", [])
                    if sid in source_inv
                ]
                tgt_nums = [
                    str(target_inv[tid])
                    for tid in rec.get("target", [])
                    if tid in target_inv
                ]
                lines.append(f"  source: [{' '.join(src_nums)}]  target: [{' '.join(tgt_nums)}]")

    return "\n".join(lines), source_map, target_map


def build_batch_message(
    verse_batch: list[tuple[str, list[Source], list[MigrateTarget], dict[str, list[dict]]]],
    target_language: str,
) -> tuple[str, dict[str, tuple[dict[int, str], dict[int, str]]]]:
    """Concatenate verse blocks for a batch into a single user message.

    Returns (message_str, all_maps) where all_maps maps verse_id →
    (source_map, target_map) for use in reverse_map_records().
    """
    blocks: list[str] = []
    all_maps: dict[str, tuple[dict[int, str], dict[int, str]]] = {}

    for vid, src, tgt, cands in verse_batch:
        block, source_map, target_map = format_verse_block(vid, src, tgt, cands, target_language)
        blocks.append(block)
        all_maps[vid] = (source_map, target_map)

    return "\n\n".join(blocks), all_maps
