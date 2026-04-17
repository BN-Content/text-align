# text-align

Tools to create and improve word-level textual alignments of Bible translations.

Alignments map tokens in a translation to tokens in the source text (Greek NT or Hebrew OT). The direction is always **translation → source**. The format is [Scripture Burrito alignment spec v0.4](https://github.com/bible-technology/alignment-spec/blob/main/spec.md) with project-specific extensions documented in [docs/alignment-principles.md](docs/alignment-principles.md).

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
│   ├── prompt.py        # System prompt and batch message assembly
│   ├── llm.py           # Provider-agnostic LLM call layer (OpenAI / Anthropic)
│   └── refine.py        # refine-alignment CLI
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

Refine alignment candidates using an LLM (OpenAI or Anthropic). Reads candidate files from the `exp/` directory (one or more of `ACAI`, `SIM-MIGRATED`, `DIFF-MIGRATED`), assembles a structured prompt with source and target tokens, and writes a refined SB 0.4 alignment JSON applying the alignment-principles guidelines (primary/secondary, idiom flags, NEQ).

Requires `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in the environment depending on the chosen provider.

```
refine-alignment \
  --target-language eng \
  --target-edition OENGB \
  --target-tsv-dir  path/to/alignments-eng/data/targets/OENGB \
  --output-dir      path/to/alignments-eng/exp/OENGB/LLM-REFINED \
  [--alignment-sources ACAI SIM-MIGRATED DIFF-MIGRATED] \
  [--corpora ot nt] \
  [--llm-provider openai] \
  [--llm-model gpt-5.4-mini] \
  [--batch-size 5] \
  [--max-retries 2] \
  [--verse 41004003]              # single verse (testing)
  [--verse-range 41004001 41020]  # BCV range
```

The `--output-dir` is also used to derive the `exp/` root for candidate lookup: candidates are read from `<output-dir>/../<SOURCE-TYPE>/`. Output files are written to `--output-dir` as `WLCM-<edition>-manual.json` (OT) and `SBLGNT-<edition>-manual.json` (NT).

### `render-alignment`

Generate per-chapter HTML alignment visualizations in SBL Reverse Interlinear style. Each verse is a row of inline-block cells (translation order). Each cell shows the target token above its aligned source token(s) with subscript word-position indices. Relationship symbols follow the SBL RI convention:

| Symbol | Meaning |
|--------|---------|
| → / ← | Non-anchor token; source shown in the adjacent anchor cell |
| ▶N | Token separated from its group by intervening tokens |
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
    WLCM-<edition>-manual.json    # OT
    SBLGNT-<edition>-manual.json  # NT
```

Source TSVs (`SBLGNT.tsv`, `WLCM.tsv`) live in `data/sources/`.

## Alignment format extensions

The base [Scripture Burrito alignment spec v0.4](https://github.com/bible-technology/alignment-spec/blob/main/spec.md) is used without modification for the core `source`/`target` token lists. Project extensions live in the `meta` object (which the spec explicitly leaves open):

Record-level extensions (in `meta` on each record):

| Field | Type | Meaning |
|-------|------|---------|
| `meta.secondary.source` | `string[]` | Source token IDs that are secondary (grammatically implied, not direct lexical equivalent) |
| `meta.secondary.target` | `string[]` | Target token IDs that are secondary |
| `meta.is_idiom` | `bool` | Marks a phrase-to-phrase idiomatic alignment |

Group-level extension (in `meta` on the group, alongside `creator` and `conformsTo`):

| Field | Type | Meaning |
|-------|------|---------|
| `meta.nonEquivalent.source` | `string[]` | Source token IDs positively determined to have no translation equivalent |
| `meta.nonEquivalent.target` | `string[]` | Target token IDs positively determined to have no source correspondent |

All tokens not listed in `meta.secondary` are assumed primary. `meta.nonEquivalent` tokens are distinct from simply unrecorded tokens — they represent a positive determination of non-equivalence (see §3.5 of alignment-principles). See [docs/alignment-principles.md](docs/alignment-principles.md) for full specification.

## Alignment principles

See [docs/alignment-principles.md](docs/alignment-principles.md) for the complete alignment specification, including:

- Generous alignment philosophy
- Three-state model: aligned / NEQ (non-equivalent) / unrecorded
- Primary vs. secondary link types
- Discontiguous token alignment
- Article alignment rules (Greek definite article vs. English "the"/"a")
- Idiom handling
- Grammatical construction cases (§9): finite verbs, participials, infinitivals, adjectives/adverbs, pronouns, prepositions, conjunctions/particles, discourse restructuring
- Mounce Reverse Interlinear guidelines reference cases
- Automated → LLM sharpening workflow
