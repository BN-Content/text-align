# CLAUDE.md — text-align developer notes

## Project purpose

CLI toolchain for creating and refining word-level alignments between Bible translations
and their source texts (Greek NT / Hebrew OT). Alignment format is Scripture Burrito 0.4
with project-specific `meta` extensions (primary/secondary, NEQ, idiom).

## Key conventions

- **Alignment direction:** always translation → source. Records ask "what Greek/Hebrew
  word(s) are behind this translation word?"
- **Primary vs secondary:** primary = direct lexical/semantic connection; secondary =
  grammatically implied with no separate source token.
- **NEQ:** a positive claim of non-equivalence — never use as a fallback for uncertainty.
- See `docs/alignment-principles.md` for the full specification.

## Package layout

```
src/text_align/
├── burrito/       # SB 0.4 data model
├── migrate/       # diff-migrate, sim-migrate CLIs
├── align/         # acai-align CLI
├── refine/        # refine-alignment CLI
│   ├── prompt/    # language-aware prompt system (see below)
│   ├── llm.py     # LLMClient: OpenAI / Anthropic / Google
│   └── refine.py  # CLI entry point
└── render/        # render-alignment HTML visualizer
```

## Multi-language prompt system (`refine/prompt/`)

Prompts are assembled from a `LanguagePromptConfig` registered per ISO 639-3 code.

- `core.py` — `LanguagePromptConfig` dataclass, registry (`register_language` /
  `get_language_config`), Greek NT phenomenon detection (`detect_phenomena`), and
  all prompt assembly / verse formatting functions.
- `eng.py` — English block strings + `ENG_CONFIG`; calls `register_language` on import.
- `__init__.py` — re-exports the public API and imports `eng` to trigger registration.

**To add a new target language:** create `prompt/<iso>.py`, define a `LanguagePromptConfig`
with the appropriate block content, and call `register_language()`. Then import it in
`__init__.py`. English (`eng.py`) is a useful starting point for Romance languages.
Unknown language codes fall back to English automatically.

Planned languages (in priority order): por, spa, fra — then Arabic, Chinese Simplified,
Chinese Traditional, Hindi, Gujarati, Nepali, Tok Pisin, Bislama, Lingala, Swahili.

## LLM providers (`refine/llm.py`)

`LLMClient` supports three providers, selected by the `provider` argument:

| Provider | Env var | Notes |
|----------|---------|-------|
| `openai` | `OPENAI_API_KEY` | Uses Responses API for reasoning models |
| `anthropic` | `ANTHROPIC_API_KEY` | Extended thinking via `thinking` block |
| `google` | `GEMINI_API_KEY` | Gemini 3+ `thinkingLevel` via `ThinkingConfig` |

`reasoning_effort` (none/minimal/low/medium/high) maps to `reasoning_effort` for OpenAI
and `thinkingLevel` for Google. Omitting it sends no thinking config.

## Model names

Never substitute a user-specified model name for a known one, even if the name looks
unfamiliar (e.g. `gpt-5.4-mini`, `gemini-3-flash-preview`). Trust the user.

## render-alignment (`render/html.py`)

- Multi-primary and idiom non-anchor cells render a directional triangle + subscript
  source index. The triangle (`▸` / `◂`) points toward the anchor cell.
- `_tri_toward(token_pos, anchor_pos, is_r2l)` computes the correct direction.
- CSS classes: `.tri` (triangle, 90% font-size), `.sub` (subscript, 60%).

## Testing

Run tests with:
```bash
poetry run pytest
```

For a quick smoke test of a specific LLM provider, use `test_gemini.py` (not committed —
local scratch file) or pass `--verse` to `refine-alignment`.
