"""Provider batch-API helpers for refine-alignment async mode.

Google Gemini is fully implemented.  Anthropic and OpenAI are stubbed.

A "chapter batch" is a list of dicts, one per LLM call:
    {
        "chapter_id":  "41003",         # BBCCC
        "batch_index": 0,               # 0-based within the chapter
        "verse_ids":   [...],           # BBCCCVVV strings in the call
        "system_prompt": "...",
        "user_message":  "...",
    }

Job metadata files live at  jobs/{provider}/{safe_job_id}.json.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

from .llm import (
    TOOL_NAME,
    _NEUTRAL_TOOL_SCHEMA,
    _gemini_tool_schema,
    _iter_verse_entries,
    validate_records,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    """Convert a job name like 'batches/abc123' to a safe filename stem."""
    return name.replace("/", "_").replace(":", "_")


def save_job_metadata(jobs_dir: Path, provider: str, stem: str, metadata: dict) -> Path:
    """Write job metadata JSON to jobs/{provider}/{stem}.json and return the path."""
    provider_dir = jobs_dir / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    path = provider_dir / f"{stem}.json"
    if path.exists():
        raise FileExistsError(
            f"Job metadata file already exists: {path}\n"
            f"This should not happen — check for a duplicate job submission."
        )
    with path.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    return path


def load_job_metadata(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Google (Gemini) batch API
# ---------------------------------------------------------------------------

def _build_gemini_gen_config(reasoning_effort: str | None):
    """Build a GenerateContentConfig for use in InlinedRequest.config."""
    from google.genai import types

    tool = _gemini_tool_schema(_NEUTRAL_TOOL_SCHEMA)
    thinking_config = None
    if reasoning_effort and reasoning_effort != "none":
        thinking_config = types.ThinkingConfig(thinking_level=reasoning_effort)
    return types.GenerateContentConfig(
        tools=[tool],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="ANY",
                allowed_function_names=[TOOL_NAME],
            )
        ),
        thinking_config=thinking_config,
    )


def _build_google_inlined_requests(
    chapter_batches: list[dict],
    reasoning_effort: str | None,
) -> list[Any]:
    """Convert chapter_batches to a list of InlinedRequest objects."""
    from google.genai import types

    base_config = _build_gemini_gen_config(reasoning_effort)
    requests = []
    for idx, cb in enumerate(chapter_batches):
        per_request_config = types.GenerateContentConfig(
            system_instruction=cb["system_prompt"],
            tools=base_config.tools,
            tool_config=base_config.tool_config,
            thinking_config=base_config.thinking_config,
        )
        requests.append(types.InlinedRequest(
            contents=[types.Content(role="user", parts=[types.Part(text=cb["user_message"])])],
            config=per_request_config,
            metadata={"request_index": str(idx)},
        ))
    return requests


def submit_google(
    genai_client: Any,
    model: str,
    reasoning_effort: str | None,
    chapter_batches: list[dict],
    jobs_dir: Path,
    job_metadata_base: dict,
) -> tuple[str, Path]:
    """Submit chapter_batches to Google's batch API.

    Returns (job_name, metadata_file_path).
    ``job_metadata_base`` must already contain: target_edition, target_language,
    corpus, corpus_id, output_dir, creator, sources_dir, target_tsv_dir.
    """
    from google.genai import types

    inlined = _build_google_inlined_requests(chapter_batches, reasoning_effort)

    batch_job = genai_client.batches.create(
        model=model,
        src=types.BatchJobSource(inlined_requests=inlined),
    )
    job_name: str = batch_job.name

    # Build a human-readable filename: EDITION-corpus-YYYYMMDD-SHORTID
    edition = job_metadata_base.get("target_edition", "unknown")
    corpus = job_metadata_base.get("corpus", "")
    date_str = datetime.date.today().strftime("%Y%m%d")
    short_id = job_name.split("/")[-1][-8:]
    stem = f"{edition}-{corpus}-{date_str}-{short_id}"

    request_meta = [
        {
            "request_index": idx,
            "chapter_id": cb["chapter_id"],
            "batch_index": cb["batch_index"],
            "verse_ids": cb["verse_ids"],
        }
        for idx, cb in enumerate(chapter_batches)
    ]

    metadata = {
        **job_metadata_base,
        "provider": "google",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "job_name": job_name,
        "submitted_at": datetime.datetime.now().isoformat(),
        "requests": request_meta,
    }

    path = save_job_metadata(jobs_dir, "google", stem, metadata)
    return job_name, path


def poll_google(genai_client: Any, job_name: str) -> str:
    """Return the current state string for a Google batch job."""
    job = genai_client.batches.get(name=job_name)
    return job.state.name


def retrieve_google(
    genai_client: Any,
    job_name: str,
    requests_meta: list[dict],
    verse_source_ids: dict[str, set[str]],
    verse_target_ids: dict[str, set[str]],
) -> tuple[dict[str, dict[str, list[dict]]], list[str], list[str]]:
    """Fetch completed Google batch results and validate alignment records.

    Returns:
        ({chapter_id: {verse_id: [records]}}, error_messages, san_details)
    """
    job = genai_client.batches.get(name=job_name)
    inlined_responses = (job.dest.inlined_responses or []) if job.dest else []

    # Build index → request_meta map for matching (API may not preserve order)
    index_map: dict[int, dict] = {r["request_index"]: r for r in requests_meta}

    chapter_results: dict[str, dict[str, list[dict]]] = {}
    all_errors: list[str] = []
    all_san: list[str] = []

    for resp in inlined_responses:
        # Determine which request this response belongs to
        meta = resp.metadata or {}
        try:
            req_idx = int(meta.get("request_index", -1))
        except (TypeError, ValueError):
            req_idx = -1
        req = index_map.get(req_idx)
        if req is None:
            all_errors.append(
                f"Response has unknown request_index {req_idx!r} — skipping"
            )
            continue

        chapter_id = req["chapter_id"]
        verse_ids = req["verse_ids"]

        if resp.error:
            all_errors.append(
                f"Chapter {chapter_id} batch {req['batch_index']}: "
                f"request error: {resp.error}"
            )
            continue

        gen_response = resp.response
        if gen_response is None or not gen_response.candidates:
            all_errors.append(
                f"Chapter {chapter_id} batch {req['batch_index']}: empty response"
            )
            continue

        candidate = gen_response.candidates[0]
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason is not None and "MAX_TOKENS" in str(finish_reason):
            print(
                f"  WARNING: Chapter {chapter_id} batch {req['batch_index']} truncated "
                f"(finish_reason=MAX_TOKENS) — some verses may be missing."
            )

        function_calls = [
            part.function_call
            for part in (candidate.content.parts if candidate.content else [])
            if getattr(part, "function_call", None)
        ]

        for fc in function_calls:
            try:
                data = fc.args if isinstance(fc.args, dict) else dict(fc.args)
            except Exception as exc:
                all_errors.append(
                    f"Chapter {chapter_id} batch {req['batch_index']}: "
                    f"could not read function call args: {exc}"
                )
                continue

            fc_errors: list[str] = []
            for verse_id, records in _iter_verse_entries(data, fc_errors):
                valid, errs, san = validate_records(
                    records,
                    verse_source_ids.get(verse_id, set()),
                    verse_target_ids.get(verse_id, set()),
                )
                all_san.extend(f"VERSE {verse_id}: {d}" for d in san)
                if valid:
                    chapter_results.setdefault(chapter_id, {})[verse_id] = valid
                if errs:
                    all_errors.extend(f"VERSE {verse_id}: {e}" for e in errs)
            all_errors.extend(fc_errors)

    return chapter_results, all_errors, all_san


# ---------------------------------------------------------------------------
# Anthropic (stubbed)
# ---------------------------------------------------------------------------

def submit_anthropic(
    anthropic_client: Any,
    model: str,
    chapter_batches: list[dict],
    jobs_dir: Path,
    job_metadata_base: dict,
) -> tuple[str, Path]:
    raise NotImplementedError(
        "Anthropic batch API submission is not yet implemented. "
        "Use --batch-mode sync or --llm-provider google."
    )


def retrieve_anthropic(
    anthropic_client: Any,
    batch_id: str,
    requests_meta: list[dict],
    verse_source_ids: dict[str, set[str]],
    verse_target_ids: dict[str, set[str]],
) -> tuple[dict[str, dict[str, list[dict]]], list[str], list[str]]:
    raise NotImplementedError("Anthropic batch API retrieval is not yet implemented.")


# ---------------------------------------------------------------------------
# OpenAI (stubbed)
# ---------------------------------------------------------------------------

def submit_openai(
    openai_client: Any,
    model: str,
    chapter_batches: list[dict],
    jobs_dir: Path,
    job_metadata_base: dict,
) -> tuple[str, Path]:
    raise NotImplementedError(
        "OpenAI batch API submission is not yet implemented. "
        "Use --batch-mode sync or --llm-provider google."
    )


def retrieve_openai(
    openai_client: Any,
    batch_id: str,
    requests_meta: list[dict],
    verse_source_ids: dict[str, set[str]],
    verse_target_ids: dict[str, set[str]],
) -> tuple[dict[str, dict[str, list[dict]]], list[str], list[str]]:
    raise NotImplementedError("OpenAI batch API retrieval is not yet implemented.")
