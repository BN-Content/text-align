"""Provider-agnostic LLM call layer for refine-alignment.

Supports OpenAI and Anthropic.  Provider packages are imported lazily so only
the package for the active provider needs to be installed.

Environment variables:
    OPENAI_API_KEY    — required when provider is "openai"
    ANTHROPIC_API_KEY — required when provider is "anthropic"
"""

import json
import os

# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

TOOL_NAME = "submit_verse_alignments"

# Neutral tool schema; translated to provider-specific format before each call.
_NEUTRAL_TOOL_SCHEMA: dict = {
    "name": TOOL_NAME,
    "description": "Submit refined alignment records for one verse in the batch.",
    "parameters": {
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


def _anthropic_tool_schema(neutral: dict) -> dict:
    return {
        "name": neutral["name"],
        "description": neutral["description"],
        "input_schema": neutral["parameters"],
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_records(
    records: list[dict],
    source_ids: set[str],
    target_ids: set[str],
) -> tuple[list[dict], list[str], int]:
    """Validate alignment records against the known token ID sets for a verse.

    Invalid records are dropped.  Records where secondary exhausts all tokens on
    one side are silently sanitized (secondary stripped) rather than dropped —
    the alignment correspondence is valid; only the classification is wrong.

    Returns ``(valid_records, error_messages, n_sanitized)`` where
    ``n_sanitized`` is the count of records that had secondary stripped.
    """
    valid: list[dict] = []
    errors: list[str] = []
    n_sanitized = 0

    for i, rec in enumerate(records):
        label = f"record {i + 1}"
        src = rec.get("source") or []
        tgt = rec.get("target") or []
        meta = rec.get("meta") or {}
        is_neq = meta.get("rel") == "NEQ"

        if is_neq:
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
            stripped = False
            if src and set(sec_src) >= set(src):
                sec_src = []
                stripped = True
            if tgt and set(sec_tgt) >= set(tgt):
                sec_tgt = []
                stripped = True

            if stripped:
                n_sanitized += 1
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

    return valid, errors, n_sanitized


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
        provider: ``"openai"`` or ``"anthropic"``.
        model: Model name, e.g. ``"gpt-5.4-mini"`` or ``"claude-sonnet-4-6"``.
    """

    #: Anthropic max_tokens for alignment batch calls.
    ANTHROPIC_MAX_TOKENS = 16384

    def __init__(self, provider: str, model: str) -> None:
        if provider not in ("openai", "anthropic"):
            raise ValueError(f"Unknown provider {provider!r}. Use 'openai' or 'anthropic'.")
        self.provider = provider
        self.model = model
        self._client = self._init_client()

    def _init_client(self):
        if self.provider == "openai":
            try:
                import openai
            except ImportError:
                raise ImportError("Install the openai package: poetry add openai")
            return openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        else:
            try:
                import anthropic
            except ImportError:
                raise ImportError("Install the anthropic package: poetry add anthropic")
            return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def call_batch(
        self,
        system_prompt: str,
        user_message: str,
        verse_source_ids: dict[str, set[str]],
        verse_target_ids: dict[str, set[str]],
        max_retries: int = 2,
    ) -> tuple[dict[str, list[dict]], list[str]]:
        """Call the LLM for a verse batch with forced tool use, validate, and retry.

        Args:
            system_prompt: Assembled system prompt from ``prompt.build_system_prompt()``.
            user_message: Batch message from ``prompt.build_batch_message()``.
            verse_source_ids: ``verse_id → set`` of valid source token IDs.
            verse_target_ids: ``verse_id → set`` of valid target token IDs.
            max_retries: Maximum retry attempts on validation failure.

        Returns:
            ``(results, unresolved_errors, n_sanitized)`` where ``results`` maps
            ``verse_id → list[record_dict]``, ``unresolved_errors`` lists errors
            that remained after all retries, and ``n_sanitized`` is the count of
            records that had an all-secondary side stripped.
        """
        if self.provider == "openai":
            return self._call_openai(
                system_prompt, user_message, verse_source_ids, verse_target_ids, max_retries
            )
        else:
            return self._call_anthropic(
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
    ) -> tuple[dict[str, list[dict]], list[str]]:
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ]
        tool_schema = [_openai_tool_schema(_NEUTRAL_TOOL_SCHEMA)]
        tool_choice = {"type": "function", "function": {"name": TOOL_NAME}}

        results: dict[str, list[dict]] = {}
        all_errors: list[str] = []
        total_sanitized = 0

        for attempt in range(max_retries + 1):
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tool_schema,
                tool_choice=tool_choice,
            )
            assistant_msg = response.choices[0].message
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

                verse_id = data.get("verse_id", "")
                records  = data.get("records", [])
                valid, errs, n_san = validate_records(
                    records,
                    verse_source_ids.get(verse_id, set()),
                    verse_target_ids.get(verse_id, set()),
                )
                total_sanitized += n_san
                if valid:
                    results[verse_id] = valid
                if errs:
                    verse_errors[verse_id] = errs
                    tool_results.append({
                        "role": "tool",
                        "content": "Validation errors:\n" + "\n".join(f"  - {e}" for e in errs),
                        "tool_call_id": tc.id,
                    })
                else:
                    tool_results.append({
                        "role": "tool",
                        "content": "ok",
                        "tool_call_id": tc.id,
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

        return results, all_errors, total_sanitized

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
    ) -> tuple[dict[str, list[dict]], list[str]]:
        messages: list[dict] = [{"role": "user", "content": user_message}]
        tool_schema = [_anthropic_tool_schema(_NEUTRAL_TOOL_SCHEMA)]
        tool_choice = {"type": "tool", "name": TOOL_NAME}

        results: dict[str, list[dict]] = {}
        all_errors: list[str] = []

        for attempt in range(max_retries + 1):
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.ANTHROPIC_MAX_TOKENS,
                system=system_prompt,
                messages=messages,
                tools=tool_schema,
                tool_choice=tool_choice,
            )

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            verse_errors: dict[str, list[str]] = {}
            tool_results: list[dict] = []

            for block in tool_use_blocks:
                verse_id = block.input.get("verse_id", "")
                records  = block.input.get("records", [])
                valid, errs, n_san = validate_records(
                    records,
                    verse_source_ids.get(verse_id, set()),
                    verse_target_ids.get(verse_id, set()),
                )
                total_sanitized += n_san
                if valid:
                    results[verse_id] = valid
                if errs:
                    verse_errors[verse_id] = errs
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Validation errors:\n" + "\n".join(f"  - {e}" for e in errs),
                    })
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "ok",
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

        return results, all_errors, total_sanitized
