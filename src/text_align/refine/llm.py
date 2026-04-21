"""Provider-agnostic LLM call layer for refine-alignment.

Supports OpenAI, Anthropic, and Google (Gemini).  Provider packages are
imported lazily so only the package for the active provider needs to be
installed.

Environment variables:
    OPENAI_API_KEY    — required when provider is "openai"
    ANTHROPIC_API_KEY — required when provider is "anthropic"
    GEMINI_API_KEY    — required when provider is "google"
"""

import json
import os
import time

# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

TOOL_NAME = "submit_verse_alignments"

# Neutral tool schema; translated to provider-specific format before each call.
# The tool accepts ALL verses in the batch in a single call so that providers
# which only make one forced tool call per turn still return every verse.
_NEUTRAL_TOOL_SCHEMA: dict = {
    "name": TOOL_NAME,
    "description": (
        "Submit refined alignment records for every verse in the batch. "
        "Include one entry per verse — do not omit any verse from the batch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "verses": {
                "type": "array",
                "description": "One entry per verse in the batch.",
                "items": {
                    "type": "object",
                    "properties": {
                        "verse_id": {
                            "type": "string",
                            "description": "8-digit BCV verse ID, e.g. '41004003'.",
                        },
                        "records": {
                            "type": "array",
                            "description": "Alignment records for this verse.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Source token IDs.",
                                    },
                                    "target": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Target token IDs.",
                                    },
                                    "meta": {
                                        "type": "object",
                                        "properties": {
                                            "secondary": {
                                                "type": "object",
                                                "properties": {
                                                    "source": {"type": "array", "items": {"type": "string"}},
                                                    "target": {"type": "array", "items": {"type": "string"}},
                                                },
                                            },
                                            "is_idiom": {"type": "boolean"},
                                            "rel": {
                                                "type": "string",
                                                "enum": ["NEQ"],
                                                "description": "NEQ = non-equivalent; internal use only.",
                                            },
                                        },
                                    },
                                },
                                "required": ["source", "target"],
                            },
                        },
                    },
                    "required": ["verse_id", "records"],
                },
            },
        },
        "required": ["verses"],
    },
}


def _openai_tool_schema(neutral: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": neutral["name"],
            "description": neutral["description"],
            "parameters": neutral["parameters"],
        },
    }


def _openai_responses_tool_schema(neutral: dict) -> dict:
    return {
        "type": "function",
        "name": neutral["name"],
        "description": neutral["description"],
        "parameters": neutral["parameters"],
    }


def _anthropic_tool_schema(neutral: dict) -> dict:
    return {
        "name": neutral["name"],
        "description": neutral["description"],
        "input_schema": neutral["parameters"],
    }


def _gemini_tool_schema(neutral: dict):
    """Return a google.genai types.Tool for the neutral schema."""
    from google.genai import types
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name=neutral["name"],
            description=neutral["description"],
            parameters=neutral["parameters"],
        )
    ])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_records(
    records: list[dict],
    source_ids: set[str],
    target_ids: set[str],
) -> tuple[list[dict], list[str], list[str]]:
    """Validate alignment records against the known token ID sets for a verse.

    Invalid records are dropped.  Records where secondary exhausts all tokens on
    one side are silently sanitized (secondary stripped) rather than dropped —
    the alignment correspondence is valid; only the classification is wrong.

    Returns ``(valid_records, error_messages, san_details)`` where
    ``san_details`` is a list of human-readable strings describing each
    sanitization event (useful for prompt diagnostics).
    """
    valid: list[dict] = []
    errors: list[str] = []
    san_details: list[str] = []

    # Build a map from bare ID → prefixed source ID for normalization.
    # Some models (reasoning mode) strip the canon prefix from source token IDs.
    bare_to_src: dict[str, str] = {
        sid[1:]: sid for sid in source_ids if sid and sid[0].isalpha()
    }

    for i, rec in enumerate(records):
        label = f"record {i + 1}"
        src = rec.get("source") or []
        tgt = rec.get("target") or []
        meta = rec.get("meta") or {}
        is_neq = meta.get("rel") == "NEQ"

        # Normalize bare source IDs: add canon prefix when the bare form matches a
        # known source token and is not itself a target token.
        normalized_src = [
            bare_to_src[s] if (s not in source_ids and s in bare_to_src and s not in target_ids)
            else s
            for s in src
        ]
        if normalized_src != src:
            san_details.append(
                f"{label}: source IDs normalized (canon prefix added): "
                f"{[s for s, n in zip(src, normalized_src) if s != n]!r}"
            )
            src = normalized_src
            rec = {**rec, "source": src}

        # If a record is flagged NEQ but has both source and target tokens, the model
        # confused NEQ with a regular alignment — strip the NEQ flag and continue as
        # a regular record.
        if is_neq and src and tgt:
            clean_meta = {k: v for k, v in meta.items() if k != "rel"}
            rec = {**rec, "meta": clean_meta} if clean_meta else {
                k: v for k, v in rec.items() if k != "meta"
            }
            meta = clean_meta
            is_neq = False
            san_details.append(
                f"{label}: NEQ flag removed — both source and target present; "
                f"treating as regular alignment"
            )

        if is_neq:
            # secondary is meaningless on a NEQ record — strip it silently
            if meta.get("secondary"):
                clean_meta = {k: v for k, v in meta.items() if k != "secondary"}
                rec = {**rec, "meta": clean_meta} if clean_meta else {
                    k: v for k, v in rec.items() if k != "meta"
                }
                meta = clean_meta
                san_details.append(
                    f"{label} (NEQ): secondary stripped — source={src!r} target={tgt!r}"
                )
            # Exactly one non-empty array
            if bool(src) == bool(tgt):
                errors.append(
                    f"{label}: NEQ record must have exactly one non-empty array "
                    f"(source={src!r}, target={tgt!r})"
                )
                continue
            bad = [s for s in src if s not in source_ids] + \
                  [t for t in tgt if t not in target_ids]
            if bad:
                errors.append(f"{label}: unknown token ID(s): {', '.join(bad)}")
                continue

        else:
            if not src and not tgt:
                errors.append(f"{label}: non-NEQ record has neither source nor target")
                continue

            rec_errors: list[str] = []
            bad_src = [s for s in src if s not in source_ids]
            bad_tgt = [t for t in tgt if t not in target_ids]
            secondary = meta.get("secondary") or {}
            bad_sec_src = [s for s in (secondary.get("source") or []) if s not in set(src)]
            bad_sec_tgt = [t for t in (secondary.get("target") or []) if t not in set(tgt)]

            if bad_src:
                rec_errors.append(f"unknown source ID(s): {', '.join(bad_src)}")
            if bad_tgt:
                rec_errors.append(f"unknown target ID(s): {', '.join(bad_tgt)}")
            if bad_sec_src:
                rec_errors.append(f"secondary.source not subset of source: {', '.join(bad_sec_src)}")
            if bad_sec_tgt:
                rec_errors.append(f"secondary.target not subset of target: {', '.join(bad_sec_tgt)}")

            if rec_errors:
                errors.extend(f"{label}: {e}" for e in rec_errors)
                continue

            # Sanitize: strip secondary lists that exhaust all tokens on one side.
            # A record with no primary tokens on a side is invalid, but the
            # alignment itself is correct — strip the bad classification only.
            sec_src = list(secondary.get("source") or [])
            sec_tgt = list(secondary.get("target") or [])
            sides_stripped: list[str] = []
            if src and set(sec_src) >= set(src):
                sides_stripped.append(f"secondary.source exhausted source={src!r}")
                sec_src = []
            if tgt and set(sec_tgt) >= set(tgt):
                sides_stripped.append(f"secondary.target exhausted target={tgt!r}")
                sec_tgt = []

            if sides_stripped:
                san_details.append(f"{label}: {'; '.join(sides_stripped)}")
                clean_secondary = {}
                if sec_src:
                    clean_secondary["source"] = sec_src
                if sec_tgt:
                    clean_secondary["target"] = sec_tgt
                clean_meta = {k: v for k, v in meta.items() if k != "secondary"}
                if clean_secondary:
                    clean_meta["secondary"] = clean_secondary
                rec = {**rec, "meta": clean_meta} if clean_meta else {
                    k: v for k, v in rec.items() if k != "meta"
                }

        valid.append(rec)

    # Cross-record: deduplicate target IDs (non-NEQ only — each target token
    # should appear in exactly one alignment record per verse).
    seen_targets: set[str] = set()
    deduped: list[dict] = []
    for rec in valid:
        if (rec.get("meta") or {}).get("rel") == "NEQ":
            deduped.append(rec)
            continue
        tgts = list(rec.get("target") or [])
        dup = [t for t in tgts if t in seen_targets]
        if dup:
            clean = [t for t in tgts if t not in seen_targets]
            src_ids = rec.get("source") or []
            if not clean:
                errors.append(
                    f"record dropped: all target ID(s) already used in this verse: "
                    f"{', '.join(dup)}"
                )
                continue
            rec = {**rec, "target": clean}
            san_details.append(
                f"record: duplicate target(s) removed {dup!r} "
                f"— source={src_ids!r} kept={clean!r}"
            )
            tgts = clean
        seen_targets.update(tgts)
        deduped.append(rec)

    return deduped, errors, san_details


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iter_verse_entries(
    data: dict,
    errors: list[str],
) -> list[tuple[str, list[dict]]]:
    """Return (verse_id, records) pairs from a tool-call data dict.

    Skips and logs any entry that is not a dict (malformed model output).
    """
    out: list[tuple[str, list[dict]]] = []
    for entry in data.get("verses", []):
        if not isinstance(entry, dict):
            errors.append(
                f"Malformed entry in verses array (expected object, got "
                f"{type(entry).__name__!r}): {str(entry)[:80]!r}"
            )
            continue
        out.append((entry.get("verse_id", ""), entry.get("records", [])))
    return out


# ---------------------------------------------------------------------------
# API-level backoff retry
# ---------------------------------------------------------------------------

_RETRIABLE_STATUS_CODES: frozenset[int] = frozenset({429, 503})


def _status_code(exc: Exception) -> int | None:
    """Return the HTTP status code from a provider exception, or None."""
    if hasattr(exc, "status_code"):
        try:
            return int(exc.status_code)
        except (TypeError, ValueError):
            pass
    # Fallback: the Google SDK embeds the code at the start of the str repr
    s = str(exc)
    for code in (429, 503, 500):
        if s.startswith(str(code)):
            return code
    return None


def _api_call_with_backoff(fn, max_retries: int, provider: str):
    """Call fn(), retrying on transient API errors with exponential backoff.

    Retries on 429 (rate-limited) and 503 (overloaded) up to *max_retries*
    times.  Raises RuntimeError immediately on non-retriable errors or after
    exhausting retries.  Delays: 2s, 4s, 8s, 16s, …
    """
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            code = _status_code(exc)
            if code not in _RETRIABLE_STATUS_CODES or attempt == max_retries:
                raise RuntimeError(
                    f"{provider} API error (attempt {attempt + 1}): {exc}"
                ) from exc
            delay = 2 ** (attempt + 1)
            print(
                f"  {provider} API {code} — retrying in {delay}s "
                f"(attempt {attempt + 1}/{max_retries + 1}) ...",
                flush=True,
            )
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Retry message builder
# ---------------------------------------------------------------------------

def _build_retry_message(verse_errors: dict[str, list[str]]) -> str:
    lines = [
        "The following verses had validation errors in your previous response.",
        "Please resubmit corrected records for each.",
        "",
    ]
    for verse_id, errs in verse_errors.items():
        lines.append(f"VERSE {verse_id}:")
        lines.extend(f"  - {e}" for e in errs)
        lines.append("")
    lines.append("Resubmit only the corrected verses.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------

class LLMClient:
    """Provider-agnostic client for refine-alignment LLM calls.

    Args:
        provider: ``"openai"``, ``"anthropic"``, or ``"google"``.
        model: Model name, e.g. ``"gpt-5.4-mini"``, ``"claude-sonnet-4-6"``,
            or ``"gemini-3.1-flash"``.
    """

    #: Anthropic max_tokens for alignment batch calls.
    #: 32 000 gives Opus 4.7 headroom for thinking tokens before the tool call.
    ANTHROPIC_MAX_TOKENS = 32000

    def __init__(
        self,
        provider: str,
        model: str,
        reasoning_effort: str | None = None,
        max_api_retries: int = 4,
    ) -> None:
        if provider not in ("openai", "anthropic", "google"):
            raise ValueError(
                f"Unknown provider {provider!r}. Use 'openai', 'anthropic', or 'google'."
            )
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort  # OpenAI only; None = use model default
        self.max_api_retries = max_api_retries
        self._client = self._init_client()

    def _init_client(self):
        if self.provider == "openai":
            try:
                import openai
            except ImportError:
                raise ImportError("Install the openai package: poetry add openai")
            return openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        elif self.provider == "anthropic":
            try:
                import anthropic
            except ImportError:
                raise ImportError("Install the anthropic package: poetry add anthropic")
            return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        else:
            try:
                from google import genai
            except ImportError:
                raise ImportError("Install the google-genai package: poetry add google-genai")
            return genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    def call_batch(
        self,
        system_prompt: str,
        user_message: str,
        verse_source_ids: dict[str, set[str]],
        verse_target_ids: dict[str, set[str]],
        max_retries: int = 2,
    ) -> tuple[dict[str, list[dict]], list[str], list[str]]:
        """Call the LLM for a verse batch with forced tool use, validate, and retry.

        Args:
            system_prompt: Assembled system prompt from ``prompt.build_system_prompt()``.
            user_message: Batch message from ``prompt.build_batch_message()``.
            verse_source_ids: ``verse_id → set`` of valid source token IDs.
            verse_target_ids: ``verse_id → set`` of valid target token IDs.
            max_retries: Maximum retry attempts on validation failure.

        Returns:
            ``(results, unresolved_errors, san_details)`` where ``results`` maps
            ``verse_id → list[record_dict]``, ``unresolved_errors`` lists errors
            that remained after all retries, and ``san_details`` is a list of
            human-readable strings describing each sanitization event.
        """
        if self.provider == "openai":
            return self._call_openai(
                system_prompt, user_message, verse_source_ids, verse_target_ids, max_retries
            )
        elif self.provider == "anthropic":
            return self._call_anthropic(
                system_prompt, user_message, verse_source_ids, verse_target_ids, max_retries
            )
        else:
            return self._call_gemini(
                system_prompt, user_message, verse_source_ids, verse_target_ids, max_retries
            )

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------

    def _call_openai(
        self,
        system_prompt: str,
        user_message: str,
        verse_source_ids: dict[str, set[str]],
        verse_target_ids: dict[str, set[str]],
        max_retries: int,
    ) -> tuple[dict[str, list[dict]], list[str], list[str]]:
        if self.reasoning_effort is not None:
            return self._call_openai_responses(
                system_prompt, user_message, verse_source_ids, verse_target_ids, max_retries
            )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ]
        tool_schema = [_openai_tool_schema(_NEUTRAL_TOOL_SCHEMA)]
        tool_choice = {"type": "function", "function": {"name": TOOL_NAME}}

        results: dict[str, list[dict]] = {}
        all_errors: list[str] = []
        all_san_details: list[str] = []

        for attempt in range(max_retries + 1):
            response = _api_call_with_backoff(
                lambda: self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tool_schema,
                    tool_choice=tool_choice,
                ),
                self.max_api_retries,
                "OpenAI",
            )

            choice = response.choices[0]
            if choice.finish_reason == "length":
                print(
                    f"  WARNING: response truncated (finish_reason=length) — "
                    f"some verses may be missing. Reduce --batch-size."
                )
            assistant_msg = choice.message
            tool_calls = assistant_msg.tool_calls or []

            verse_errors: dict[str, list[str]] = {}
            tool_results: list[dict] = []

            for tc in tool_calls:
                try:
                    data = json.loads(tc.function.arguments)
                except json.JSONDecodeError as exc:
                    all_errors.append(f"JSON parse error in tool call: {exc}")
                    tool_results.append({
                        "role": "tool",
                        "content": f"parse error: {exc}",
                        "tool_call_id": tc.id,
                    })
                    continue

                tc_errors: list[str] = []
                for verse_id, records in _iter_verse_entries(data, all_errors):
                    valid, errs, san_details = validate_records(
                        records,
                        verse_source_ids.get(verse_id, set()),
                        verse_target_ids.get(verse_id, set()),
                    )
                    all_san_details.extend(f"VERSE {verse_id}: {d}" for d in san_details)
                    if valid:
                        results[verse_id] = valid
                    if errs:
                        verse_errors[verse_id] = errs
                        tc_errors.extend(f"VERSE {verse_id}: {e}" for e in errs)

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": (
                        "Validation errors:\n" + "\n".join(f"  - {e}" for e in tc_errors)
                        if tc_errors else "ok"
                    ),
                })

            if not verse_errors or attempt == max_retries:
                if verse_errors:
                    for vid, errs in verse_errors.items():
                        all_errors.extend(f"VERSE {vid} (unresolved): {e}" for e in errs)
                break

            # Extend conversation for retry
            messages.append({
                "role": "assistant",
                "content": assistant_msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })
            messages.extend(tool_results)
            messages.append({"role": "user", "content": _build_retry_message(verse_errors)})

        return results, all_errors, all_san_details

    def _call_openai_responses(
        self,
        system_prompt: str,
        user_message: str,
        verse_source_ids: dict[str, set[str]],
        verse_target_ids: dict[str, set[str]],
        max_retries: int,
    ) -> tuple[dict[str, list[dict]], list[str], list[str]]:
        """Use /v1/responses API — required when reasoning_effort is set."""
        initial_input: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ]
        tool_schema = [_openai_responses_tool_schema(_NEUTRAL_TOOL_SCHEMA)]
        tool_choice = {"type": "function", "name": TOOL_NAME}

        results: dict[str, list[dict]] = {}
        all_errors: list[str] = []
        all_san_details: list[str] = []
        previous_response_id: str | None = None
        retry_input: list[dict] = []

        for attempt in range(max_retries + 1):
            input_items = initial_input if previous_response_id is None else retry_input
            kwargs: dict = dict(
                model=self.model,
                input=input_items,
                tools=tool_schema,
                tool_choice=tool_choice,
                reasoning={"effort": self.reasoning_effort},
            )
            if previous_response_id is not None:
                kwargs["previous_response_id"] = previous_response_id
            response = _api_call_with_backoff(
                lambda: self._client.responses.create(**kwargs),
                self.max_api_retries,
                "OpenAI",
            )

            previous_response_id = response.id

            if getattr(response, "status", None) == "incomplete":
                print(
                    f"  WARNING: response incomplete — "
                    f"some verses may be missing. Reduce --batch-size."
                )

            tool_calls = [
                item for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]

            verse_errors: dict[str, list[str]] = {}
            tool_results: list[dict] = []

            for tc in tool_calls:
                try:
                    data = json.loads(tc.arguments)
                except json.JSONDecodeError as exc:
                    all_errors.append(f"JSON parse error in tool call: {exc}")
                    tool_results.append({
                        "type": "function_call_output",
                        "call_id": tc.call_id,
                        "output": f"parse error: {exc}",
                    })
                    continue

                tc_errors: list[str] = []
                for verse_id, records in _iter_verse_entries(data, all_errors):
                    valid, errs, san_details = validate_records(
                        records,
                        verse_source_ids.get(verse_id, set()),
                        verse_target_ids.get(verse_id, set()),
                    )
                    all_san_details.extend(f"VERSE {verse_id}: {d}" for d in san_details)
                    if valid:
                        results[verse_id] = valid
                    if errs:
                        verse_errors[verse_id] = errs
                        tc_errors.extend(f"VERSE {verse_id}: {e}" for e in errs)

                tool_results.append({
                    "type": "function_call_output",
                    "call_id": tc.call_id,
                    "output": (
                        "Validation errors:\n" + "\n".join(f"  - {e}" for e in tc_errors)
                        if tc_errors else "ok"
                    ),
                })

            if not verse_errors or attempt == max_retries:
                if verse_errors:
                    for vid, errs in verse_errors.items():
                        all_errors.extend(f"VERSE {vid} (unresolved): {e}" for e in errs)
                break

            # For retry: tool results + new user message become the next input;
            # previous_response_id chains the conversation context.
            retry_input = tool_results + [
                {"role": "user", "content": _build_retry_message(verse_errors)},
            ]

        return results, all_errors, all_san_details

    # ------------------------------------------------------------------
    # Anthropic
    # ------------------------------------------------------------------

    def _call_anthropic(
        self,
        system_prompt: str,
        user_message: str,
        verse_source_ids: dict[str, set[str]],
        verse_target_ids: dict[str, set[str]],
        max_retries: int,
    ) -> tuple[dict[str, list[dict]], list[str], list[str]]:
        messages: list[dict] = [{"role": "user", "content": user_message}]
        tool_schema = [_anthropic_tool_schema(_NEUTRAL_TOOL_SCHEMA)]
        tool_choice = {"type": "tool", "name": TOOL_NAME}

        results: dict[str, list[dict]] = {}
        all_errors: list[str] = []
        all_san_details: list[str] = []

        for attempt in range(max_retries + 1):
            def _do_anthropic():
                with self._client.messages.stream(
                    model=self.model,
                    max_tokens=self.ANTHROPIC_MAX_TOKENS,
                    system=system_prompt,
                    messages=messages,
                    tools=tool_schema,
                    tool_choice=tool_choice,
                ) as stream:
                    return stream.get_final_message()

            response = _api_call_with_backoff(_do_anthropic, self.max_api_retries, "Anthropic")

            if response.stop_reason == "max_tokens":
                print(
                    f"  WARNING: response truncated (stop_reason=max_tokens, "
                    f"limit={self.ANTHROPIC_MAX_TOKENS}) — "
                    f"some verses may be missing. Reduce --batch-size."
                )

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            verse_errors: dict[str, list[str]] = {}
            tool_results: list[dict] = []

            for block in tool_use_blocks:
                block_errors: list[str] = []
                for verse_id, records in _iter_verse_entries(block.input, all_errors):
                    valid, errs, san_details = validate_records(
                        records,
                        verse_source_ids.get(verse_id, set()),
                        verse_target_ids.get(verse_id, set()),
                    )
                    all_san_details.extend(f"VERSE {verse_id}: {d}" for d in san_details)
                    if valid:
                        results[verse_id] = valid
                    if errs:
                        verse_errors[verse_id] = errs
                        block_errors.extend(f"VERSE {verse_id}: {e}" for e in errs)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": (
                        "Validation errors:\n" + "\n".join(f"  - {e}" for e in block_errors)
                        if block_errors else "ok"
                    ),
                })

            if not verse_errors or attempt == max_retries:
                if verse_errors:
                    for vid, errs in verse_errors.items():
                        all_errors.extend(f"VERSE {vid} (unresolved): {e}" for e in errs)
                break

            # Extend conversation for retry — tool_results must accompany tool_use blocks
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": tool_results + [
                    {"type": "text", "text": _build_retry_message(verse_errors)},
                ],
            })

        return results, all_errors, all_san_details

    # ------------------------------------------------------------------
    # Google (Gemini)
    # ------------------------------------------------------------------

    def _call_gemini(
        self,
        system_prompt: str,
        user_message: str,
        verse_source_ids: dict[str, set[str]],
        verse_target_ids: dict[str, set[str]],
        max_retries: int,
    ) -> tuple[dict[str, list[dict]], list[str], list[str]]:
        from google.genai import types

        tool = _gemini_tool_schema(_NEUTRAL_TOOL_SCHEMA)
        thinking_config = None
        if self.reasoning_effort and self.reasoning_effort != "none":
            thinking_config = types.ThinkingConfig(thinking_level=self.reasoning_effort)
        gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[tool],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=[TOOL_NAME],
                )
            ),
            thinking_config=thinking_config,
        )

        contents: list = [
            types.Content(role="user", parts=[types.Part(text=user_message)])
        ]

        results: dict[str, list[dict]] = {}
        all_errors: list[str] = []
        all_san_details: list[str] = []

        for attempt in range(max_retries + 1):
            response = _api_call_with_backoff(
                lambda: self._client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=gen_config,
                ),
                self.max_api_retries,
                "Google",
            )

            candidate = response.candidates[0]
            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason is not None and "MAX_TOKENS" in str(finish_reason):
                print(
                    f"  WARNING: response truncated (finish_reason=MAX_TOKENS) — "
                    f"some verses may be missing. Reduce --batch-size."
                )

            function_calls = [
                part.function_call
                for part in candidate.content.parts
                if getattr(part, "function_call", None)
            ]

            verse_errors: dict[str, list[str]] = {}
            response_parts: list = []

            for fc in function_calls:
                try:
                    # fc.args is a dict in google-genai 1.x
                    data = fc.args if isinstance(fc.args, dict) else dict(fc.args)
                except Exception as exc:
                    all_errors.append(f"Could not read function call args: {exc}")
                    response_parts.append(types.Part(
                        function_response=types.FunctionResponse(
                            name=fc.name,
                            response={"output": f"parse error: {exc}"},
                        )
                    ))
                    continue

                fc_errors: list[str] = []
                for verse_id, records in _iter_verse_entries(data, all_errors):
                    valid, errs, san_details = validate_records(
                        records,
                        verse_source_ids.get(verse_id, set()),
                        verse_target_ids.get(verse_id, set()),
                    )
                    all_san_details.extend(f"VERSE {verse_id}: {d}" for d in san_details)
                    if valid:
                        results[verse_id] = valid
                    if errs:
                        verse_errors[verse_id] = errs
                        fc_errors.extend(f"VERSE {verse_id}: {e}" for e in errs)

                response_parts.append(types.Part(
                    function_response=types.FunctionResponse(
                        name=fc.name,
                        response={
                            "output": (
                                "Validation errors:\n" + "\n".join(f"  - {e}" for e in fc_errors)
                                if fc_errors else "ok"
                            )
                        },
                    )
                ))

            if not verse_errors or attempt == max_retries:
                if verse_errors:
                    for vid, errs in verse_errors.items():
                        all_errors.extend(f"VERSE {vid} (unresolved): {e}" for e in errs)
                break

            # Extend contents for retry: model turn then user function results + message
            contents.append(candidate.content)
            contents.append(types.Content(
                role="user",
                parts=response_parts + [
                    types.Part(text=_build_retry_message(verse_errors))
                ],
            ))

        return results, all_errors, all_san_details
