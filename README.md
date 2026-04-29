# text-align

Tools to create and improve word-level textual alignments of Bible translations.

Alignments map tokens in a translation to tokens in the source text (Greek NT or Hebrew OT). The direction is always **translation → source**. The format is [Scripture Burrito alignment spec v0.4](https://github.com/bible-technology/alignment-spec/blob/main/spec.md) with project-specific extensions documented in [docs/alignment-principles-nt.md](docs/alignment-principles-nt.md) (NT/Greek) and [docs/alignment-principles-ot.md](docs/alignment-principles-ot.md) (OT/Hebrew).

## Source texts

| Canon | Corpus | File |
|-------|--------|------|
| NT | SBLGNT | `data/sources/SBLGNT.tsv` |
| OT | MACULA Hebrew (WLCM) | `data/sources/WLCM.tsv` |

## Installation

Requires Python ≥ 3.10. Dependencies are managed with [Poetry](https://python-poetry.org/).

```bash
poetry install
```

## Project config files

Each alignment project (source → target translation pair) can be described in a YAML file under `configs/`. All four CLI tools accept `--config <name>` (`.yaml` extension assumed), which loads that file as argument defaults. Any argument can still be overridden on the command line.

```bash
# Run with everything from the config file
sim-migrate --config bonbv

# Override one setting on the fly
sim-migrate --config bonbv --output-dir C:/tmp/test-run
```

Copy `configs/example.yaml` as a starting point — it documents every key with comments. Keys use underscores matching argparse dest names. Path values should be absolute.

## Package layout

```
src/text_align/
├── __init__.py          # ROOT, DATAPATH, SourceidEnum, normalize_strongs, …
├── strongs.py           # Strong's number normalisation
├── stopwords.py         # Shared stopword loaders (stopwordsiso + NLTK)
├── burrito/             # Scripture Burrito data model
│   ├── AlignmentGroup.py
│   ├── AlignmentRecord.py
│   ├── AlignmentSet.py
│   ├── AlignmentType.py
│   ├── BadRecord.py
│   ├── BaseToken.py
│   ├── Manager.py
│   ├── Source.py / SourceReader
│   ├── Target.py / TargetReader
│   ├── VerseData.py
│   └── alignments.py    # AlignmentsReader, write_alignment_group
├── migrate/             # Alignment migration
│   ├── models.py        # MigrateTarget, MigrateVerse
│   ├── tsv.py           # process_usfm_tsv, dump_verse_text, get_wordlist
│   ├── alignment_io.py  # load/write alignment JSON, create_new_alignments
│   ├── diff.py          # diff-migrate CLI
│   └── sim.py           # sim-migrate CLI
├── align/               # Alignment creation
│   ├── acai_common.py   # AcaiEntity, matching logic, trabina, populate_alignment
│   └── acai.py          # acai-align CLI
├── refine/              # LLM-assisted alignment refinement
│   ├── source.py        # Source token loader
│   ├── prompt/          # Language-aware prompt assembly
│   │   ├── core.py      #   LanguagePromptConfig, registry, detection, assembly
│   │   ├── eng.py       #   English prompt blocks and config (auto-registered)
│   │   ├── por.py       #   Portuguese (auto-registered)
│   │   ├── spa.py       #   Latin American Spanish (auto-registered)
│   │   └── __init__.py  #   Public API re-export
│   ├── llm.py           # Provider-agnostic LLM call layer (OpenAI / Anthropic / Google / OpenRouter)
│   ├── async_batch.py   # Provider batch-API helpers (Google, OpenAI, Anthropic)
│   ├── coverage.py      # Per-verse source-token coverage evaluation (legacy)
│   ├── scoring.py       # Composite alignment quality scorer (five signals)
│   ├── scoring_stopwords.py  # Per-language stopword sets for scorer
│   ├── refine.py        # refine-alignment CLI
│   ├── fetch_batch.py   # fetch-batch CLI
│   ├── retry.py         # Verse merge/retry core logic
│   ├── retry_cli.py     # retry-alignment CLI
│   └── score_alignments.py  # score-alignment CLI
└── render/
    └── html.py          # render-alignment CLI
```

## CLI tools

All tools are installed by `poetry install` and available on the Poetry shell path.

### `diff-migrate`

Migrate alignments from a reference translation to a similar translation using word-level text diffs ([diff_match_patch](https://github.com/google/diff-match-patch)).

```
diff-migrate \
  --source-edition NIV11 \
  --target-edition NIrV \
  --source-tsv-dir  path/to/alignments-eng/data/targets/NIV11 \
  --target-tsv-dir  path/to/alignments-eng/data/targets/NIrV \
  --source-alignment-dir path/to/alignments-eng/data/alignments/NIV11 \
  --output-dir path/to/alignments-eng/exp/NIrV/DIFF-MIGRATED
```

### `sim-migrate`

Migrate alignments using multilingual sentence similarity. Supports [LaBSE](https://huggingface.co/sentence-transformers/LaBSE) (default, broad language coverage) and [SONAR_200](https://huggingface.co/cointegrated/SONAR_200_text_encoder) (useful for languages LaBSE does not cover, e.g. Lingala).

```
sim-migrate \
  --source-edition NIV11 --source-language eng \
  --target-edition BONBV --target-language spa \
  --source-tsv-dir  path/to/alignments-eng/data/targets/NIV11 \
  --target-tsv-dir  path/to/alignments-spa/data/targets/BONBV \
  --source-alignment-dir path/to/alignments-eng/data/alignments/NIV11 \
  --output-dir path/to/alignments-spa/exp/BONBV/SIM-MIGRATED \
  [--model sentence-transformers/LaBSE] \
  [--min-similarity 0.7] [--max-word-distance 8] \
  [--no-stopword-filter]
```

### `acai-align`

Create entity alignments (persons, places, groups, etc.) using [ACAI](https://github.com/BibleAquifer/ACAI) data. Matches entities to translation tokens via reference-list overlap and Jaro-Winkler string similarity, with [trabina](https://github.com/RickBrannan/trabina) name-translation data to improve cross-language matching.

```
acai-align \
  --target-language spa \
  --target-edition BONBV \
  --targets-dir  path/to/alignments-spa/data/targets/BONBV \
  --acai-data-dir C:/git/BibleAquifer/ACAI \
  --trabina-dir  C:/git/BN-Content/trabina/data/weighted \
  --output-dir   path/to/alignments-spa/exp/BONBV/ACAI \
  [--include-secondaries] \
  [--acai-types people places groups deities]
```

### `refine-alignment`

Refine alignment candidates using an LLM (OpenAI, Anthropic, Google, or any model via OpenRouter). Reads candidate files from the `exp/` directory, assembles a structured prompt with source and target tokens, and writes refined SB 0.4 alignment JSON applying the alignment-principles guidelines (primary/secondary, idiom flags, NEQ).

Output is **one file per chapter**: `SBLGNT-<edition>-<BB>-<CCC>-manual.json` (NT) or `WLCM-<edition>-<BB>-<CCC>-manual.json` (OT). For example, Mark 3 produces `SBLGNT-OENGB-41-003-manual.json`.

Requires the appropriate API key in the environment:
- `OPENAI_API_KEY` for OpenAI models
- `ANTHROPIC_API_KEY` for Anthropic models
- `GEMINI_API_KEY` for Google Gemini models
- `OPENROUTER_API_KEY` for OpenRouter (access to Qwen, Kimi, GLM, Mistral, and 200+ other models via a single account)

```
refine-alignment \
  --target-language eng \
  --target-edition OENGB \
  --target-tsv-dir  path/to/alignments-eng/data/targets/OENGB \
  --output-dir      path/to/alignments-eng/exp/OENGB/LLM-REFINED \
  [--alignment-sources ACAI SIM-MIGRATED DIFF-MIGRATED MERGED FASTALIGN] \
  [--from-scratch]               # align without candidates
  [--corpora ot nt] \
  [--llm-provider openai]        # openai | anthropic | google | openrouter
  [--llm-model gpt-5.4-mini] \  #   openrouter: use any model slug, e.g. qwen/qwen3-235b-a22b
  [--reasoning-effort high]      # none/minimal/low/medium/high
                                 #   OpenAI gpt-5.x → reasoning_effort (Responses API)
                                 #   Google gemini-3+ → thinkingLevel (ThinkingConfig)
                                 #   ignored for openrouter (always uses chat completions)
  [--batch-size 5] \
  [--max-retries 2] \
  [--max-api-retries 4]          # retries on 429/503 with exponential backoff
  [--temperature 1]              # sampling temperature (default: 1); explicit value
                                 #   ensures sync and async batch calls are identical
                                 #   not applied to OpenAI reasoning models
  [--max-output-tokens 32000]    # token budget (default: 32000); matches Anthropic's
                                 #   hardcoded budget and gives thinking models headroom
  [--batch-mode sync]            # sync (default) | async (google/openai/anthropic only)
  [--jobs-dir jobs/]             # where async job metadata is stored
```

Range filtering — all mutually exclusive:

| Flag | Format | Example |
|------|--------|---------|
| `--verse BCV` | 8-digit BBCCCVVV | `--verse 41004003` |
| `--verse-range START END` | BCV pair | `--verse-range 41004001 41004020` |
| `--book BB` | 2-digit book number | `--book 41` |
| `--book-range START END` | book pair | `--book-range 41 44` |
| `--chapter BBCCC` | 5-digit chapter | `--chapter 41003` |
| `--chapter-range START END` | chapter pair | `--chapter-range 41001 41016` |

Candidate source types (default: all — ACAI, SIM-MIGRATED, DIFF-MIGRATED, MERGED, FASTALIGN, REVISED):
- `ACAI` — entity alignments from `acai-align`
- `SIM-MIGRATED` — similarity-migrated alignments from `sim-migrate`
- `DIFF-MIGRATED` — diff-migrated alignments from `diff-migrate`
- `MERGED` — a pre-merged candidate file
- `FASTALIGN` — fast_align output
- `REVISED` — manually revised alignments

Candidates are read from `<output-dir>/../<SOURCE-TYPE>/`. Use `--from-scratch` to skip candidate loading entirely.

#### Async batch mode (Google, OpenAI, and Anthropic)

Pass `--batch-mode async` to submit all LLM calls to the provider's Batch API (~50% cost reduction, up to 24h turnaround) instead of making synchronous requests. The job is submitted and a metadata file is written to `--jobs-dir` (default `jobs/{provider}/`); the process then exits. Retrieve results later with `fetch-batch`.

Supported for: `google`, `openai`, `anthropic`. **Not supported for `openrouter`** — use `--batch-mode sync` with OpenRouter.

```bash
# Submit (Google)
refine-alignment --config OENGB --book 41 \
  --llm-provider google --llm-model gemini-2.0-flash-001 \
  --batch-mode async

# Submit (OpenAI)
refine-alignment --config OENGB --book 41 \
  --llm-provider openai --llm-model gpt-5.4-mini \
  --batch-mode async

# Submit (Anthropic)
refine-alignment --config OENGB --book 41 \
  --llm-provider anthropic --llm-model claude-haiku-4-5-20251001 \
  --batch-mode async

# Check status
fetch-batch jobs/google/OENGB-nt-20260424-abc12345.json --poll

# Block until done and write chapter files
fetch-batch jobs/google/OENGB-nt-20260424-abc12345.json --wait
```

#### OpenRouter (sync only)

[OpenRouter](https://openrouter.ai/) provides a single OpenAI-compatible API that routes to 200+ models — Qwen, Kimi, GLM, Mistral, Llama, and more — without requiring separate accounts. Set `OPENROUTER_API_KEY` and pass `--llm-provider openrouter` with any OpenRouter model slug.

Per-call cost (USD) is printed after each verse batch and a session total is printed at the end of the run.

```bash
# Qwen 3 235B via OpenRouter
refine-alignment --config OENGB --chapter 41003 \
  --llm-provider openrouter --llm-model qwen/qwen3-235b-a22b

# Kimi K2 via OpenRouter
refine-alignment --config OENGB --chapter 41003 \
  --llm-provider openrouter --llm-model moonshotai/kimi-k2
```

### `fetch-batch`

Retrieve results from an async `refine-alignment` or `retry-alignment` batch job and write the chapter output files.

```
fetch-batch <job-metadata-file> [--poll] [--wait] [--wait-interval SECONDS]
```

| Flag | Behaviour |
|------|-----------|
| *(none)* | Fetch once; exit with error if job not yet complete |
| `--poll` | Print current status (with request counts for OpenAI/Anthropic) and exit |
| `--wait` | Block, printing progress each `--wait-interval` seconds (default 60) |
| `--cancel` | Request cancellation of the job and exit |

For OpenAI and Anthropic, `--poll` and `--wait` display request-level progress derived from the batch object's `request_counts`, e.g.:

```
Batch batch_abc123: in_progress  47/200
Batch batch_abc123: in_progress  118/200, 2 failed
Batch batch_abc123: completed
```

Google exposes only a coarse state enum (`JOB_STATE_PENDING` / `JOB_STATE_RUNNING` / `JOB_STATE_SUCCEEDED`), so its output remains state-only.

For retry jobs (submitted by `retry-alignment --batch-mode async`), `fetch-batch` merges the new verse records into existing chapter files rather than writing fresh ones. The job metadata file identifies retry jobs via `"job_type": "retry"`.

### `score-alignment`

Scores alignment quality for existing chapter JSON files and writes a per-verse TSV report. Does **not** call the LLM — use this between `refine-alignment` and `retry-alignment` to inspect quality and tune the retry threshold before committing to API spend.

Each verse receives a composite penalty score (0–1, higher = worse) from five signals: weighted source-token coverage, translation content-word coverage, NEQ overuse, token smearing (N:M records where both sides have multiple primary tokens), and per-verse deviation from chapter mean. Verses above the threshold are flagged `needs_retry=True`.

```
score-alignment \
  --alignment-dir path/to/alignments-eng/exp/OENGB/LLM-REFINED \
  --corpus nt \
  --target-language eng \
  [--target-edition OENGB] \
  [--target-tsv-dir path/to/alignments-eng/data/targets/OENGB]  # enables signal 2
  [--sources-dir data/sources/] \
  [--score-retry-threshold 0.25] \
  [--flagged-only] \
  [--output scores.tsv] \
  [--config OENGB]
```

Output columns: `verse_id`, `composite`, `signal_1`–`signal_5`, `needs_retry`, `structural_errors`.

### `retry-alignment`

After `fetch-batch` writes chapter JSON files, `retry-alignment` scores each verse using the composite quality scorer and re-aligns flagged verses from a **blank slate** — no prior alignment is passed as a candidate (to avoid the LLM perpetuating bad alignments).

Use `--dry-run` first to inspect which verses would be flagged before making any LLM calls.

```
retry-alignment \
  --alignment-dir path/to/alignments-eng/exp/OENGB/LLM-REFINED \
  --corpus nt \
  --target-language eng \
  --target-edition OENGB \
  --target-tsv-dir path/to/alignments-eng/data/targets/OENGB \
  [--sources-dir data/sources/] \
  [--llm-provider anthropic]          # openai | anthropic | google | openrouter (default: anthropic)
  [--llm-model claude-opus-4-7] \
  [--reasoning-effort high] \
  [--score-retry-threshold 0.25] \    # composite penalty threshold (default: 0.25)
  [--batch-size 5] \
  [--max-retries 2] \
  [--max-api-retries 4] \
  [--temperature 1] \
  [--max-output-tokens 32000] \
  [--batch-mode sync]                 # sync (default) | async
  [--jobs-dir jobs/] \
  [--dry-run]                         # report flagged verses without calling the LLM
  [--config OENGB]
```

Range filtering (same flags as `refine-alignment`, minus `--verse` / `--verse-range`):

| Flag | Example |
|------|---------|
| `--book BB` | `--book 66` |
| `--book-range START END` | `--book-range 65 66` |
| `--chapter BBCCC` | `--chapter 66007` |
| `--chapter-range START END` | `--chapter-range 66001 66022` |

#### Two-pass workflow (cheap model → score → retry with better model)

```bash
# 1. First pass — cheap/fast model
refine-alignment --config OENGB --corpus nt \
  --llm-provider openrouter --llm-model deepseek/deepseek-v4-pro

# 2. Audit scores (no LLM call)
score-alignment --config OENGB --corpus nt --flagged-only --output scores.tsv

# 3. Re-align flagged verses with a better model
retry-alignment --config OENGB --corpus nt \
  --llm-provider anthropic --llm-model claude-sonnet-4-6 --reasoning-effort high
```

The YAML config supports separate model keys for the retry pass (`retry_llm_provider`, `retry_llm_model`, `retry_reasoning_effort`) that override the refine-phase keys in `retry-alignment`. See `configs/example.yaml`.

#### Async retry

```bash
retry-alignment --config OENGB --corpus nt --book 66 \
  --llm-provider anthropic --llm-model claude-opus-4-7 --reasoning-effort high \
  --batch-mode async

fetch-batch jobs/anthropic/OENGB-nt-20260424-abc12345.json --wait
```

### `render-alignment`

Generate per-chapter HTML alignment visualizations in SBL Reverse Interlinear style. Each verse is a row of inline-block cells (translation order). Each cell shows the target token above its aligned source token(s) with subscript word-position indices. Relationship symbols follow the SBL RI convention:

| Symbol | Meaning |
|--------|---------|
| → / ← | Non-anchor token; source shown in the adjacent anchor cell |
| ▸N / ◂N | Token separated from its anchor; triangle points toward anchor cell; N = source word index |
| • | Target token with no source correspondent |
| ≠ | Token positively confirmed as non-equivalent (NEQ) |
| ‹ … › | Multiple source tokens behind one target token/phrase |

Secondary (grammatically implied) tokens are rendered in italic grey. Idiomatic records are rendered in italic. ACAI entity tokens are highlighted.

```
render-alignment \
  --alignment-lang spa \
  --alignment-edition BONBV \
  --lang-data-path path/to/alignments-spa/data \
  --output-dir path/to/alignments-spa/viz \
  [--alignment-dir path/to/exp/BONBV/LLM-REFINED]  # override default alignments/ path
  [--target-edition-name "Biblia de Nuestra Familia Versión Breve"] \
  [--acai-data-dir C:/git/BibleAquifer/ACAI] \
  [--r2l]
```

## Data layout

The tools expect kathairo-produced target TSVs split by canon:

```
data/targets/<edition>/
    ot_<edition>.tsv
    nt_<edition>.tsv

data/alignments/<edition>/
    WLCM-<edition>-manual.json      # OT (legacy single-file or hand-curated)
    SBLGNT-<edition>-manual.json    # NT (legacy single-file or hand-curated)

exp/<edition>/LLM-REFINED/
    SBLGNT-<edition>-41-001-manual.json   # NT chapter files from refine-alignment
    SBLGNT-<edition>-41-002-manual.json
    ...
    WLCM-<edition>-01-001-manual.json     # OT chapter files
    ...

jobs/
    google/<stem>.json      # async batch job metadata (from --batch-mode async)
    openai/<stem>.json      # stem = {edition}-{corpus}-{YYYYMMDD}-{short_id}
    anthropic/<stem>.json
```

Source TSVs (`SBLGNT.tsv`, `WLCM.tsv`) live in `data/sources/`.

`render-alignment` auto-detects chapter files when `--alignment-dir` is pointed at the `LLM-REFINED` (or similar) directory. If `{sourceid}-{edition}-??-???-manual.json` files are present they are merged on the fly; otherwise the tool falls back to the single-file path.

## Alignment format extensions

The base [Scripture Burrito alignment spec v0.4](https://github.com/bible-technology/alignment-spec/blob/main/spec.md) is used without modification for the core `source`/`target` token lists. Project extensions live in the `meta` object (which the spec explicitly leaves open):

Record-level extensions (in `meta` on each record):

| Field | Type | Meaning |
|-------|------|---------|
| `meta.secondary.source` | `string[]` | Source token IDs that are secondary (grammatically implied, not direct lexical equivalent) |
| `meta.secondary.target` | `string[]` | Target token IDs that are secondary |
| `meta.is_idiom` | `bool` | Marks a phrase-to-phrase idiomatic alignment |

Group-level extensions (in `meta` on the group, alongside `creator` and `conformsTo`):

| Field | Type | Meaning |
|-------|------|---------|
| `meta.nonEquivalent.source` | `string[]` | Source token IDs positively determined to have no translation equivalent |
| `meta.nonEquivalent.target` | `string[]` | Target token IDs positively determined to have no source correspondent |
| `meta.llm.provider` | `string` | LLM provider used by `refine-alignment` (`openai`, `anthropic`, `google`, `openrouter`) |
| `meta.llm.model` | `string` | Model name, e.g. `gpt-5.4-mini` |
| `meta.llm.reasoning_effort` | `string` | Reasoning effort level if set, e.g. `high` |

`AlignmentsReader.group_meta` exposes the full raw group meta dict so downstream tools (e.g. `render-alignment`) can read back fields like `llm` without re-parsing the JSON.

All tokens not listed in `meta.secondary` are assumed primary. `meta.nonEquivalent` tokens are distinct from simply unrecorded tokens — they represent a positive determination of non-equivalence (see §3.5 of alignment-principles). See [docs/alignment-principles-nt.md](docs/alignment-principles-nt.md) for full specification.

## Alignment principles

See [docs/alignment-principles-nt.md](docs/alignment-principles-nt.md) (NT/Greek) and [docs/alignment-principles-ot.md](docs/alignment-principles-ot.md) (OT/Hebrew) for the complete alignment specification, including:

- Generous alignment philosophy
- Three-state model: aligned / NEQ (non-equivalent) / unrecorded
- Primary vs. secondary link types
- Discontiguous token alignment
- Article alignment rules (Greek definite article vs. English "the"/"a")
- Idiom handling
- Grammatical construction cases (§9): finite verbs, participials, infinitivals, adjectives/adverbs, pronouns, prepositions, conjunctions/particles, discourse restructuring
- Mounce Reverse Interlinear guidelines reference cases
- Automated → LLM sharpening workflow
