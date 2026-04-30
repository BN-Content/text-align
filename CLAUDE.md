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
- See `docs/alignment-principles-nt.md` (NT/Greek) and `docs/alignment-principles-ot.md` (OT/Hebrew) for the full specification.

## Package layout

```
src/text_align/
├── burrito/       # SB 0.4 data model
├── migrate/       # diff-migrate, sim-migrate CLIs
├── align/         # acai-align CLI
├── refine/        # refine-alignment + fetch-batch + retry-alignment + score-alignment CLIs
│   ├── prompt/          # language-aware prompt system (see below)
│   ├── llm.py           # LLMClient: OpenAI / Anthropic / Google / OpenRouter (sync)
│   ├── async_batch.py   # provider batch-API helpers (Google, OpenAI, Anthropic)
│   ├── coverage.py      # legacy per-verse source-token coverage evaluation
│   ├── scoring.py       # composite alignment quality scorer (five signals)
│   ├── scoring_stopwords.py # per-language stopword sets for scorer signal 2
│   ├── refine.py        # refine-alignment CLI entry point
│   ├── fetch_batch.py   # fetch-batch CLI entry point
│   ├── retry.py         # verse merge/retry core logic
│   ├── retry_cli.py     # retry-alignment CLI entry point
│   └── score_alignments.py  # score-alignment CLI entry point
└── render/        # render-alignment HTML visualizer
```

## Multi-language prompt system (`refine/prompt/`)

Prompts are assembled from a `LanguagePromptConfig` registered per ISO 639-3 code.

- `core.py` — `LanguagePromptConfig` dataclass, registry (`register_language` /
  `get_language_config`), Greek NT phenomenon detection (`detect_phenomena`), and
  all prompt assembly / verse formatting functions.
- `eng.py` — English block strings + `ENG_CONFIG`; calls `register_language` on import.
- `por.py` — Portuguese. Pro-drop, contracted preposition+article forms (do/da/no/na/
  ao/à/pelo/pela), conditional proper-name articles (BP retains them), reflexive passive,
  personal infinitive. Unchanged blocks imported from `eng.py`.
- `spa.py` — Latin American Spanish. Same pro-drop rules; contracted forms limited to
  `del` and `al` only; proper-name articles always Branch B (LA translations omit them);
  vos/tú regional note; ustedes for 2nd plural; no personal infinitive.
- `__init__.py` — re-exports the public API and imports all language modules to trigger
  registration.

**To add a new target language:** create `prompt/<iso>.py`, define a `LanguagePromptConfig`
with the appropriate block content, and call `register_language()`. Then add the import
to `__init__.py`. Import unchanged blocks from `eng.py` rather than duplicating them.
Unknown language codes fall back to English automatically.

Current languages: eng, por, spa.
Planned: fra — then Arabic, Chinese Simplified, Chinese Traditional, Hindi, Gujarati,
Nepali, Tok Pisin, Bislama, Lingala, Swahili.

## LLM providers (`refine/llm.py`)

`LLMClient` supports three providers, selected by the `provider` argument:

| Provider | Env var | Notes |
|----------|---------|-------|
| `openai` | `OPENAI_API_KEY` | Uses Responses API for reasoning models |
| `anthropic` | `ANTHROPIC_API_KEY` | Extended thinking via `thinking` block |
| `google` | `GEMINI_API_KEY` | Gemini 3+ `thinkingLevel` via `ThinkingConfig` |
| `openrouter` | `OPENROUTER_API_KEY` | OpenAI-compatible proxy to 200+ models (Qwen, Kimi, GLM, …); sync-only; per-call cost tracked in `LLMClient.session_cost` |

`reasoning_effort` (none/minimal/low/medium/high) maps to `reasoning_effort` for OpenAI
and `thinkingLevel` for Google. Omitting it sends no thinking config. Ignored for
`openrouter` (always uses the chat completions path).

## OpenRouter cost tracking (`refine/llm.py`)

`LLMClient.session_cost` accumulates the USD cost of all OpenRouter calls made during
the session. `_track_openrouter_cost(response)` reads `response.usage.model_extra["cost"]`
(Pydantic captures extra fields OpenRouter adds to the standard usage object) and prints
a per-call + running total after each API call. A session total is printed at the end of
`refine-alignment` and `retry-alignment` when `--llm-provider openrouter` is active.
Async batch mode is not supported for `openrouter`.

## Model names

Never substitute a user-specified model name for a known one, even if the name looks
unfamiliar (e.g. `gpt-5.4-mini`, `gemini-3-flash-preview`). Trust the user.

## render-alignment (`render/html.py`)

- Multi-primary and idiom non-anchor cells render a directional triangle + subscript
  source index. The triangle (`▸` / `◂`) points toward the anchor cell.
- `_tri_toward(token_pos, anchor_pos, is_r2l)` computes the correct direction.
- CSS classes: `.tri` (triangle, 90% font-size), `.sub` (subscript, 60%).

## LLM robustness (`refine/llm.py`)

`_iter_verse_entries(data, errors)` is a helper used by all four provider call paths
(`_call_openai`, `_call_openai_responses`, `_call_anthropic`, `_call_gemini`). It
iterates the `verses` array from a tool-call response, skipping and logging any entry
that is not a dict. This guards against malformed model output (e.g. a string element
in the array) that would otherwise crash with `AttributeError` on `.get()`.

`_api_call_with_backoff(fn, max_retries, provider)` wraps each provider's API call.
It retries on 429 (rate-limited) and 503 (overloaded) with exponential backoff (2s,
4s, 8s, …) up to `max_retries` times, and fails fast on non-retriable errors.
`_status_code(exc)` extracts the HTTP status code from any provider exception.
Exposed via `--max-api-retries` (default 4) in `refine-alignment`.

## render-alignment header (`render/html.py`)

Each chapter file opens with a styled `.file-meta` row below the `<h1>` showing:
edition abbreviation + full name, LLM provider/model/reasoning_effort (read from
`group_meta["llm"]` in the alignment JSON), and the render date.

- `_build_meta_row(meta_info)` assembles the HTML row; missing fields are omitted
  gracefully.
- `AlignmentsReader.group_meta` (added to `burrito/alignments.py`) exposes the full
  group-level JSON meta dict so render can read back whatever refine stored.
- `--target-edition-name` CLI arg (also `target_edition_name` in YAML) supplies the
  full translation name; the edition abbreviation comes from `--alignment-edition`.

## refine-alignment output granularity

Output is one JSON file per chapter, not one per corpus:

```
SBLGNT-OENGB-41-003-manual.json   ← Mark 3 (book 41, chapter 003)
SBLGNT-OENGB-41-004-manual.json
```

Format: `{corpus_id}-{edition}-{BB}-{CCC}-manual.json`.
The internal SB 0.4 JSON structure is identical to the old corpus-level file;
it just covers one chapter.

## refine-alignment range filtering

New args narrow which verses are processed. All are mutually exclusive with
each other and with `--verse` / `--verse-range`.

| Arg | Format | Example |
|-----|--------|---------|
| `--book BB` | 2-digit book number | `--book 41` |
| `--book-range BB BB` | inclusive book range | `--book-range 41 44` |
| `--chapter BBCCC` | 5-digit chapter | `--chapter 41003` |
| `--chapter-range BBCCC BBCCC` | inclusive chapter range | `--chapter-range 41001 41016` |

Filtering uses string-prefix comparison on 8-char `BBCCCVVV` verse IDs
(`vid[:2]` = book, `vid[:5]` = chapter); no extra biblelib imports needed.

## Async batch mode (`refine/async_batch.py`)

`refine-alignment --batch-mode async` submits all LLM calls to the provider's
batch API (all three providers implemented) and exits, writing a job metadata
JSON to `jobs/{provider}/{stem}.json`.

`fetch-batch <job-metadata-file>` retrieves completed results and writes
chapter JSON files. Flags: `--poll` (print status, exit), `--wait` (block
until done), `--cancel` (request cancellation).

## fetch-batch progress display (`fetch_batch.py`)

`--poll` and `--wait` show request-level progress counts for OpenAI and
Anthropic (Google exposes only a coarse state enum, so it stays state-only).

- `_openai_progress(batch)` — derives `done/total` from `request_counts.completed`
  + `request_counts.failed`; appends `, N failed` when non-zero.
- `_anthropic_progress(batch)` — sums `succeeded + errored + expired + canceled`
  for `done`; total includes `processing`; appends `, N errored` when non-zero.

Both helpers fall back to the bare status string if `request_counts` is absent
or all zeros (guards against API objects that omit the field).

Example output during `--wait`:
```
  Batch batch_abc123: in_progress  47/200 — waiting ...
  Batch batch_abc123: in_progress  118/200
  Batch batch_abc123: completed
```

Job metadata format: see `docs/batch-api-plan.md`.

Google batch API: `client.batches.create(src=types.BatchJobSource(inlined_requests=[...]))`.
Each `InlinedRequest` carries `metadata={"request_index": "N"}` for result
matching; responses come back as `job.dest.inlined_responses`.

OpenAI batch API: JSONL file uploaded via `files.create`, then submitted with
`batches.create(input_file_id=..., endpoint=..., completion_window="24h")`.
Uses `/v1/responses` when `reasoning_effort` is set, `/v1/chat/completions`
otherwise.

Anthropic batch API: `client.messages.batches.create(requests=[...])` where
each request carries a `custom_id` (the request index as a string) and `params`
matching the `messages.create` schema. Terminal state: `processing_status ==
"ended"`. Individual result types: `"succeeded"`, `"errored"`, `"expired"`,
`"canceled"`. Results retrieved via `client.messages.batches.results(batch_id)`.

## Sync/async generation parameter parity (`refine/llm.py`, `async_batch.py`)

Batch API infrastructure may apply different defaults than the sync path
(different temperature, lower token limits), causing consistent quality
degradation on the async path. Fix: `LLMClient` now always sends `temperature`
and `max_output_tokens` explicitly on every call — both sync and async.

Defaults: `temperature=1`, `max_output_tokens=32000`. The 32 000 token budget
matches the Anthropic hardcoded value (`ANTHROPIC_MAX_TOKENS`) and gives
thinking models (OpenAI reasoning, Gemini with `thinkingLevel`) enough headroom
before the tool call output. Temperature is not sent for OpenAI reasoning
models (it is fixed by the API). Overridable via `--temperature` and
`--max-output-tokens` CLI flags (also settable in YAML config files).

## render-alignment chapter-file detection (`render/html.py`)

The renderer auto-detects chapter files. When `--alignment-dir` contains files
matching `{sourceid}-{edition}-??-???-manual.json`, it merges them via
`AlignmentsReader.from_chapter_files()` and skips the single-file load path.
Falls back to the single-file behavior when no chapter files are found.

Implementation details:
- `AlignmentsReader.from_chapter_files(paths, alignmentset)` — class method in
  `burrito/alignments.py`. Merges `groups[0].records` and `nonEquivalent` sets
  from all chapter files; takes `group_meta` from the first. Accepts an optional
  `_preloaded_data` dict via the regular constructor to bypass the file read.
- `AlignmentSet.__post_init__` — assertion `alignmentpath.exists()` now only
  fires when no `alignmentpath_override` is given. When chapter files are used,
  `alignmentpath_override` is set to the first chapter file (a real existing
  file), so the assertion still passes.
- `Manager.__init__` — accepts optional `preloaded_reader: AlignmentsReader`.
  When supplied, it skips creating its own reader and uses the provided one
  (still calls `clean_alignments` on it).

Full design: `docs/batch-api-plan.md`.

## Alignment quality scoring (`refine/scoring.py`, `refine/scoring_stopwords.py`)

`score_chapter_file(path, source_verses, lang, config, target_verses=None)` scores all
verses in a chapter JSON file and returns `list[VerseScore]`. Each `VerseScore` carries
five penalty signals (0–1 each) and a composite score; verses above `config.retry_threshold`
have `needs_retry=True`.

**Five signals:**

| # | Signal | What it catches |
|---|--------|----------------|
| 1 | Weighted source coverage | Unaligned source tokens, weighted by POS (verb/noun=1.0 … article=0.1) |
| 2 | Translation content-word coverage | Target words not in any record and not NEQ (stop-words excluded) |
| 3 | NEQ overuse | NEQ rate above a per-language baseline (default 10%) |
| 4 | Token smearing | N:M records where both sides have >1 primary and no `is_idiom` flag |
| 5 | Per-verse deviation | Verses anomalously worse than the chapter mean (second pass) |

Signals 1–4 are computed per verse; signal 5 requires a second pass over all verses in the
chapter. `score_chapter()` handles the two-pass logic and sets `needs_retry`.

**Signal 4 (smearing):** catches the cheap-model failure mode where tokens that should be
separate records (e.g. adjective + noun) are grouped into one N:M record. Weighted by
`primary_src × primary_tgt`; a 1.5× adjacency boost applies when source and target token
IDs are both consecutive, which is the strongest indicator of over-grouping.

**Stop-word lists (`scoring_stopwords.py`):** uses `stopwordsiso` (already a project
dependency) intersected with a small curated core per language to keep lists minimal.
Languages without coverage (Tok Pisin, Bislama, Lingala, …) return an empty frozenset —
the safe direction is to penalise gaps rather than suppress content words.

**`ScoringConfig`** holds signal weights (w1–w5), NEQ baseline, adjacency multiplier,
deviation k, and retry threshold. All overridable; defaults work for NT English.

YAML config keys: `score_retry_threshold` (default 0.25). Weights are code defaults;
adjust via `ScoringConfig` if needed.

## score-alignment (`refine/score_alignments.py`)

Standalone audit tool. Reads chapter JSON files and writes a per-verse TSV report (columns:
`verse_id`, `composite`, `signal_1`–`signal_5`, `needs_retry`, `structural_errors`) to
stdout or `--output`. Does **not** call the LLM.

```bash
score-alignment \
  --config OENGB --corpus nt \
  --alignment-dir path/to/LLM-REFINED \
  [--target-tsv-dir path/to/targets/OENGB]   # enables signal 2
  [--score-retry-threshold 0.25] \
  [--flagged-only] \
  [--output scores.tsv]
```

Primary use: run between `refine-alignment` and `retry-alignment` to inspect quality
before committing to a retry spend, and to tune the threshold against manually reviewed
chapters.

## Two-pass workflow (cheap model → score → retry)

```bash
# 1. First pass — cheap/fast model
refine-alignment --config MYEDITION --corpus nt \
  --llm-provider openrouter --llm-model deepseek/deepseek-v4-pro

# 2. Audit scores (no LLM call)
score-alignment --config MYEDITION --corpus nt --flagged-only --output scores.tsv

# 3. Retry flagged verses with a better model
retry-alignment --config MYEDITION --corpus nt \
  --llm-provider anthropic --llm-model claude-sonnet-4-6 --reasoning-effort high
```

The YAML config supports separate model keys per pass — `retry_llm_provider`,
`retry_llm_model`, `retry_reasoning_effort` — that override the refine-phase `llm_*`
keys in `retry-alignment`. If absent, the retry pass falls back to the refine keys.

## retry-alignment (`refine/retry_cli.py`, `refine/retry.py`, `refine/scoring.py`)

Post-batch quality pass: identifies verses that scored above the retry threshold
and re-aligns them from scratch.

**Detection** (`scoring.py`, `coverage.py`): a verse is flagged when either condition
holds: (a) `score_chapter_file()` returns `composite > --score-retry-threshold`
(default 0.25), or (b) `find_low_coverage_verses()` finds more than
`--min-unaligned-src` (default 2) unaligned source tokens. Both checks run for every
chapter; a verse needs only one to trigger.

**Remedy**: flagged verses are sent to the LLM **blank-slate** — no prior
alignment is passed as a candidate. Passing existing records as candidates caused
the LLM to over-weight them and perpetuate bad alignments (including wrong
token-swap errors, not just gaps). Blank-slate lets the LLM produce a clean
realignment of the entire verse.

**Merge** (`retry.py:merge_verse_results`): replaces only the flagged verse
records in the existing chapter JSON. For non-replaced verses, regular records
are kept as-is; NEQ entries are re-inflated into `{"meta": {"rel": "NEQ"}}`
records so `build_output_alignment` can reprocess them uniformly. The resulting
file is written in place.

**Async support**: `--batch-mode async` submits retry verses to the provider
batch API (same three providers as `refine-alignment`). Job metadata carries
`"job_type": "retry"`. `fetch-batch` detects this and calls `merge_verse_results`
instead of writing fresh chapter files.

## Testing

Run tests with:
```bash
poetry run pytest
```

For a quick smoke test of a specific LLM provider, use `test_gemini.py` (not committed —
local scratch file) or pass `--verse 41004003` or `--chapter 41004` to `refine-alignment`
to limit scope. `--chapter` is the natural unit for both sync and async modes.
