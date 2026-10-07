"""Model attempt admission, retries and phase expiry; no tool vocabulary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from ..state import redact_text
from .audit import AgentRunState
from .contracts import AssistantTurn
from .model_client import ModelClientError

MAX_MODEL_ATTEMPTS = 3
MODEL_RETRY_DELAYS_SECONDS = (0.5, 1.5)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_RETRYABLE_PROVIDER_MARKERS = (
    "overload",
    "rate_limit",
    "rate limit",
    "server_error",
    "temporar",
    "timeout",
    "try again",
    "unavailable",
    "upstream",
)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _model_retry_reason(error: ModelClientError) -> str | None:
    diagnostics = error.diagnostics
    if diagnostics.get("first_useful_delta_ms") is not None:
        return None
    if error.code in {"model_transport", "model_transport_timeout", "model_first_response_timeout"}:
        return error.code
    if error.code == "model_incomplete_stream":
        return error.code
    if error.code == "model_http_error" and diagnostics.get("http_status") in _RETRYABLE_HTTP_STATUSES:
        return f"http_{diagnostics['http_status']}"
    if error.code != "model_stream_error":
        return None
    provider_error = diagnostics.get("provider_error")
    provider = provider_error if isinstance(provider_error, Mapping) else {}
    marker_text = " ".join(str(provider.get(key) or "").lower() for key in ("code", "type", "message"))
    return "provider_transient" if any(marker in marker_text for marker in _RETRYABLE_PROVIDER_MARKERS) else None


@dataclass(frozen=True, slots=True)
class ModelRequestResult:
    turn: AssistantTurn | None
    finalizing: bool
    error: ModelClientError | None = None


async def request_model(
    model,
    settings,
    ledger,
    session,
    request_messages,
    tool_schemas,
    *,
    finalizing,
    reserve,
    research_deadline,
    cycle,
    request_sha256,
    logger,
    max_attempts=MAX_MODEL_ATTEMPTS,
):
    attempt = 1
    turn = None
    while True:
        attempt_remaining = settings.run_budget_seconds - ledger.elapsed_seconds
        available = attempt_remaining if finalizing or reserve == 0 else research_deadline - ledger.elapsed_seconds
        if available <= 0 and not finalizing and reserve > 0:
            finalizing = True
            break
        request_timeout = min(
            settings.request_timeout_seconds,
            max(0.001, available),
        )
        request_event = {
            "model": settings.model,
            "base_url": settings.base_url,
            "timeout_seconds": request_timeout,
            "messages": request_messages,
            "tools": tool_schemas,
            "request_sha256": request_sha256,
            "attempt": attempt,
            "max_attempts": max_attempts,
        }
        if attempt == 1 and ledger.state != AgentRunState.MODEL_REQUESTING:
            ledger.transition(AgentRunState.MODEL_REQUESTING, "model_request_started", request_event)
        else:
            ledger.record("model_request_started", request_event)
        session.model_calls += 1
        try:
            async with asyncio.timeout(request_timeout):
                turn = await model.complete(request_messages, tool_schemas, timeout=request_timeout)
            break
        except asyncio.CancelledError:
            ledger.record(
                "model_request_cancelled",
                {
                    "attempt": attempt,
                    "diagnostics": getattr(model, "last_diagnostics", {}),
                    "usage": getattr(model, "last_usage", {}),
                },
            )
            raise
        except (ModelClientError, TimeoutError) as exception:
            if isinstance(exception, TimeoutError):
                exception = ModelClientError(
                    "model_request_deadline",
                    "The model request reached its phase deadline.",
                    diagnostics=getattr(model, "last_diagnostics", {}),
                    usage=getattr(model, "last_usage", {}),
                )
            if reserve > 0 and not finalizing and ledger.elapsed_seconds >= research_deadline:
                ledger.record(
                    "research_request_expired",
                    {"error_code": exception.code, "diagnostics": exception.diagnostics, "usage": exception.usage},
                )
                finalizing = True
                break
            retry_reason = _model_retry_reason(exception)
            retry_delay = MODEL_RETRY_DELAYS_SECONDS[min(attempt - 1, len(MODEL_RETRY_DELAYS_SECONDS) - 1)]
            remaining_after_failure = settings.run_budget_seconds - ledger.elapsed_seconds
            retry_available = (
                remaining_after_failure if finalizing or reserve == 0 else research_deadline - ledger.elapsed_seconds
            )
            retry_budget_floor = 3.0
            will_retry = (
                retry_reason is not None
                and attempt < max_attempts
                and retry_available > retry_delay + retry_budget_floor
            )
            failure = {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "error_code": exception.code,
                "message": str(exception),
                "diagnostics": exception.diagnostics,
                "usage": exception.usage,
                "retryable": retry_reason is not None,
                "retry_reason": retry_reason,
                "will_retry": will_retry,
            }
            provider_error = exception.diagnostics.get("provider_error")
            provider = provider_error if isinstance(provider_error, Mapping) else {}
            log_method = logger.warning if will_retry else logger.error
            log_method(
                "[agent] model request {} run_id={} cycle={} attempt={}/{} error_code={} request_id={} "
                "phase={} provider_code={} provider_type={} provider_param={} provider_message={}",
                "retry scheduled" if will_retry else "failed",
                ledger.run_id,
                cycle,
                attempt,
                max_attempts,
                exception.code,
                redact_text(exception.diagnostics.get("request_id"), 128),
                redact_text(exception.diagnostics.get("phase"), 64),
                redact_text(provider.get("code"), 128),
                redact_text(provider.get("type"), 128),
                redact_text(provider.get("param"), 128),
                redact_text(provider.get("message"), 512),
            )
            if will_retry:
                ledger.record("model_request_attempt_failed", failure)
                ledger.record(
                    "model_retry_scheduled",
                    {
                        "failed_attempt": attempt,
                        "next_attempt": attempt + 1,
                        "delay_seconds": retry_delay,
                        "retry_reason": retry_reason,
                        "remaining_seconds": round(remaining_after_failure, 3),
                    },
                )
                await asyncio.sleep(retry_delay)
                attempt += 1
                continue
            if (
                retry_reason is not None
                and attempt < max_attempts
                and reserve > 0
                and not finalizing
                and remaining_after_failure > 0
            ):
                ledger.record("research_request_failed", failure)
                finalizing = True
                break
            ledger.transition(AgentRunState.DEGRADED, "model_request_failed", failure)
            return ModelRequestResult(None, finalizing, error=exception)

    return ModelRequestResult(turn, finalizing)
