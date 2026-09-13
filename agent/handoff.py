"""Transport-only validation for text handed from the private agent to N.E.K.O."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping

from ..state import redact_text

HANDOFF_TOKEN_BUDGET = 180
MAX_FULL_TEXT_CHARS = 12_000
MAX_AGENT_TEXT_CHARS = 4_000

_MISSING_HANDOFF = (
    "The internal RimWorld agent completed without a callback-safe response. "
    "Review the full result in the plugin panel."
)


class HandoffValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@lru_cache(maxsize=1)
def _tokenizer() -> Any | None:
    # tiktoken is supplied by the N.E.K.O host. A strict byte upper bound keeps
    # the packaged plugin safe if it is unavailable in another environment.
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except (ImportError, KeyError, RuntimeError, ValueError):
        return None


def count_handoff_tokens(text: str) -> tuple[int, str]:
    encoding = _tokenizer()
    if encoding is not None:
        return len(encoding.encode(text)), "o200k_base"
    return len(text.encode("utf-8", "replace")), "utf8_bytes_upper_bound"


def _validated_text(value: object, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffValidationError("invalid_submission", f"{field} must be a non-empty string.")
    raw = value.replace("\x00", " ").strip()
    if len(raw) > max_chars:
        raise HandoffValidationError(
            "submission_too_large",
            f"{field} exceeded its {max_chars}-character transport safety limit.",
        )
    return redact_text(raw, max(len(raw) + 1, 1))


def validate_full_text(value: object) -> str:
    return _validated_text(value, field="full_text", max_chars=MAX_FULL_TEXT_CHARS)


def validate_agent_text(value: object) -> str:
    text = _validated_text(value, field="agent_text", max_chars=MAX_AGENT_TEXT_CHARS)
    tokens, _ = count_handoff_tokens(text)
    if tokens > HANDOFF_TOKEN_BUDGET:
        raise HandoffValidationError(
            "handoff_over_budget", f"agent_text uses {tokens} tokens; the maximum is {HANDOFF_TOKEN_BUDGET}."
        )
    return text


class SubmissionPatchError(HandoffValidationError):
    def __init__(self, code: str, message: str, details: dict[str, Any]):
        super().__init__(code, message)
        self.details = details


def _answer_parts(value, *, references_supported=True):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise HandoffValidationError(
            "invalid_submission", "answer requires 1..16 ordered parts matching the submission schema."
        )
    normalized = []
    for index, part in enumerate(value):
        if not isinstance(part, dict):
            raise HandoffValidationError("invalid_submission", f"answer[{index}] must be an object.")
        if not references_supported and set(part) != {"text"}:
            extra = sorted(set(part) - {"text"})
            top_level = sorted(set(extra) & {"answer", "detail", "language"})
            message = f"answer[{index}] accepts only {{text}}."
            if extra:
                message += " Unsupported fields: " + ", ".join(extra) + "."
            if top_level:
                message += " Put " + ", ".join(top_level) + " at the top level of submit_response, not inside an answer part."
            raise HandoffValidationError("unsupported_reference" if "observation_id" in part else "invalid_submission", message)
        if set(part) == {"text"}:
            normalized.append(
                {"text": _validated_text(part["text"], field="answer.text", max_chars=MAX_FULL_TEXT_CHARS)}
            )
        elif "observation_id" in part and not set(part) - {"observation_id", "paths"}:
            if not isinstance(part["observation_id"], str):
                raise HandoffValidationError(
                    "invalid_submission", "observation_id must identify this run's observation."
                )
            normalized.append(copy.deepcopy(part))
        else:
            raise HandoffValidationError(
                "invalid_submission",
                "A part is either {text} or {observation_id, paths?}; no added labels or expressions.",
            )
    return normalized


def _render_answer(parts, observations, language):
    from .presentation import EvidenceError, render_evidence

    grouped = []
    references = set()
    for part in parts:
        if "text" in part:
            grouped.append(part)
            continue
        identity = part["observation_id"]
        references.add(identity)
        paths = part.get("paths", [""])
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise EvidenceError("paths must be an array of data-relative JSON Pointers.")
        if grouped and grouped[-1].get("observation_id") == identity:
            grouped[-1]["brief_paths"] = list(dict.fromkeys(grouped[-1]["brief_paths"] + paths))
        else:
            grouped.append({"observation_id": identity, "brief_paths": paths[:]})
    if len(references) > 4:
        raise EvidenceError("An answer can reference at most 4 distinct observations.")
    full, short, sources = [], [], []
    fact_tokens = 0
    seen_readings = set()
    for part in grouped:
        if "text" in part:
            full.append(part["text"])
            short.append(part["text"])
        else:
            body, brief, provenance = render_evidence([part], observations, language, _seen=seen_readings)
            if body:
                full.append(body)
            if brief:
                short.append(brief)
            sources.extend(provenance["sources"])
            fact_tokens += count_handoff_tokens(brief)[0]
    return (
        "\n\n".join(full),
        "\n".join(short),
        {
            "format": "answer_parts.v1",
            "renderer_version": "observed_values.v1",
            "sources": sources,
        },
        fact_tokens,
    )


@dataclass(slots=True)
class SubmissionDraft:
    answer: list[dict[str, Any]] = field(default_factory=list)
    detail: str | None = None
    language: str = "zh"
    full_text: str | None = None
    agent_text: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    revision: int = 0
    errors: dict[str, dict[str, str]] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "answer": copy.deepcopy(self.answer),
            "detail": self.detail,
            "language": self.language,
            "full_text": self.full_text,
            "agent_text": self.agent_text,
            "provenance": copy.deepcopy(self.provenance),
            "revision": self.revision,
            "saved_fields": [key for key in ("answer", "detail") if getattr(self, key)],
            "errors": copy.deepcopy(self.errors),
            "complete": bool(self.full_text and self.agent_text and not self.errors),
        }

    def apply(self, patch: Mapping[str, Any], *, observations=(), references_supported=True) -> dict[str, Any]:
        from .presentation import EvidenceError

        if not patch or set(patch) - {"answer", "detail", "language"}:
            raise SubmissionPatchError(
                "invalid_submission",
                "Submit answer parts, optional detail and language.",
                {"saved_fields": self.snapshot()["saved_fields"]},
            )
        previous_answer = self.answer
        changed = set()
        for key in ("language", "answer", "detail"):
            if key not in patch:
                continue
            try:
                value = patch[key]
                if key == "language":
                    if value not in {"zh", "en"}:
                        raise HandoffValidationError("invalid_submission", "language must be zh or en.")
                elif key == "answer":
                    value = _answer_parts(value, references_supported=references_supported)
                    _render_answer(value, observations, self.language)
                elif value is not None:
                    value = _validated_text(value, field=key, max_chars=MAX_FULL_TEXT_CHARS)
                if value != getattr(self, key):
                    changed.add(key)
                setattr(self, key, value)
                self.errors.pop(key, None)
            except (HandoffValidationError, EvidenceError, TypeError) as error:
                self.errors[key] = {"code": getattr(error, "code", "invalid_evidence"), "message": str(error)}
        if "answer" in changed and previous_answer and self.detail is not None and "detail" not in patch:
            self.errors["detail"] = {
                "code": "submission_requires_confirmation",
                "message": "Answer changed; resubmit matching detail or explicitly clear it with null.",
            }
        if changed:
            self.revision += 1
        self.errors.pop("delivery", None)
        self.full_text = self.agent_text = None
        fact_tokens = 0
        short = ""
        try:
            valid_answer = self.answer if "answer" not in self.errors else []
            full, short, provenance, fact_tokens = _render_answer(valid_answer, observations, self.language)
            self.provenance = provenance
            valid_detail = self.detail if "detail" not in self.errors and "answer" not in self.errors else None
            if valid_detail and valid_detail != full:
                full = "\n\n".join(part for part in (full, valid_detail) if part)
            if full:
                self.full_text = validate_full_text(full)
            if not short:
                raise HandoffValidationError(
                    "invalid_submission",
                    "Provide answer parts for the short handoff; detail cannot replace them.",
                )
            self.agent_text = validate_agent_text(short)
        except (HandoffValidationError, EvidenceError) as error:
            self.errors["delivery"] = {"code": getattr(error, "code", "invalid_evidence"), "message": str(error)}
        if self.errors:
            required = [key for key in self.errors if key != "delivery"]
            if "delivery" in self.errors:
                required.append("answer")
            details = {
                "saved_fields": self.snapshot()["saved_fields"],
                "required_fields": list(dict.fromkeys(required)),
                "field_errors": copy.deepcopy(self.errors),
                "draft_revision": self.revision,
            }
            if references_supported:
                details.update(fact_tokens=fact_tokens, text_tokens_available=max(0, HANDOFF_TOKEN_BUDGET - fact_tokens))
            else:
                details.update(handoff_tokens=count_handoff_tokens(short)[0] if short else None, token_budget=HANDOFF_TOKEN_BUDGET)
            first = next(iter(self.errors.values()))
            repair = " Valid inputs are saved. Correct the required fields."
            if references_supported and any("observation_id" in part for part in self.answer):
                repair += " Reference parts already emit the facts; do not restate them in text parts."
            elif self.errors.get("delivery", {}).get("code") == "handoff_over_budget":
                repair += (
                    " Shorten answer to the concise conclusion with room below the token limit;"
                    " keep requested facts in detail and preserve the conclusion's scope and unknowns."
                    " No new game query is needed for this length repair."
                )
            if "answer" in required and self.detail is not None:
                repair += (
                    " If answer changes, resubmit matching saved detail or clear it with null;"
                    " do not clear detail that is needed to answer the question."
                )
            raise SubmissionPatchError(
                first["code"],
                " ".join(item["message"] for item in self.errors.values())
                + repair,
                details,
            )
        result = validate_submission(self.full_text, self.agent_text)
        result["provenance"] = copy.deepcopy(self.provenance)
        return result


def make_submission_tool(draft: SubmissionDraft, observations, on_update=None, *, references_supported=True):
    """One production terminal implementation for the runtime and fixed-observation studies."""
    from .tools import ToolExecutionError, ToolSpec
    from .prompt import build_task_submission_guidance

    async def submit(arguments):
        parts = arguments.get("answer")
        if (
            not references_supported
            and isinstance(parts, list)
            and any(isinstance(part, dict) and "observation_id" in part for part in parts)
        ):
            raise ToolExecutionError(
                "unsupported_reference", "This Mod lacks result descriptions; submit text answer parts."
            )
        try:
            result = draft.apply(arguments, observations=observations, references_supported=references_supported)
        except SubmissionPatchError as error:
            if on_update is not None:
                on_update(draft.snapshot(), "submission_patch_rejected", error.details)
            raise ToolExecutionError(error.code, str(error), details=error.details) from None
        if on_update is not None:
            on_update(
                draft.snapshot(),
                "submission_patch_accepted",
                {"saved_fields": draft.snapshot()["saved_fields"], "revision": draft.revision},
            )
        return result

    spec = ToolSpec(
        name="submit_response",
        description=(
            "Finish by composing the answer itself as ordered parts. A reference part EMITS the selected facts with their necessary subject, unit, scope and unknowns; it is not a citation attachment. "
            "For factual questions, reference parts alone are sufficient when they cover the requested facts. Add a text part only for additional interpretation, not a second restatement of the selected facts. "
            "Text-only answers use one {text:...} part. Optional detail is extra explanation for the full report. "
            "The combined answer parts must fit 180 tokens; full report plus detail must fit 12000 characters. "
            "Use actual data-relative paths, with or without a leading slash: do not prefix /payload/data or /response/payload/data; array positions are integers, never metadata ~2 templates. "
            "Value/status paths bound to the same reading are rendered once. Valid inputs survive rejection. Changed answer requires reconfirming or clearing prior detail. Submit alone after reading query results."
        ),
        parameters={
            "type": "object",
            "properties": {
                "answer": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "description": "The actual answer, shared by full report and short handoff. Do not append reference parts to a textual copy of the same facts.",
                    "items": {
                        "oneOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string", "minLength": 1, "maxLength": MAX_FULL_TEXT_CHARS}
                                },
                                "required": ["text"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "observation_id": {"type": "string"},
                                    "paths": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 32,
                                        "items": {"type": "string"},
                                        "description": "Existing paths relative to that observation's data. These values will appear in the answer; no formulas, new filters or invented labels. Omit for the whole bounded result.",
                                    },
                                },
                                "required": ["observation_id"],
                                "additionalProperties": False,
                            },
                        ]
                    },
                },
                "detail": {
                    "type": ["string", "null"],
                    "maxLength": MAX_FULL_TEXT_CHARS,
                    "description": "Optional further explanation, excluded from the short handoff. null clears saved detail.",
                },
                "language": {"type": "string", "enum": ["zh", "en"], "default": "zh", "description": "Language of the whole submission. Set only here at the top level, never inside answer parts."},
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
        handler=submit,
        terminal=True,
    )
    if not references_supported:
        spec.parameters["properties"]["answer"]["description"] = (
            "Concise conclusion passed to the caller, at most 180 tokens across all parts. "
            "Leave room below the limit; preserve scope and unknowns that affect its meaning. "
            "It need not repeat every fact or row from detail."
        )
        spec.parameters["properties"]["detail"]["description"] = (
            "Requested facts that do not fit the concise answer. Omit when unnecessary; null clears saved detail."
        )
        spec.parameters["properties"]["answer"]["items"]["oneOf"] = spec.parameters["properties"]["answer"]["items"][
            "oneOf"
        ][:1]
        return ToolSpec(
            name=spec.name,
            description=build_task_submission_guidance(),
            parameters=spec.parameters,
            handler=submit,
            terminal=True,
        )
    return spec


def validate_submission(full_text: object, agent_text: object) -> dict[str, Any]:
    """Validate mechanics only; never interpret or rewrite domain content."""

    full = validate_full_text(full_text)
    handoff = validate_agent_text(agent_text)
    handoff_tokens, tokenizer = count_handoff_tokens(handoff)
    full_tokens, _ = count_handoff_tokens(full)
    return {
        "full_text": full,
        "agent_text": handoff,
        "delivery": {
            "format": "natural_text",
            "token_budget": HANDOFF_TOKEN_BUDGET,
            "handoff_tokens": handoff_tokens,
            "full_answer_tokens": full_tokens,
            "tokenizer": tokenizer,
            "host_truncation_expected": False,
        },
    }


def attach_agent_handoff(result: object) -> dict[str, Any]:
    """Attach an already-authored handoff without deriving any game meaning."""

    source = dict(result) if isinstance(result, Mapping) else {}
    status = str(source.get("status") or "degraded").lower()
    answer = source.get("answer")
    agent_text = source.get("agent_text")
    submission_mode = str(source.get("submission_mode") or "unknown")
    try:
        if agent_text is None and status == "ok":
            agent_text = answer
            submission_mode = "text_fallback"
        validated = validate_submission(answer, agent_text)
        text = validated["agent_text"]
        delivery = dict(validated["delivery"])
        delivery["submission_mode"] = submission_mode
    except HandoffValidationError as exception:
        text = _MISSING_HANDOFF
        handoff_tokens, tokenizer = count_handoff_tokens(text)
        full_tokens = count_handoff_tokens(answer)[0] if isinstance(answer, str) else None
        delivery = {
            "format": "natural_text",
            "token_budget": HANDOFF_TOKEN_BUDGET,
            "handoff_tokens": handoff_tokens,
            "full_answer_tokens": full_tokens,
            "tokenizer": tokenizer,
            "submission_mode": "operational_fallback",
            "host_truncation_expected": False,
            "error_code": exception.code,
        }
    source["summary"] = text
    source["reply"] = text
    source["delivery"] = delivery
    return source


__all__ = [
    "HANDOFF_TOKEN_BUDGET",
    "HandoffValidationError",
    "SubmissionDraft",
    "SubmissionPatchError",
    "validate_full_text",
    "validate_agent_text",
    "attach_agent_handoff",
    "count_handoff_tokens",
    "validate_submission",
]
