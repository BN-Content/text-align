# GitHub Actions Alignment Pipeline — Implementation Plan

## Goal

Run `refine-alignment` for the full NT either locally or via GitHub Actions, with
parallel execution at chapter granularity and a simple progress/status story.
OT alignment will use a similar approach but is deferred; the design should not
preclude it.

---

## Architecture decisions

- **From-scratch only** — no diff-migrate, sim-migrate, or ACAI-align support needed.
  `alignment_sources` and migration tooling are irrelevant for this workflow.
- **ACAI** — only needed for HTML visualization, not for alignment itself. Not checked
  out in the alignment workflow.
- **OpenRouter is sync-only** — no batch API. All LLM calls are sequential within a
  chapter job, but chapter jobs run in parallel across the GHA matrix.
- **Single repo** — all data (source TSVs, target TSVs, output JSON, HTML) lives in
  `text-align`. The Clear repo is no longer needed for alignment runs.
- **`refine-alignment` is provider-agnostic** — the GHA workflow calls the same CLI
  you'd run locally. API keys come from GHA secrets or local shell environment.
- **Chapter = unit of parallelism** — one GHA matrix job per chapter. This is the
  natural boundary: it matches the output file, `--skip-existing` skips at exactly
  the right granularity, and a timed-out job wastes at most one chapter's work.

---

## Data layout (within text-align repo)

Mirrors the Clear repo hierarchy, with `alignments_root` changed from
`C:/git/Clear` to `.` (relative to the project root):

```
alignments-eng/
  data/
    targets/
      BSB/
        nt_BSB.tsv        ← moved from Clear repo
        ot_BSB.tsv
      OENGB/
        nt_OENGB.tsv
        ...
  exp/
    BSB/
      LLM-REFINED/        ← chapter JSON output (committed to git)
        SBLGNT-BSB-40-001-manual.json
        SBLGNT-BSB-40-002-manual.json
        ...
    OENGB/
      LLM-REFINED/
        ...
```

TSV files and chapter JSON files are committed directly to git — they are small text
files and don't warrant LFS.

---

## Config change (BSB.yaml and others)

Replace:
```yaml
alignments_root: C:/git/Clear
```
With:
```yaml
alignments_root: .
```

Running `refine-alignment` from the project root (`C:/git/BN-Content/text-align`
locally, or `$GITHUB_WORKSPACE` in GHA) resolves all derived paths correctly via the
existing `load_config_from_args` path derivation logic.

---

## Pieces to build

### A. `--skip-existing` in `refine-alignment`

New flag, default `false`. At the top of the chapter loop in `_process_corpus_sync`
(refine.py line 379), check whether the output file already exists:

```python
book_id, chap_num = chapter_id[:2], chapter_id[2:]
out_path = output_dir / f"{corpus_id}-{target_edition}-{book_id}-{chap_num}-manual.json"
if skip_existing and out_path.exists():
    print(f"  Chapter {chapter_id}: skipping (output exists)")
    continue
```

Also needs to thread through: `parse_args()` → `process_corpus()` →
`_process_corpus_sync()` and `_process_corpus_async()`.

Default is `false` so existing behavior is unchanged unless the flag is passed.
In GHA, always pass `--skip-existing` so a re-triggered job doesn't redo a chapter
that completed before a timeout.

### B. `scripts/nt_chapters.py` — chapter matrix and status

Reads `data/sources/SBLGNT.tsv`, counts unique verse IDs per chapter, and produces
the GHA matrix or a human-readable status report.

**Modes:**

| Invocation | Output |
|------------|--------|
| `python scripts/nt_chapters.py` | Human-readable table: book name, chapter ID, verse count, completion status |
| `python scripts/nt_chapters.py --json` | JSON array for GHA matrix input |
| `python scripts/nt_chapters.py --status --edition BSB` | Per-chapter DONE / PENDING, summary line |

**`--json` output shape** (one element per chapter or bundled entry):
```json
[
  {"id": "40001", "chapter": "40001", "label": "Matt 1 (25v)"},
  {"id": "40002", "chapter": "40002", "label": "Matt 2 (23v)"},
  ...
]
```

**`--status` logic**: check whether
`alignments-eng/exp/{edition}/LLM-REFINED/SBLGNT-{edition}-{BB}-{CCC}-manual.json`
exists for each chapter. Print a summary line:
`187/261 chapters complete, 4890/7957 verses done`.

Options:
- `--edition NAME` — required for `--status`
- `--output-dir PATH` — override the default output path derivation

### C. GHA matrix ceiling and short-chapter bundling

The NT has ~261 chapters; GHA's matrix limit is **256 jobs**. The fix is to bundle
the shortest chapters (those under ~30 verses) with an adjacent chapter into a single
job. There are enough short chapters (2 John=13v, 3 John=15v, Philemon=25v, Jude=25v,
and others) that bundling ~6–8 of them collapses the count to ≤256 comfortably.

Bundled jobs pass `--chapter-range START END` covering two adjacent chapters.
`--skip-existing` still handles re-runs correctly because it checks per output file.

`nt_chapters.py --json` encapsulates this logic: it emits 256 or fewer matrix
entries, bundling short chapters automatically.

### D. `.github/workflows/align-nt.yml`

Three-job pipeline:

```
plan ──→ refine (matrix, up to 256 parallel chapter jobs) ──→ collect (commit back)
```

**Workflow inputs** (`workflow_dispatch`):

| Input | Required | Default | Purpose |
|-------|----------|---------|---------|
| `config` | yes | — | Edition config name (e.g. `BSB`) |
| `chapter` | no | — | Re-run a single chapter or range, e.g. `40013` or `40001 40002` |
| `model` | no | — | Override the model in the config YAML |

**`plan` job**: runs `nt_chapters.py --json`. If `chapter` input is provided, emits
a single-element matrix instead of the full list.

**`refine` job** (matrix, `fail-fast: false`, `max-parallel: 20`,
`timeout-minutes: 300`):
```bash
poetry run refine-alignment \
  --config ${{ inputs.config }} \
  --corpora nt \
  --chapter ${{ matrix.chunk.chapter }}      # or --chapter-range for bundled pairs
  --skip-existing
```
The `timeout-minutes: 300` cap (5 hours) ensures GHA kills a hung job cleanly rather
than consuming the full 6-hour slot. In practice most chapters should complete in
under 3 hours even for complex content; observed worst case was Matt 13 (58 verses)
at a few hours locally.

API keys injected via env from repo secrets (`OPENROUTER_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`). Each job uploads its
output file(s) as a GHA artifact named `align-{chunk.id}`.

**`collect` job** (runs after all refine jobs, `permissions: contents: write`):
Downloads all `align-*` artifacts, copies files into
`alignments-eng/exp/{config}/LLM-REFINED/`, commits and pushes with
`github-actions[bot]` identity. Skips commit if no files changed (idempotent).

---

## Execution model

```
Local (single chapter, development/testing):
  cd C:/git/BN-Content/text-align
  poetry run refine-alignment --config BSB --corpora nt --chapter 40001

Local (full NT, sequential):
  poetry run refine-alignment --config BSB --corpora nt

GHA (full NT, parallel):
  → trigger align-nt workflow with config=BSB
  → up to 256 chapter jobs run simultaneously (~3 hrs worst case vs days sequential)

GHA (re-run one failed/timed-out chapter):
  → trigger with config=BSB, chapter=40013
```

---

## Not in scope (for now)

- OT alignment — deferred; the same workflow structure will apply when the time comes
- `score-alignment` and `retry-alignment` in GHA (run locally after pulling output)
- HTML visualization in GHA
- diff-migrate, sim-migrate, acai-align paths
