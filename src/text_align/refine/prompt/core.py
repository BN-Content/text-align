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
    show_gloss: bool = True


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
# Per-verse message formatting
# ---------------------------------------------------------------------------

def _format_source_token(token: Source, show_gloss: bool) -> str:
    parts = [f"  {token.id:<16}", token.text, f"  {token.pos:<6}", token.morph or ""]
    if show_gloss and token.gloss:
        parts.append(f'  gloss:"{token.gloss}"')
        if token.gloss2 and token.gloss2 != token.gloss:
            lexeme = token.gloss2.replace(".", " ")
            parts.append(f'  lexeme:"{lexeme}"')
    return "".join(parts)


def format_verse_block(
    verse_id: str,
    source_tokens: list[Source],
    target_tokens: list[MigrateTarget],
    candidates: dict[str, list[dict]],
    target_language: str,
) -> str:
    """Return the formatted text block for one verse within a batch message."""
    config = get_language_config(target_language)
    lines: list[str] = []

    lines.append(f"--- VERSE {verse_id} ---")
    lines.append("")

    lines.append("SOURCE TOKENS (SBLGNT):")
    for t in source_tokens:
        lines.append(_format_source_token(t, config.show_gloss))
    lines.append("")

    lines.append("TARGET TOKENS:")
    for t in target_tokens:
        lines.append(f"  {t.id:<16}  {t.text!r}")
    lines.append("")

    lines.append("ALIGNMENT CANDIDATES:")
    if not candidates:
        lines.append("  (none)")
    else:
        for source_type, records in candidates.items():
            lines.append(f"\n[{source_type}]")
            for rec in records:
                src_ids = " ".join(rec.get("source", []))
                tgt_ids = " ".join(rec.get("target", []))
                lines.append(f"  source: [{src_ids}]  target: [{tgt_ids}]")

    return "\n".join(lines)


def build_batch_message(
    verse_batch: list[tuple[str, list[Source], list[MigrateTarget], dict[str, list[dict]]]],
    target_language: str,
) -> str:
    """Concatenate verse blocks for a batch into a single user message."""
    return "\n\n".join(
        format_verse_block(vid, src, tgt, cands, target_language)
        for vid, src, tgt, cands in verse_batch
    )
