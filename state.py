"""Thread-safe, presentation-safe bridge status and diagnostics."""

from __future__ import annotations

import copy
import json
import math
import re
import threading
import uuid
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .protocol import PROTOCOL_VERSION


class BridgeStatus:
    """Stable coarse states suitable for UI and health reporting."""

    STOPPED = "stopped"
    STARTING = "starting"
    LISTENING = "listening"
    AUTHENTICATING = "authenticating"
    READY = "ready"
    STALE = "stale"
    STOPPING = "stopping"
    ERROR = "error"

    ALL = frozenset({STOPPED, STARTING, LISTENING, AUTHENTICATING, READY, STALE, STOPPING, ERROR})


_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_nonce",
        "frame",
        "pairing_token",
        "password",
        "proof",
        "private_key",
        "raw_frame",
        "secret",
        "server_nonce",
        "token",
    }
)
_IDENTIFIER_KEYS = frozenset({"colony_id", "connection_epoch", "load_epoch", "request_id", "session_id"})
_SENSITIVE_SUFFIXES = ("_api_key", "_access_token", "_private_key", "_secret", "_password", "_proof", "_token")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)[\"']?\b((?:[a-z0-9_.-]+_)?(?:token|proof|password|secret|api_key|access_token|private_key)"
    r"|authorization|client_nonce|server_nonce)\b[\"']?\s*[:=]\s*[^,;\r\n]+"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact_text(value: object, limit: int = 512) -> str:
    """Return bounded text without common credential assignments."""

    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", str(value))
    text = _BEARER_VALUE.sub("Bearer <redacted>", text)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}…"


def mask_identifier(value: str | None) -> str | None:
    """Keep IDs useful for diagnosis without exposing their full value."""

    if not value:
        return None
    if value == "<redacted>" or "…" in value:
        return value
    if len(value) <= 12:
        return "<redacted>"
    return f"{value[:8]}…{value[-4:]}"


def json_safe_copy(value: object) -> Any:
    """Convert arbitrary input to a bounded tree containing JSON values only."""

    safe = _json_safe(value, depth=0)
    # This is an invariant check, not the copy mechanism.
    json.dumps(safe, allow_nan=False, ensure_ascii=False)
    return safe


def _json_safe(value: object, *, depth: int) -> Any:
    if depth >= 8:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_text(value, 2048)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 128:
                result["_truncated"] = True
                break
            name = redact_text(key, 128)
            normalized = name.lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES):
                result[name] = "<redacted>"
            elif normalized in _IDENTIFIER_KEYS and isinstance(item, str):
                result[name] = mask_identifier(item)
            else:
                result[name] = _json_safe(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:256]]
    return f"<{type(value).__name__}>"


class DiagnosticRing:
    """A bounded, thread-safe diagnostic event ring."""

    MAX_EVENTS = 200
    MAX_MESSAGE_LENGTH = 512
    MAX_CONTEXT_BYTES = 2048

    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        if not 1 <= max_events <= self.MAX_EVENTS:
            raise ValueError(f"max_events must be between 1 and {self.MAX_EVENTS}")
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._sequence = 0

    def record(
        self,
        level: str,
        category: str,
        message: object,
        *,
        incident_id: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> str:
        incident = redact_text(incident_id or uuid.uuid4().hex[:12], 64)
        with self._lock:
            self._sequence += 1
            event: dict[str, Any] = {
                "sequence": self._sequence,
                "timestamp": utc_now(),
                "level": redact_text(level, 32).lower(),
                "category": redact_text(category, 64),
                "message": redact_text(message, self.MAX_MESSAGE_LENGTH),
                "incident_id": incident,
            }
            if context:
                safe_context = json_safe_copy(context)
                encoded_context = json.dumps(safe_context, allow_nan=False, ensure_ascii=False).encode("utf-8")
                event["context"] = (
                    safe_context
                    if len(encoded_context) <= self.MAX_CONTEXT_BYTES
                    else {"truncated": True, "encoded_bytes": len(encoded_context)}
                )
            self._events.append(event)
        return incident

    add = record

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(self._events))

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


class BridgeStateStore:
    """Owns a small JSON-only view of bridge state for tools and UI."""

    def __init__(self, diagnostics: DiagnosticRing | None = None) -> None:
        self._lock = threading.RLock()
        self._diagnostics = diagnostics or DiagnosticRing()
        now = utc_now()
        self._state: dict[str, Any] = {
            "status": BridgeStatus.STOPPED,
            "endpoint": None,
            "port": None,
            "worker_alive": False,
            "connection": None,
            "session": None,
            "metrics": {"pending_requests": 0, "outbound_depth": 0},
            "protocol": {
                "version": PROTOCOL_VERSION,
                "rx_frames": 0,
                "tx_frames": 0,
                "auth_failures": 0,
                "invalid_frames": 0,
                "last_rx_at": None,
                "last_tx_at": None,
            },
            "last_query": None,
            "last_error_code": None,
            "last_incident": None,
            "updated_at": now,
        }

    @property
    def diagnostics(self) -> DiagnosticRing:
        return self._diagnostics

    def transition(self, status: str, **fields: object) -> dict[str, Any]:
        if status not in BridgeStatus.ALL:
            raise ValueError(f"unsupported bridge status: {status!r}")
        fields["status"] = status
        return self.update(**fields)

    def update(self, **fields: object) -> dict[str, Any]:
        safe_fields = json_safe_copy(fields)
        with self._lock:
            self._state.update(safe_fields)
            self._state["updated_at"] = utc_now()
            return copy.deepcopy(self._state)

    def set_connection(
        self,
        *,
        connection_epoch: str,
        mod_version: str | None,
        game_version: str | None,
        capabilities: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        return self.update(
            connection={
                "connection_epoch": mask_identifier(connection_epoch),
                "mod_version": mod_version,
                "game_version": game_version,
                "capabilities": list(capabilities)[:64],
                "authenticated_at": utc_now(),
            }
        )

    def set_session(
        self,
        *,
        colony_id: str,
        load_epoch: str,
        game_tick: int,
        snapshot_sequence: int,
        stale: bool = False,
    ) -> dict[str, Any]:
        return self.update(
            session={
                "colony_id": mask_identifier(colony_id),
                "load_epoch": mask_identifier(load_epoch),
                "game_tick": max(0, int(game_tick)),
                "snapshot_sequence": max(0, int(snapshot_sequence)),
                "stale": bool(stale),
            }
        )

    def set_metrics(self, *, pending_requests: int, outbound_depth: int) -> dict[str, Any]:
        return self.update(
            metrics={
                "pending_requests": max(0, int(pending_requests)),
                "outbound_depth": max(0, int(outbound_depth)),
            }
        )

    def set_worker_alive(self, alive: bool) -> dict[str, Any]:
        return self.update(worker_alive=bool(alive))

    def record_protocol(
        self,
        *,
        rx_frames: int = 0,
        tx_frames: int = 0,
        auth_failures: int = 0,
        invalid_frames: int = 0,
    ) -> dict[str, Any]:
        increments = {
            "rx_frames": max(0, int(rx_frames)),
            "tx_frames": max(0, int(tx_frames)),
            "auth_failures": max(0, int(auth_failures)),
            "invalid_frames": max(0, int(invalid_frames)),
        }
        with self._lock:
            protocol = self._state["protocol"]
            for key, increment in increments.items():
                protocol[key] = min((1 << 63) - 1, int(protocol[key]) + increment)
            now = utc_now()
            if increments["rx_frames"]:
                protocol["last_rx_at"] = now
            if increments["tx_frames"]:
                protocol["last_tx_at"] = now
            self._state["updated_at"] = now
            return copy.deepcopy(self._state)

    def set_last_query(
        self,
        status: str,
        latency_ms: float,
        *,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        return self.update(
            last_query={
                "status": redact_text(status, 64),
                "latency_ms": round(min(60_000.0, max(0.0, float(latency_ms))), 2),
                "error_code": redact_text(error_code, 128) if error_code else None,
                "completed_at": utc_now(),
            }
        )

    def record_incident(
        self,
        category: str,
        message: object,
        *,
        level: str = "error",
        error_code: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> str:
        incident = self._diagnostics.record(level, category, message, context=context)
        self.update(
            last_incident={
                "incident_id": incident,
                "category": redact_text(category, 64),
                "message": redact_text(message, DiagnosticRing.MAX_MESSAGE_LENGTH),
                "timestamp": utc_now(),
            },
            last_error_code=redact_text(error_code or category, 128),
        )
        return incident

    def coarse_status(self) -> dict[str, Any]:
        """Return the complete, intentionally small host status contract."""

        with self._lock:
            status = self._state["status"]
            connection = self._state["connection"]
            session = self._state["session"]
            result = {
                "status": status,
                "connected": status in {BridgeStatus.AUTHENTICATING, BridgeStatus.READY, BridgeStatus.STALE},
                "authenticated": connection is not None,
                "session_open": session is not None and not bool(session.get("stale", False)),
                "protocol_version": int(self._state["protocol"]["version"]),
                "port": self._state["port"],
                "last_error_code": self._state["last_error_code"],
                "worker_alive": bool(self._state["worker_alive"]),
            }
        return json_safe_copy(result)

    def snapshot(self, *, include_diagnostics: bool = True) -> dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._state)
        if include_diagnostics:
            result["diagnostics"] = self._diagnostics.snapshot()
        # Keep the public contract explicitly JSON-only.
        return json_safe_copy(result)
