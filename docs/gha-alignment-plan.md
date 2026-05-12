# GitHub Actions Alignment Pipeline — Implementation Plan

## Goal

Run `refine-alignment` + `retry-alignment` for the full NT either locally or via
GitHub Actions, with parallel execution at chapter granularity and a simple
progress/status story.  OT alignment will use a similar approach but is deferred;
the design should not preclude it.

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
- **refine + retry in the same matrix job** — retry depends directly on refine output
  for the same chapter, so doing both in one job avoids artifact hand-off and keeps
  per-chapter work self-contained.

---

## Per-chapter job sequence

Each matrix job runs this sequence end-to-end for its chapter:

```
1. refine-alignment  (initial pass, cheap model)
         │
         ▼
2. retry-alignment   (loop while exit code 2 — fallback model active)
   ├── exit 2 → loop back (flagged rate ≥ fallback-threshold; cheap model used)
   └── exit 0 → done   (flagged rate < fallback-threshold; retry_llm_* model used
                         for this final pass — or nothing needed)
         │
         ▼
3. upload artifact
```

The final expensive `retry_llm_*` pass happens naturally as the **last** loop iteration
that exits 0.  No explicit "one final run" step is needed.

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

### B. Exit codes for `retry-alignment`

`retry-alignment` must communicate whether the fallback model was active so that the
GHA loop knows whether to iterate again.

New exit code contract (add to `retry_cli.py:main()`):

| Exit code | Meaning |
|-----------|---------|
| 0 | No retries needed, **or** retry_llm_* model was used — done |
| 1 | Unhandled exception (Python default) |
| 2 | Fallback triggered: flagged rate ≥ `--fallback-threshold`; cheap model used — run again |

Implementation: after the fallback decision block (around line 259 of `retry_cli.py`),
stash whether the fallback was used:

```python
used_fallback = False
if retry_differs and flagged_rate >= args.fallback_threshold:
    args.llm_provider     = args._refine_llm_provider
    args.llm_model        = args._refine_llm_model
    args.reasoning_effort = args._refine_reasoning_effort
    used_fallback = True
    print(...)
```

Then at the end of `main()`, after the sync/async call returns:

```python
if used_fallback:
    sys.exit(2)
```

When `retry_differs` is `False` (only one model configured), `used_fallback` stays
`False` and the loop runs once and exits 0 — correct behavior.

### C. `scripts/nt_chapters.py` — chapter matrix and status

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

### D. GHA matrix ceiling and short-chapter bundling

The NT has ~261 chapters; GHA's matrix limit is **256 jobs**. The fix is to bundle
the shortest chapters (those under ~30 verses) with an adjacent chapter into a single
job. There are enough short chapters (2 John=13v, 3 John=15v, Philemon=25v, Jude=25v,
and others) that bundling ~6–8 of them collapses the count to ≤256 comfortably.

Bundled jobs pass `--chapter-range START END` covering two adjacent chapters.
`--skip-existing` still handles re-runs correctly because it checks per output file.

`nt_chapters.py --json` encapsulates this logic: it emits 256 or fewer matrix
entries, bundling short chapters automatically.

### E. `.github/workflows/align-nt.yml`

Three-job pipeline:

```
plan ──→ refine+retry (matrix, up to 256 parallel chapter jobs) ──→ collect (commit back)
```

**Workflow inputs** (`workflow_dispatch`):

| Input | Required | Default | Purpose |
|-------|----------|---------|---------|
| `config` | yes | — | Edition config name (e.g. `BSB`) |
| `chapter` | no | — | Re-run a single chapter or range, e.g. `40013` or `40001 40002` |
| `model` | no | — | Override the model in the config YAML |
| `max-retry-passes` | no | `5` | Max retry loop iterations before giving up |

**`plan` job**: runs `nt_chapters.py --json`. If `chapter` input is provided, emits
a single-element matrix instead of the full list.

**`refine+retry` job** (matrix, `fail-fast: false`, `max-parallel: 20`,
`timeout-minutes: 360`):

```bash
# Step 1 — initial alignment
poetry run refine-alignment \
  --config ${{ inputs.config }} \
  --corpus nt \
  --chapter ${{ matrix.chunk.chapter }} \
  --skip-existing

# Step 2 — retry loop
MAX_PASSES=${{ inputs.max-retry-passes || 5 }}
for i in $(seq 1 $MAX_PASSES); do
  poetry run retry-alignment \
    --config ${{ inputs.config }} \
    --corpus nt \
    --chapter ${{ matrix.chunk.chapter }}
  rc=$?
  [ $rc -eq 0 ] && break           # retry model used (or nothing needed) — done
  [ $rc -ne 2 ] && exit $rc        # unexpected error — fail the job
  echo "Pass $i: fallback model used, looping..."
done
```

The `timeout-minutes: 360` cap (6 hours) gives a chapter enough time for refine +
up to 5 retry passes while still ensuring GHA kills a hung job cleanly.

API keys injected via env from repo secrets (`OPENROUTER_API_KEY`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`). Each job uploads its
output file(s) as a GHA artifact named `align-{chunk.id}`.

**`collect` job** (runs after all refine+retry jobs, `permissions: contents: write`):
Downloads all `align-*` artifacts, copies files into
`alignments-eng/exp/{config}/LLM-REFINED/`, commits and pushes with
`github-actions[bot]` identity. Skips commit if no files changed (idempotent).

---

## Execution model

```
Local (single chapter, development/testing):
  cd C:/git/BN-Content/text-align
  poetry run refine-alignment --config BSB --corpus nt --chapter 40001
  poetry run retry-alignment  --config BSB --corpus nt --chapter 40001

Local (full NT, sequential):
  poetry run refine-alignment --config BSB --corpus nt
  # then retry loop manually or via script

GHA (full NT, parallel):
  → trigger align-nt workflow with config=BSB
  → up to 256 chapter jobs run simultaneously; each does refine + retry loop
  → collect job commits results back to repo

GHA (re-run one failed/timed-out chapter):
  → trigger with config=BSB, chapter=40013
```

---

## Not in scope (for now)

- OT alignment — deferred; the same workflow structure will apply when the time comes
- `score-alignment` standalone reporting in GHA (run locally after pulling output)
- HTML visualization in GHA
- diff-migrate, sim-migrate, acai-align paths
