"""Append-only, process-local audit state for independent agent runs."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from ..state import redact_text, utc_now
from .usage import combine_run_summaries, summarize_usage

MAX_RECENT_RUNS = 20
MAX_EVENTS_PER_RUN = 512
MAX_AUDIT_TEXT_CHARS = 131_072
MAX_AUDIT_DEPTH = 40  # Protocol depth 32 plus observation, tool-result and audit wrappers.
AUDIT_CHUNK_CHARS = 1_800

_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "pairing_token",
        "password",
        "private_key",
        "proof",
        "secret",
        "token",
    }
)
_SENSITIVE_SUFFIXES = ("_api_key", "_access_token", "_private_key", "_secret", "_password", "_proof", "_token")


class AgentRunState(StrEnum):
    CREATED = "created"
    MODEL_REQUESTING = "model_requesting"
    MODEL_RESPONDED = "model_responded"
    TOOL_DISPATCHING = "tool_dispatching"
    TOOL_RESULTS_READY = "tool_results_ready"
    FINALIZING = "finalizing"
    DEGRADED = "degraded"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


_ALLOWED_TRANSITIONS: dict[AgentRunState, frozenset[AgentRunState]] = {
    AgentRunState.CREATED: frozenset(
        {AgentRunState.MODEL_REQUESTING, AgentRunState.DEGRADED, AgentRunState.CANCELLED, AgentRunState.FAILED}
    ),
    AgentRunState.MODEL_REQUESTING: frozenset(
        {AgentRunState.MODEL_RESPONDED, AgentRunState.DEGRADED, AgentRunState.CANCELLED, AgentRunState.FAILED}
    ),
    AgentRunState.MODEL_RESPONDED: frozenset(
        {
            AgentRunState.MODEL_REQUESTING,
            AgentRunState.TOOL_DISPATCHING,
            AgentRunState.FINALIZING,
            AgentRunState.DEGRADED,
            AgentRunState.CANCELLED,
            AgentRunState.FAILED,
        }
    ),
    AgentRunState.TOOL_DISPATCHING: frozenset(
        {AgentRunState.TOOL_RESULTS_READY, AgentRunState.DEGRADED, AgentRunState.CANCELLED, AgentRunState.FAILED}
    ),
    AgentRunState.TOOL_RESULTS_READY: frozenset(
        {
            AgentRunState.MODEL_REQUESTING,
            AgentRunState.FINALIZING,
            AgentRunState.DEGRADED,
            AgentRunState.CANCELLED,
            AgentRunState.FAILED,
        }
    ),
    AgentRunState.DEGRADED: frozenset({AgentRunState.FINALIZING, AgentRunState.CANCELLED, AgentRunState.FAILED}),
    AgentRunState.FINALIZING: frozenset(
        {AgentRunState.COMPLETED, AgentRunState.DEGRADED, AgentRunState.CANCELLED, AgentRunState.FAILED}
    ),
    AgentRunState.COMPLETED: frozenset(),
    AgentRunState.CANCELLED: frozenset(),
    AgentRunState.FAILED: frozenset(),
}


class AgentAuditError(RuntimeError):
    """Raised when the runtime violates its explicit audit state contract."""


class AgentAuditCapacityError(AgentAuditError):
    """Raised after the ledger records its own bounded-capacity terminal event."""


def _safe_key(value: object) -> str:
    return redact_text(value, 128)


def _chunked_text(value: object) -> str | dict[str, Any]:
    original = str(value)
    redacted = redact_text(original, MAX_AUDIT_TEXT_CHARS)
    digest = hashlib.sha256(redacted.encode("utf-8", "replace")).hexdigest()
    if len(redacted) <= AUDIT_CHUNK_CHARS:
        return redacted
    return {
        "text_chunks": [
            redacted[index : index + AUDIT_CHUNK_CHARS] for index in range(0, len(redacted), AUDIT_CHUNK_CHARS)
        ],
        "chars": len(redacted),
        "sha256": digest,
        "truncated": len(original) > len(redacted),
    }


def audit_safe(value: object, *, _depth: int = 0) -> Any:
    """Return JSON-only audit data with credentials removed and long text chunked."""

    if _depth >= MAX_AUDIT_DEPTH:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in {float("inf"), float("-inf")} else None
    if isinstance(value, str):
        return _chunked_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 65_536:
                result["_truncated_items"] = True
                break
            key = _safe_key(raw_key)
            normalized = key.lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES):
                result[key] = "<redacted>"
            else:
                result[key] = audit_safe(item, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        rows = list(value)
        result = [audit_safe(item, _depth=_depth + 1) for item in rows[:65_536]]
        if len(rows) > 65_536:
            result.append({"_truncated_items": len(rows) - 65_536})
        return result
    return f"<{type(value).__name__}>"


class AgentAuditLedger:
    """One run's immutable event sequence plus a derived current state."""

    def __init__(self, question: str) -> None:
        self.run_id = uuid.uuid4().hex
        self._question_preview = redact_text(question, 300)
        self._lock = threading.RLock()
        self._state = AgentRunState.CREATED
        self._cycle = 0
        self._created_at = utc_now()
        self._updated_at = self._created_at
        self._started_monotonic = time.monotonic()
        self._ended_monotonic: float | None = None
        self._events: list[dict[str, Any]] = []
        self._result: dict[str, Any] | None = None
        self._draft: dict[str, Any] | None = None
        self._append_locked(
            "run_created",
            state_before=None,
            state_after=AgentRunState.CREATED,
            data={"question": question},
        )

    @property
    def state(self) -> AgentRunState:
        with self._lock:
            return self._state

    @property
    def cycle(self) -> int:
        with self._lock:
            return self._cycle

    @property
    def elapsed_seconds(self) -> float:
        return max(
            0.0,
            (self._ended_monotonic if self._ended_monotonic is not None else time.monotonic())
            - self._started_monotonic,
        )

    def begin_cycle(self) -> int:
        with self._lock:
            self._cycle += 1
            return self._cycle

    def record(self, event: str, data: Mapping[str, object] | None = None) -> None:
        with self._lock:
            self._ensure_capacity_locked()
            self._append_locked(
                event,
                state_before=self._state,
                state_after=self._state,
                data=data,
            )

    def transition(
        self,
        state: AgentRunState,
        event: str,
        data: Mapping[str, object] | None = None,
    ) -> None:
        with self._lock:
            before = self._state
            if state not in _ALLOWED_TRANSITIONS[before]:
                raise AgentAuditError(f"invalid agent transition: {before.value} -> {state.value}")
            self._ensure_capacity_locked()
            self._state = state
            if state in {AgentRunState.COMPLETED, AgentRunState.CANCELLED, AgentRunState.FAILED}:
                self._ended_monotonic = time.monotonic()
            self._append_locked(event, state_before=before, state_after=state, data=data)

    def set_draft(self, draft: Mapping[str, Any]) -> None:
        with self._lock:
            self._draft = audit_safe(dict(draft))

    def set_result(self, result: Mapping[str, object]) -> None:
        with self._lock:
            self._result = audit_safe(dict(result))
            self._updated_at = utc_now()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            event_counts: dict[str, int] = {}
            for item in self._events:
                name = str(item.get("event") or "unknown")
                event_counts[name] = event_counts.get(name, 0) + 1
            return {
                "run_id": self.run_id,
                "question": self._question_preview,
                "draft_available": bool(self._draft and self._draft.get("full_text")),
                "result_status": self._result.get("status") if self._result else None,
                "result_summary": self._result.get("summary") if self._result else None,
                "state": self._state.value,
                "cycle": self._cycle,
                "created_at": self._created_at,
                "updated_at": self._updated_at,
                "duration_ms": round(self.elapsed_seconds * 1000.0, 2),
                "event_count": len(self._events),
                "model_calls": event_counts.get("model_request_started", 0),
                "model_responses": event_counts.get("model_response_received", 0),
                "model_failures": event_counts.get("model_request_failed", 0),
                "model_cancellations": event_counts.get("model_request_cancelled", 0),
                "usage": summarize_usage(
                    event_counts.get("model_request_started", 0),
                    (
                        event["data"].get("usage")
                        for event in self._events
                        if event["event"]
                        in {"model_response_received", "model_request_failed", "model_request_cancelled"}
                    ),
                ),
                "latest_request": next(
                    (
                        event["data"].get("diagnostics")
                        for event in reversed(self._events)
                        if event["event"]
                        in {"model_response_received", "model_request_failed", "model_request_cancelled"}
                        and event["data"].get("diagnostics")
                    ),
                    None,
                ),
                "tool_calls": event_counts.get("tool_call_requested", 0),
                "tool_failures": event_counts.get("tool_failed", 0),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = self.summary()
            result["events"] = copy.deepcopy(self._events)
            result["result"] = copy.deepcopy(self._result)
            result["draft"] = copy.deepcopy(self._draft)
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return result

    def details(self, event_offset: int = 0, event_limit: int = 50) -> dict[str, Any]:
        with self._lock:
            return {
                **self.summary(),
                "events": copy.deepcopy(self._events[event_offset : event_offset + event_limit]),
                "events_page": {
                    "offset": event_offset,
                    "limit": event_limit,
                    "total": len(self._events),
                    "has_more": event_offset + event_limit < len(self._events),
                },
                "result": copy.deepcopy(self._result),
                "draft": copy.deepcopy(self._draft),
            }

    def _append_locked(
        self,
        event: str,
        *,
        state_before: AgentRunState | None,
        state_after: AgentRunState,
        data: Mapping[str, object] | None,
    ) -> None:
        if len(self._events) >= MAX_EVENTS_PER_RUN:
            raise AgentAuditError("agent audit event capacity reached")
        self._updated_at = utc_now()
        self._events.append(
            {
                "event_seq": len(self._events) + 1,
                "timestamp": self._updated_at,
                "elapsed_ms": round(self.elapsed_seconds * 1000.0, 2),
                "event": redact_text(event, 96),
                "cycle": self._cycle,
                "state_before": state_before.value if state_before is not None else None,
                "state_after": state_after.value,
                "data": audit_safe(dict(data or {})),
            }
        )

    def _ensure_capacity_locked(self) -> None:
        if len(self._events) < MAX_EVENTS_PER_RUN - 1:
            return
        before = self._state
        self._state = AgentRunState.FAILED
        self._ended_monotonic = time.monotonic()
        self._append_locked(
            "audit_capacity_reached",
            state_before=before,
            state_after=AgentRunState.FAILED,
            data={"max_events": MAX_EVENTS_PER_RUN},
        )
        raise AgentAuditCapacityError("agent audit event capacity reached")


class AgentAuditRegistry:
    """Bounded process-local run registry; never used as model memory."""

    def __init__(self, max_recent_runs: int = MAX_RECENT_RUNS) -> None:
        if not 1 <= max_recent_runs <= 100:
            raise ValueError("max_recent_runs must be between 1 and 100")
        self._lock = threading.RLock()
        self._active: dict[str, AgentAuditLedger] = {}
        self._recent: deque[AgentAuditLedger] = deque(maxlen=max_recent_runs)

    def start(self, ledger: AgentAuditLedger) -> None:
        with self._lock:
            self._active[ledger.run_id] = ledger

    def finish(self, ledger: AgentAuditLedger) -> None:
        with self._lock:
            removed = self._active.pop(ledger.run_id, None)
            if removed is not None and all(item.run_id != ledger.run_id for item in self._recent):
                self._recent.append(ledger)

    def get_details(self, run_id: str, event_offset: int = 0, event_limit: int = 50) -> dict[str, Any] | None:
        with self._lock:
            ledger = self._active.get(run_id) or next((item for item in self._recent if item.run_id == run_id), None)
        return ledger.details(event_offset, event_limit) if ledger is not None else None

    def snapshot(self, *, include_latest_details: bool = True) -> dict[str, Any]:
        with self._lock:
            active = list(self._active.values())
            recent = list(self._recent)
        active_summaries = [item.summary() for item in active]
        recent_summaries = [item.summary() for item in reversed(recent)]
        latest = active[-1] if active else (recent[-1] if recent else None)
        return {
            "totals": combine_run_summaries(recent_summaries + active_summaries),
            "retained_run_limit": self._recent.maxlen,
            "active_count": len(active_summaries),
            "recent_count": len(recent_summaries),
            "active_runs": active_summaries,
            "recent_runs": recent_summaries,
            "latest_run": (latest.snapshot() if include_latest_details else latest.summary())
            if latest is not None
            else None,
        }


__all__ = [
    "AgentAuditError",
    "AgentAuditCapacityError",
    "AgentAuditLedger",
    "AgentAuditRegistry",
    "AgentRunState",
    "audit_safe",
]
