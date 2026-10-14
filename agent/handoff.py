"""Transport-only validation for text handed from the private agent to N.E.K.O."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..state import redact_text
from .tokenizer import TokenCounter, acquire_token_counter, get_token_counter
from .tokenizer_work import run_tokenizer_work

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


def count_handoff_tokens(text: str, *, counter: TokenCounter | None = None) -> tuple[int, str]:
    return (counter if counter is not None else get_token_counter()).count(text)


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


def validate_agent_text(value: object, *, counter: TokenCounter | None = None) -> str:
    text = _validated_text(value, field="agent_text", max_chars=MAX_AGENT_TEXT_CHARS)
    tokens, _ = count_handoff_tokens(text, counter=counter)
    if tokens > HANDOFF_TOKEN_BUDGET:
        raise HandoffValidationError(
            "handoff_over_budget", f"agent_text uses {tokens} tokens; the maximum is {HANDOFF_TOKEN_BUDGET}."
        )
    return text


class SubmissionPatchError(HandoffValidationError):
    def __init__(self, code: str, message: str, details: dict[str, Any]):
        super().__init__(code, message)
        self.details = details


@dataclass(slots=True)
class SubmissionDraft:
    full_text: str | None = None
    agent_text: str | None = None
    revision: int = 0
    errors: dict[str, dict[str, str]] = field(default_factory=dict)
    _patch_operation: object | None = field(default=None, init=False, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "full_text": self.full_text,
            "agent_text": self.agent_text,
            "revision": self.revision,
            "saved_fields": [key for key in ("full_text", "agent_text") if getattr(self, key) is not None],
            "errors": dict(self.errors),
            "complete": bool(self.full_text and self.agent_text and not self.errors),
        }

    def apply(self, patch: Mapping[str, Any], *, counter: TokenCounter | None = None) -> dict[str, Any]:
        self._patch_operation = object()
        counter = counter if counter is not None else get_token_counter()
        if not patch or set(patch) - {"full_text", "agent_text"}:
            raise SubmissionPatchError(
                "invalid_submission",
                "Submit at least one of full_text or agent_text.",
                {"saved_fields": self.snapshot()["saved_fields"]},
            )
        changed = False
        if "full_text" in patch:
            try:
                full = validate_full_text(patch["full_text"])
                if self.full_text is not None and full != self.full_text:
                    self.agent_text = None
                    self.errors["agent_text"] = {
                        "code": "handoff_requires_confirmation",
                        "message": "The full report changed; submit a matching agent_text again.",
                    }
                changed = full != self.full_text
                self.full_text = full
                self.errors.pop("full_text", None)
            except HandoffValidationError as error:
                self.errors["full_text"] = {"code": error.code, "message": str(error)}
        if "agent_text" in patch:
            try:
                agent = validate_agent_text(patch["agent_text"], counter=counter)
                changed = changed or agent != self.agent_text
                self.agent_text = agent
                self.errors.pop("agent_text", None)
            except HandoffValidationError as error:
                self.errors["agent_text"] = {"code": error.code, "message": str(error)}
        if changed:
            self.revision += 1
        for key in ("full_text", "agent_text"):
            if getattr(self, key) is None and key not in self.errors:
                self.errors[key] = {"code": "invalid_submission", "message": key + " has not been provided."}
        if self.errors:
            required = list(self.errors)
            saved = self.snapshot()["saved_fields"]
            detail = {
                "saved_fields": saved,
                "required_fields": required,
                "field_errors": dict(self.errors),
                "draft_revision": self.revision,
            }
            code = next(iter(self.errors.values()))["code"]
            message = (
                " ".join(error["message"] for error in self.errors.values())
                + " Saved fields: "
                + ", ".join(saved)
                + ". Correct only: "
                + ", ".join(required)
                + "; omit unchanged saved fields to reuse them."
            )
            raise SubmissionPatchError(code, message, detail)
        return validate_submission(self.full_text, self.agent_text, counter=counter)

    async def apply_async(
        self, patch: Mapping[str, Any], *, counter: TokenCounter | None = None,
    ) -> dict[str, Any]:
        """Validate a detached draft; publish partial fields only while still owned."""
        operation = object()
        self._patch_operation = operation
        before = deepcopy(self.snapshot())
        candidate = SubmissionDraft(
            full_text=self.full_text, agent_text=self.agent_text, revision=self.revision,
            errors=deepcopy(self.errors),
        )
        submitted = dict(patch)
        counter = counter if counter is not None else await acquire_token_counter()

        def evaluate() -> tuple[dict[str, Any] | None, SubmissionPatchError | None]:
            try:
                return candidate.apply(submitted, counter=counter), None
            except SubmissionPatchError as error:
                return None, error

        result, error = await run_tokenizer_work(evaluate)
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if self._patch_operation is not operation or self.snapshot() != before:
            raise SubmissionPatchError(
                "submission_superseded", "The submission draft changed while validation was pending.",
                {"saved_fields": self.snapshot()["saved_fields"]},
            )
        self.full_text = candidate.full_text
        self.agent_text = candidate.agent_text
        self.revision = candidate.revision
        self.errors = candidate.errors
        if error is not None:
            raise error
        assert result is not None
        return result


def validate_submission(
    full_text: object, agent_text: object, *, counter: TokenCounter | None = None,
) -> dict[str, Any]:
    """Validate mechanics only; never interpret or rewrite domain content."""

    counter = counter if counter is not None else get_token_counter()
    full = validate_full_text(full_text)
    handoff = validate_agent_text(agent_text, counter=counter)
    handoff_tokens, tokenizer = count_handoff_tokens(handoff, counter=counter)
    full_tokens, _ = count_handoff_tokens(full, counter=counter)
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


def attach_agent_handoff(result: object, *, counter: TokenCounter | None = None) -> dict[str, Any]:
    """Attach an already-authored handoff without deriving any game meaning."""

    counter = counter if counter is not None else get_token_counter()
    source = dict(result) if isinstance(result, Mapping) else {}
    status = str(source.get("status") or "degraded").lower()
    answer = source.get("answer")
    agent_text = source.get("agent_text")
    submission_mode = str(source.get("submission_mode") or "unknown")
    try:
        if agent_text is None and status == "ok":
            agent_text = answer
            submission_mode = "text_fallback"
        validated = validate_submission(answer, agent_text, counter=counter)
        text = validated["agent_text"]
        delivery = dict(validated["delivery"])
        delivery["submission_mode"] = submission_mode
    except HandoffValidationError as exception:
        text = _MISSING_HANDOFF
        handoff_tokens, tokenizer = count_handoff_tokens(text, counter=counter)
        full_tokens = count_handoff_tokens(answer, counter=counter)[0] if isinstance(answer, str) else None
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
