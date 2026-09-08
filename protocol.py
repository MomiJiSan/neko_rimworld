"""Strict protocol-v2 codec for the local N.E.K.O/RimWorld bridge.

This module deliberately has no logging side effects.  Callers may log an
exception's ``error_code`` and class name, but must not log pairing tokens or
raw frames.  Decode paths return typed envelopes containing detached JSON-safe values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import socket
import struct
import time
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, BinaryIO, Final, Mapping, Sequence

PROTOCOL_VERSION: Final = 2
AUTH_DOMAIN: Final = "neko-rimworld-bridge/v2"
MAX_MOD_TO_PLUGIN_FRAME_BYTES: Final = 1024 * 1024
MAX_PLUGIN_TO_MOD_FRAME_BYTES: Final = 64 * 1024
MAX_HANDSHAKE_FRAME_BYTES: Final = 64 * 1024
DEFAULT_WRITE_TIMEOUT_SECONDS: Final = 2.0
MAX_JSON_DEPTH: Final = 32
MAX_JSON_ITEMS: Final = 65_536

EMPTY_ID: Final = ""
NO_GAME_COUNTER: Final = -1

HELLO: Final = "hello"
CHALLENGE: Final = "challenge"
AUTHENTICATE: Final = "authenticate"
WELCOME: Final = "welcome"
SESSION_OPEN: Final = "session_open"
SESSION_STALE: Final = "session_stale"
HEARTBEAT: Final = "heartbeat"
QUERY_REQUEST: Final = "query_request"
QUERY_RESULT: Final = "query_result"
PROTOCOL_ERROR: Final = "protocol_error"

OK: Final = "ok"
NOT_FOUND: Final = "not_found"
STALE_SESSION: Final = "stale_session"
UNSUPPORTED: Final = "unsupported"
BUSY: Final = "busy"
PARTIAL: Final = "partial"
FAILED: Final = "failed"

INVALID_ENVELOPE: Final = "invalid_envelope"
INVALID_FIELD: Final = "invalid_field"
INVALID_PAYLOAD: Final = "invalid_payload"
UNSUPPORTED_VERSION: Final = "unsupported_version"
EMPTY_FRAME: Final = "empty_frame"
FRAME_TOO_LARGE: Final = "frame_too_large"
UNEXPECTED_END_OF_STREAM: Final = "unexpected_end_of_stream"
READ_DEADLINE_EXCEEDED: Final = "read_deadline_exceeded"
WRITE_DEADLINE_EXCEEDED: Final = "write_deadline_exceeded"
INVALID_JSON: Final = "invalid_json"


_MESSAGE_TYPES: Final = frozenset(
    {
        HELLO,
        CHALLENGE,
        AUTHENTICATE,
        WELCOME,
        SESSION_OPEN,
        SESSION_STALE,
        HEARTBEAT,
        QUERY_REQUEST,
        QUERY_RESULT,
        PROTOCOL_ERROR,
    }
)
_STATUSES: Final = frozenset({OK, NOT_FOUND, STALE_SESSION, UNSUPPORTED, BUSY, PARTIAL, FAILED})
_ENVELOPE_FIELDS: Final = frozenset(
    {
        "v",
        "type",
        "connection_epoch",
        "colony_id",
        "load_epoch",
        "seq",
        "request_id",
        "game_tick",
        "snapshot_sequence",
        "payload",
    }
)
_PAYLOAD_FIELDS: Final = {
    HELLO: frozenset({"mod_version", "game_version", "capabilities", "client_nonce"}),
    CHALLENGE: frozenset({"client_nonce", "server_nonce", "proof"}),
    AUTHENTICATE: frozenset({"client_nonce", "server_nonce", "proof"}),
    WELCOME: frozenset({"status"}),
    SESSION_OPEN: frozenset(),
    SESSION_STALE: frozenset({"reason"}),
    HEARTBEAT: frozenset(),
    QUERY_REQUEST: frozenset({"operation", "arguments"}),
    QUERY_RESULT: frozenset({"status", "data", "issues", "execution", "error_code", "message"}),
    PROTOCOL_ERROR: frozenset({"error_code", "message"}),
}


class ProtocolError(Exception):
    """Base error with a wire-safe code and a non-sensitive message."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code

    @property
    def safe_summary(self) -> str:
        """A stable summary suitable for logs; excludes frame and token data."""

        return f"{type(self).__name__}({self.error_code})"


class FrameError(ProtocolError):
    """The length-prefixed transport frame is invalid or incomplete."""


class ValidationError(ProtocolError):
    """JSON, envelope, payload, direction, or phase validation failed."""


class AuthenticationError(ProtocolError):
    """Authentication material or a proof is invalid."""


# Compatibility aliases keep call sites expressive without multiplying error
# policy: error_code remains the machine-readable discriminator.
DecodeError = ValidationError
DirectionError = ValidationError
ReadDeadlineExceeded = FrameError
WriteDeadlineExceeded = FrameError


class Direction(str, Enum):
    MOD_TO_PLUGIN = "mod_to_plugin"
    PLUGIN_TO_MOD = "plugin_to_mod"


_PRE_AUTH_DIRECTIONS: Final = {
    Direction.MOD_TO_PLUGIN: frozenset({HELLO, AUTHENTICATE}),
    Direction.PLUGIN_TO_MOD: frozenset({CHALLENGE, WELCOME}),
}
_POST_AUTH_DIRECTIONS: Final = {
    Direction.MOD_TO_PLUGIN: frozenset({SESSION_OPEN, SESSION_STALE, HEARTBEAT, QUERY_RESULT, PROTOCOL_ERROR}),
    Direction.PLUGIN_TO_MOD: frozenset({QUERY_REQUEST, HEARTBEAT, PROTOCOL_ERROR}),
}
_ALL_DIRECTIONS: Final = {
    direction: _PRE_AUTH_DIRECTIONS[direction] | _POST_AUTH_DIRECTIONS[direction] for direction in Direction
}


class _JsonDto:
    def to_dict(self) -> dict[str, Any]:
        """Return detached JSON primitives, omitting optional ``None`` fields."""

        return _dto_dict(self)


@dataclass(frozen=True, slots=True)
class SessionDescriptor:
    colony_id: str
    load_epoch: str
    game_tick: int
    snapshot_sequence: int


@dataclass(frozen=True, slots=True)
class Payload(_JsonDto):
    mod_version: str | None = None
    game_version: str | None = None
    capabilities: tuple[str, ...] | None = None
    client_nonce: str | None = None
    server_nonce: str | None = None
    proof: str | None = None
    operation: str | None = None
    arguments: dict[str, Any] | None = None
    status: str | None = None
    data: dict[str, Any] | None = None
    issues: list[dict[str, Any]] | None = None
    execution: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Envelope(_JsonDto):
    v: int
    type: str
    connection_epoch: str
    colony_id: str
    load_epoch: str
    seq: int
    request_id: str
    game_tick: int
    snapshot_sequence: int
    payload: Payload

    @property
    def session(self) -> SessionDescriptor | None:
        if not self.colony_id:
            return None
        return SessionDescriptor(self.colony_id, self.load_epoch, self.game_tick, self.snapshot_sequence)


def validate_pairing_token(token: str) -> None:
    """Validate the deployable 32-128 character pairing-token contract."""

    if not isinstance(token, str):
        raise AuthenticationError(INVALID_PAYLOAD, "Pairing token must contain 32-128 characters.")
    try:
        token_length = _text_length(token)
    except ValidationError as exc:
        raise AuthenticationError(INVALID_PAYLOAD, "Pairing token is not valid Unicode text.") from exc
    if not 32 <= token_length <= 128:
        raise AuthenticationError(INVALID_PAYLOAD, "Pairing token must contain 32-128 characters.")
    _encode_utf8(token, "Pairing token is not valid UTF-8 text.", AuthenticationError)


def generate_nonce() -> str:
    return secrets.token_hex(32)


def compute_server_proof(token: str, connection_epoch: str, client_nonce: str, server_nonce: str) -> str:
    return _compute_proof(token, "server", connection_epoch, client_nonce, server_nonce)


def compute_client_proof(token: str, connection_epoch: str, client_nonce: str, server_nonce: str) -> str:
    return _compute_proof(token, "client", connection_epoch, client_nonce, server_nonce)


def verify_proof(expected: str | None, actual: str | None) -> bool:
    """Compare canonical proofs without leaking length or mismatch position."""

    if not _is_canonical_hex(expected, 64) or not _is_canonical_hex(actual, 64):
        return False
    return hmac.compare_digest(expected, actual)


def make_envelope(
    message_type: str,
    connection_epoch: str,
    seq: int,
    payload: Payload | None = None,
    session: SessionDescriptor | None = None,
    request_id: str = EMPTY_ID,
) -> Envelope:
    envelope = Envelope(
        v=PROTOCOL_VERSION,
        type=message_type,
        connection_epoch=connection_epoch,
        colony_id=session.colony_id if session is not None else EMPTY_ID,
        load_epoch=session.load_epoch if session is not None else EMPTY_ID,
        seq=seq,
        request_id=request_id,
        game_tick=session.game_tick if session is not None else NO_GAME_COUNTER,
        snapshot_sequence=session.snapshot_sequence if session is not None else NO_GAME_COUNTER,
        payload=payload or Payload(),
    )
    validate_envelope(envelope)
    return envelope


def make_challenge(
    hello: Envelope,
    seq: int,
    server_nonce: str,
    server_proof: str,
) -> Envelope:
    validate_auth_phase(hello, authenticated=False, direction=Direction.MOD_TO_PLUGIN)
    if hello.type != HELLO or hello.payload.client_nonce is None:
        raise DirectionError(INVALID_ENVELOPE, "A challenge can only answer a valid hello message.")
    return make_envelope(
        CHALLENGE,
        hello.connection_epoch,
        seq,
        Payload(client_nonce=hello.payload.client_nonce, server_nonce=server_nonce, proof=server_proof),
    )


def make_welcome(connection_epoch: str, seq: int) -> Envelope:
    return make_envelope(WELCOME, connection_epoch, seq, Payload(status=OK))


def make_heartbeat(
    connection_epoch: str,
    seq: int,
    session: SessionDescriptor | None = None,
) -> Envelope:
    return make_envelope(HEARTBEAT, connection_epoch, seq, session=session)


def make_query_request(
    connection_epoch: str,
    seq: int,
    session: SessionDescriptor,
    request_id: str,
    operation: str,
    arguments: dict[str, Any],
) -> Envelope:
    return make_envelope(
        QUERY_REQUEST,
        connection_epoch,
        seq,
        Payload(operation=operation, arguments=arguments),
        session=session,
        request_id=request_id,
    )


def make_protocol_error(
    connection_epoch: str,
    seq: int,
    error_code: str,
    message: str,
    request: Envelope | None = None,
    session: SessionDescriptor | None = None,
) -> Envelope:
    effective_session = session
    if effective_session is None and request is not None:
        effective_session = request.session
    return make_envelope(
        PROTOCOL_ERROR,
        connection_epoch,
        seq,
        Payload(error_code=error_code, message=message),
        session=effective_session,
        request_id=request.request_id if request is not None else EMPTY_ID,
    )


def validate_envelope(envelope: Envelope) -> Envelope:
    """Validate an in-memory envelope and return it unchanged."""

    if not isinstance(envelope, Envelope):
        raise ValidationError(INVALID_ENVELOPE, "Expected an Envelope instance.")
    try:
        raw = envelope.to_dict()
        _validate_json_limits(raw)
        canonical = _parse_envelope(raw)
    except ProtocolError:
        raise
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise ValidationError(INVALID_ENVELOPE, "Envelope object is not structurally valid.") from None
    if canonical != envelope:
        raise ValidationError(INVALID_ENVELOPE, "Envelope object is not canonical.")
    return envelope


def validate_direction(envelope: Envelope, direction: Direction | str) -> Envelope:
    validate_envelope(envelope)
    parsed_direction = _coerce_direction(direction)
    if envelope.type not in _ALL_DIRECTIONS[parsed_direction]:
        raise DirectionError(INVALID_ENVELOPE, "Message type is not valid in this peer direction.")
    return envelope


def validate_auth_phase(
    envelope: Envelope,
    *,
    authenticated: bool,
    direction: Direction | str,
) -> Envelope:
    """Enforce both peer direction and pre/post-authentication message sets."""

    validate_direction(envelope, direction)
    parsed_direction = _coerce_direction(direction)
    allowed = _POST_AUTH_DIRECTIONS[parsed_direction] if authenticated else _PRE_AUTH_DIRECTIONS[parsed_direction]
    if envelope.type not in allowed:
        phase = "authenticated" if authenticated else "authentication"
        raise DirectionError(INVALID_ENVELOPE, f"Message type is not valid in the {phase} phase.")
    return envelope


def serialize_envelope(envelope: Envelope, max_frame_bytes: int = MAX_MOD_TO_PLUGIN_FRAME_BYTES) -> bytes:
    _validate_frame_limit(max_frame_bytes)
    validate_envelope(envelope)
    try:
        encoded = json.dumps(
            envelope.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        raise DecodeError(INVALID_JSON, "Envelope serialization failed.") from None
    if not encoded:
        raise DecodeError(INVALID_JSON, "Envelope serialization produced an empty payload.")
    if len(encoded) > max_frame_bytes:
        raise FrameError(FRAME_TOO_LARGE, f"Encoded frame exceeds the {max_frame_bytes}-byte limit.")
    return encoded


def decode_envelope(
    data: bytes | bytearray | memoryview,
    *,
    max_frame_bytes: int = MAX_MOD_TO_PLUGIN_FRAME_BYTES,
    direction: Direction | str | None = None,
    authenticated: bool | None = None,
) -> Envelope:
    if authenticated is False:
        max_frame_bytes = min(max_frame_bytes, MAX_HANDSHAKE_FRAME_BYTES)
    _validate_frame_limit(max_frame_bytes)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise DecodeError(INVALID_JSON, "Protocol payload must be bytes.")
    raw = bytes(data)
    if not raw:
        raise FrameError(EMPTY_FRAME, "Empty JSON payload.")
    if len(raw) > max_frame_bytes:
        raise FrameError(FRAME_TOO_LARGE, f"Inbound frame exceeds the {max_frame_bytes}-byte limit.")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise DecodeError(INVALID_JSON, "Protocol payload is not valid UTF-8.") from None
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    except ProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError, TypeError):
        raise DecodeError(INVALID_JSON, "Invalid protocol JSON.") from None
    _validate_json_limits(value)
    envelope = _parse_envelope(value)
    if direction is not None:
        if authenticated is None:
            validate_direction(envelope, direction)
        else:
            validate_auth_phase(envelope, authenticated=authenticated, direction=direction)
    elif authenticated is not None:
        raise ValueError("authenticated requires a direction")
    return envelope


def encode_frame(envelope: Envelope, max_frame_bytes: int = MAX_PLUGIN_TO_MOD_FRAME_BYTES) -> bytes:
    payload = serialize_envelope(envelope, max_frame_bytes=max_frame_bytes)
    return struct.pack("!I", len(payload)) + payload


def read_frame(
    source: socket.socket | BinaryIO,
    deadline: float,
    max_frame_bytes: int = MAX_MOD_TO_PLUGIN_FRAME_BYTES,
    *,
    direction: Direction | str | None = None,
    authenticated: bool | None = None,
) -> Envelope:
    """Read one frame using a single absolute ``time.monotonic()`` deadline.

    Raw sockets get an OS-level remaining timeout for every receive. Generic
    streams are checked before and after every read; they should themselves use
    bounded reads if they can block independently.
    """

    if authenticated is False:
        max_frame_bytes = min(max_frame_bytes, MAX_HANDSHAKE_FRAME_BYTES)
    _validate_frame_limit(max_frame_bytes)
    absolute_deadline = _absolute_deadline(deadline)
    original_timeout: float | None | object = _NO_TIMEOUT
    if isinstance(source, socket.socket):
        original_timeout = source.gettimeout()
    try:
        header = _read_exact(source, 4, absolute_deadline)
        length = struct.unpack("!I", header)[0]
        if length == 0:
            raise FrameError(EMPTY_FRAME, "Zero-length frame.")
        if length > max_frame_bytes:
            raise FrameError(FRAME_TOO_LARGE, f"Inbound frame exceeds the {max_frame_bytes}-byte limit.")
        _check_deadline(absolute_deadline)
        payload = _read_exact(source, length, absolute_deadline)
        envelope = decode_envelope(
            payload,
            max_frame_bytes=max_frame_bytes,
            direction=direction,
            authenticated=authenticated,
        )
        _check_deadline(absolute_deadline)
        return envelope
    finally:
        if isinstance(source, socket.socket) and original_timeout is not _NO_TIMEOUT:
            try:
                source.settimeout(original_timeout)  # type: ignore[arg-type]
            except OSError:
                pass


def write_frame(
    destination: socket.socket | BinaryIO,
    envelope: Envelope,
    max_frame_bytes: int = MAX_PLUGIN_TO_MOD_FRAME_BYTES,
    *,
    deadline: float | None = None,
) -> None:
    """Write one complete frame before an absolute monotonic deadline.

    Omission uses a bounded two-second default; an explicit value is always an
    absolute ``time.monotonic()`` deadline. Sockets receive an OS-level
    remaining timeout which is restored afterward.
    Generic BinaryIO remains supported and is checked before and after each
    write/flush; such streams must make each individual operation bounded.
    A timeout makes delivery ambiguous, so callers must close the connection
    instead of retrying the same frame on it.
    """

    absolute_deadline = _absolute_deadline(
        time.monotonic() + DEFAULT_WRITE_TIMEOUT_SECONDS if deadline is None else deadline
    )
    _check_deadline(absolute_deadline, writing=True)
    frame = encode_frame(envelope, max_frame_bytes=max_frame_bytes)
    _check_deadline(absolute_deadline, writing=True)
    if isinstance(destination, socket.socket):
        original_timeout = destination.gettimeout()
        try:
            destination.settimeout(max(0.000_001, absolute_deadline - time.monotonic()))
            destination.sendall(frame)
            _check_deadline(absolute_deadline, writing=True)
            return
        except (socket.timeout, TimeoutError):
            raise WriteDeadlineExceeded(WRITE_DEADLINE_EXCEEDED, "Frame write deadline exceeded.") from None
        finally:
            try:
                destination.settimeout(original_timeout)
            except OSError:
                pass
    writer = getattr(destination, "write", None)
    if writer is None:
        raise TypeError("destination must be a socket or writable binary stream")
    view = memoryview(frame)
    offset = 0
    while offset < len(view):
        _check_deadline(absolute_deadline, writing=True)
        try:
            written = writer(view[offset:])
        except (socket.timeout, TimeoutError):
            raise WriteDeadlineExceeded(WRITE_DEADLINE_EXCEEDED, "Frame write deadline exceeded.") from None
        _check_deadline(absolute_deadline, writing=True)
        if not isinstance(written, int) or written <= 0:
            raise FrameError(UNEXPECTED_END_OF_STREAM, "Binary stream stopped while writing a frame.")
        offset += written
    flush = getattr(destination, "flush", None)
    if callable(flush):
        _check_deadline(absolute_deadline, writing=True)
        try:
            flush()
        except (socket.timeout, TimeoutError):
            raise WriteDeadlineExceeded(WRITE_DEADLINE_EXCEEDED, "Frame write deadline exceeded.") from None
        _check_deadline(absolute_deadline, writing=True)


def _compute_proof(token: str, role: str, connection_epoch: str, client_nonce: str, server_nonce: str) -> str:
    # Keep this broader 1-4096 precondition aligned with Protocol.cs. Runtime
    # configuration should additionally call validate_pairing_token().
    if not isinstance(token, str):
        raise AuthenticationError(INVALID_PAYLOAD, "Authentication token length is invalid.")
    try:
        token_length = _text_length(token)
    except ValidationError as exc:
        raise AuthenticationError(INVALID_PAYLOAD, "Authentication token is not valid Unicode text.") from exc
    if not 1 <= token_length <= 4096:
        raise AuthenticationError(INVALID_PAYLOAD, "Authentication token length is invalid.")
    _require_uuid(connection_epoch, "connection_epoch", allow_empty=False, error_type=AuthenticationError)
    _require_hex(client_nonce, "client_nonce", error_type=AuthenticationError)
    _require_hex(server_nonce, "server_nonce", error_type=AuthenticationError)
    key = _encode_utf8(token, "Authentication token is not valid UTF-8 text.", AuthenticationError)
    transcript = _encode_utf8(
        f"{AUTH_DOMAIN}/{role}\n{connection_epoch}\n{client_nonce}\n{server_nonce}",
        "Authentication transcript is not valid UTF-8 text.",
        AuthenticationError,
    )
    return hmac.new(key, transcript, hashlib.sha256).hexdigest()


def _parse_envelope(value: Any) -> Envelope:
    raw = _require_object(value, "Envelope root")
    _require_exact_fields(raw, _ENVELOPE_FIELDS, "Envelope")
    version = _require_int(raw, "v", minimum=-(2**31), maximum=2**31 - 1, scope="Envelope")
    if version != PROTOCOL_VERSION:
        raise ValidationError(UNSUPPORTED_VERSION, f"Expected protocol v{PROTOCOL_VERSION}.")
    message_type = _require_string(raw, "type", scope="Envelope", minimum=1, maximum=128)
    if message_type not in _MESSAGE_TYPES:
        raise ValidationError(INVALID_FIELD, "Envelope field 'type' is not a protocol v2 message type.")
    connection_epoch = _require_string(raw, "connection_epoch", scope="Envelope", minimum=1, maximum=32)
    colony_id = _require_string(raw, "colony_id", scope="Envelope", minimum=0, maximum=32, allow_blank=True)
    load_epoch = _require_string(raw, "load_epoch", scope="Envelope", minimum=0, maximum=32, allow_blank=True)
    request_id = _require_string(raw, "request_id", scope="Envelope", minimum=0, maximum=32, allow_blank=True)
    _require_uuid(connection_epoch, "connection_epoch", allow_empty=False)
    _require_uuid(colony_id, "colony_id", allow_empty=True)
    _require_uuid(load_epoch, "load_epoch", allow_empty=True)
    _require_uuid(request_id, "request_id", allow_empty=True)
    seq = _require_int(raw, "seq", minimum=0, maximum=2**63 - 1, scope="Envelope")
    game_tick = _require_int(raw, "game_tick", minimum=-1, maximum=2**63 - 1, scope="Envelope")
    snapshot_sequence = _require_int(raw, "snapshot_sequence", minimum=-1, maximum=2**63 - 1, scope="Envelope")
    has_session = bool(colony_id)
    if has_session != bool(load_epoch):
        raise ValidationError(INVALID_FIELD, "Envelope load_epoch must be present exactly when colony_id is present.")
    needs_session = message_type in {SESSION_OPEN, SESSION_STALE, QUERY_REQUEST, QUERY_RESULT}
    allows_session = needs_session or message_type in {HEARTBEAT, PROTOCOL_ERROR}
    if needs_session and not has_session:
        raise ValidationError(INVALID_FIELD, "Envelope colony_id is required for this message type.")
    if not allows_session and has_session:
        raise ValidationError(INVALID_FIELD, "Envelope colony_id is forbidden for this message type.")
    if has_session and (game_tick < 0 or snapshot_sequence < 0):
        raise ValidationError(INVALID_FIELD, "Session counters must be non-negative.")
    if not has_session and (game_tick != -1 or snapshot_sequence != -1):
        raise ValidationError(INVALID_FIELD, "Counters must be -1 without a session.")
    needs_request = message_type in {QUERY_REQUEST, QUERY_RESULT}
    allows_request = needs_request or message_type == PROTOCOL_ERROR
    if needs_request and not request_id:
        raise ValidationError(INVALID_FIELD, "request_id is required for this message type.")
    if not allows_request and request_id:
        raise ValidationError(INVALID_FIELD, "request_id is forbidden for this message type.")
    payload = _parse_payload(message_type, raw["payload"], game_tick, snapshot_sequence)
    return Envelope(
        version,
        message_type,
        connection_epoch,
        colony_id,
        load_epoch,
        seq,
        request_id,
        game_tick,
        snapshot_sequence,
        payload,
    )


def _parse_payload(message_type: str, value: Any, game_tick: int, snapshot_sequence: int) -> Payload:
    raw = _require_object(value, "Payload")
    _require_allowed_fields(raw, _PAYLOAD_FIELDS[message_type], "Payload")
    if message_type == HELLO:
        _require_present(raw, _PAYLOAD_FIELDS[HELLO], "Payload")
        capabilities_value = raw["capabilities"]
        if not isinstance(capabilities_value, list):
            raise ValidationError(INVALID_PAYLOAD, "capabilities must be an array.")
        if not 1 <= len(capabilities_value) <= 256:
            raise ValidationError(INVALID_PAYLOAD, "capabilities must contain 1-256 entries.")
        capabilities: list[str] = []
        seen: set[str] = set()
        for value_item in capabilities_value:
            capability = _required_text_value(value_item, "capability", 128)
            if capability in seen:
                raise ValidationError(INVALID_PAYLOAD, "capabilities cannot contain duplicates.")
            seen.add(capability)
            capabilities.append(capability)
        client_nonce = _required_text(raw, "client_nonce", 64)
        _require_hex(client_nonce, "client_nonce")
        parsed = dict(raw)
        parsed["capabilities"] = tuple(capabilities)
        return Payload(**parsed)
    if message_type in {CHALLENGE, AUTHENTICATE}:
        _require_present(raw, _PAYLOAD_FIELDS[message_type], "Payload")
        client_nonce = _required_text(raw, "client_nonce", 64)
        server_nonce = _required_text(raw, "server_nonce", 64)
        proof = _required_text(raw, "proof", 64)
        _require_hex(client_nonce, "client_nonce")
        _require_hex(server_nonce, "server_nonce")
        _require_hex(proof, "proof")
        return Payload(**raw)
    if message_type == WELCOME:
        _require_present(raw, {"status"}, "Payload")
        status = _required_text(raw, "status", 128)
        if status != OK:
            raise ValidationError(INVALID_PAYLOAD, "welcome status must be ok.")
        return Payload(**raw)
    if message_type == SESSION_STALE:
        _require_present(raw, {"reason"}, "Payload")
        _required_text(raw, "reason", 256)
        return Payload(**raw)
    if message_type == QUERY_REQUEST:
        _require_present(raw, {"operation", "arguments"}, "Payload")
        _required_text(raw, "operation", 128)
        _require_object(raw["arguments"], "arguments")
        return Payload(**raw)
    if message_type == QUERY_RESULT:
        return _parse_query_result(raw, game_tick, snapshot_sequence)
    if message_type == PROTOCOL_ERROR:
        _require_present(raw, {"error_code", "message"}, "Payload")
        _required_text(raw, "error_code", 128)
        _required_text(raw, "message", 2048)
        return Payload(**raw)
    # session_open and heartbeat have an exactly empty payload.
    return Payload()


def _parse_query_result(raw: Mapping[str, Any], game_tick: int, snapshot_sequence: int) -> Payload:
    if "status" not in raw:
        raise ValidationError(INVALID_PAYLOAD, "Query result status is required.")
    status = _required_text(raw, "status", 128)
    if status not in _STATUSES:
        raise ValidationError(INVALID_PAYLOAD, "Unknown query result status.")
    carries_data = status in {OK, PARTIAL}
    has_data = "data" in raw and raw["data"] is not None
    if carries_data != has_data:
        raise ValidationError(INVALID_PAYLOAD, "Only ok and partial query results carry data.")
    error_code = _optional_required_text(raw, "error_code", 128)
    message = _optional_required_text(raw, "message", 2048)
    if status == OK and (error_code is not None or message is not None):
        raise ValidationError(INVALID_PAYLOAD, "An ok query result cannot carry an error.")
    if not carries_data and (error_code is None or message is None):
        raise ValidationError(INVALID_PAYLOAD, "Failed query results require error_code and message.")
    if carries_data:
        _require_object(raw["data"], "Query data")
    if "execution" in raw:
        _require_object(raw["execution"], "execution")
    issues = raw.get("issues")
    if not isinstance(issues, list) or len(issues) > 256:
        raise ValidationError(INVALID_PAYLOAD, "issues must be an array of at most 256 entries.")
    if status == OK and issues or status == PARTIAL and not issues:
        raise ValidationError(INVALID_PAYLOAD, "partial requires issues; ok forbids issues.")
    for issue in issues:
        obj = _require_object(issue, "issue")
        _require_allowed_fields(obj, {"code", "path", "message", "omitted_count"}, "issue")
        _require_present(obj, {"code", "path"}, "issue")
        _required_text(obj, "code", 128)
        path = obj["path"]
        if not isinstance(path, list) or len(path) > MAX_JSON_DEPTH:
            raise ValidationError(INVALID_PAYLOAD, "issue.path must be a bounded array.")
        for part in path:
            if isinstance(part, str):
                _required_text_value(part, "path", 256)
            elif type(part) is not int or not 0 <= part <= 2**31 - 1:
                raise ValidationError(INVALID_PAYLOAD, "Invalid issue path segment.")
        _optional_required_text(obj, "message", 512)
        if "omitted_count" in obj:
            _require_int(obj, "omitted_count", minimum=1, maximum=2**31 - 1, scope="issue")
    return Payload(**raw)


def _read_exact(source: socket.socket | BinaryIO, count: int, deadline: float) -> bytes:
    buffer = bytearray(count)
    view = memoryview(buffer)
    offset = 0
    while offset < count:
        _check_deadline(deadline)
        try:
            if isinstance(source, socket.socket):
                source.settimeout(max(0.000_001, deadline - time.monotonic()))
                received = source.recv_into(view[offset:], count - offset)
            else:
                reader = getattr(source, "read", None)
                if reader is None:
                    raise TypeError("source must be a socket or readable binary stream")
                chunk = reader(count - offset)
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("binary stream read() must return bytes")
                received = len(chunk)
                if received:
                    view[offset : offset + received] = chunk
        except (socket.timeout, TimeoutError) as exc:
            raise ReadDeadlineExceeded(READ_DEADLINE_EXCEEDED, "Frame read deadline exceeded.") from exc
        _check_deadline(deadline)
        if received <= 0:
            raise FrameError(
                UNEXPECTED_END_OF_STREAM,
                f"Stream ended with {count - offset} bytes remaining.",
            )
        offset += received
    return bytes(buffer)


def _absolute_deadline(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("deadline must be an absolute monotonic timestamp")
    deadline = float(value)
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return deadline


def _check_deadline(deadline: float, *, writing: bool = False) -> None:
    if time.monotonic() >= deadline:
        code = WRITE_DEADLINE_EXCEEDED if writing else READ_DEADLINE_EXCEEDED
        operation = "write" if writing else "read"
        error_type = WriteDeadlineExceeded if writing else ReadDeadlineExceeded
        raise error_type(code, f"Frame {operation} deadline exceeded.")


def _validate_frame_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MOD_TO_PLUGIN_FRAME_BYTES:
        raise ValueError(f"max_frame_bytes must be between 1 and {MAX_MOD_TO_PLUGIN_FRAME_BYTES}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DecodeError(INVALID_JSON, "JSON object contains duplicate fields.")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise DecodeError(INVALID_JSON, "JSON contains a non-finite number.")


def _validate_json_limits(value: Any) -> None:
    if not isinstance(value, dict):
        raise DecodeError(INVALID_JSON, "JSON root must be an object.")
    pending = [(value, 1)]
    count = 0
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise DecodeError(INVALID_JSON, "JSON nesting is too deep.")
        count += 1
        if isinstance(current, dict):
            count += len(current)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise DecodeError(INVALID_JSON, "JSON numbers must be finite.")
        elif current is not None and not isinstance(current, (str, int, float, bool)):
            raise DecodeError(INVALID_JSON, "Unsupported JSON value.")
        if isinstance(current, dict) and any(not isinstance(key, str) for key in current):
            raise DecodeError(INVALID_JSON, "JSON keys must be strings.")
        if count > MAX_JSON_ITEMS:
            raise DecodeError(INVALID_JSON, "JSON object graph contains too many items.")


def _coerce_direction(direction: Direction | str) -> Direction:
    try:
        return direction if isinstance(direction, Direction) else Direction(direction)
    except (TypeError, ValueError) as exc:
        raise ValueError("direction must be mod_to_plugin or plugin_to_mod") from exc


def _require_object(value: Any, scope: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        code = INVALID_ENVELOPE if scope == "Envelope root" else INVALID_PAYLOAD
        raise ValidationError(code, f"{scope} must be an object.")
    return value


def _require_exact_fields(raw: Mapping[str, Any], fields: frozenset[str], scope: str) -> None:
    actual = frozenset(raw)
    if actual != fields:
        raise ValidationError(
            INVALID_ENVELOPE if scope == "Envelope" else INVALID_PAYLOAD,
            f"{scope} fields do not match the protocol-v2 schema.",
        )


def _require_allowed_fields(raw: Mapping[str, Any], fields: frozenset[str] | set[str], scope: str) -> None:
    if not frozenset(raw).issubset(fields):
        raise ValidationError(INVALID_PAYLOAD, f"{scope} contains an unknown field.")


def _require_present(raw: Mapping[str, Any], fields: frozenset[str] | set[str], scope: str) -> None:
    if not fields.issubset(raw.keys()):
        raise ValidationError(INVALID_PAYLOAD, f"{scope} is missing a required field.")


def _require_string(
    raw: Mapping[str, Any],
    key: str,
    *,
    scope: str,
    minimum: int,
    maximum: int,
    allow_blank: bool = False,
) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValidationError(INVALID_FIELD, f"{scope} field '{key}' must be a string.")
    length = _text_length(value)
    if not minimum <= length <= maximum or (not allow_blank and not value.strip()):
        raise ValidationError(INVALID_FIELD, f"{scope} field '{key}' has an invalid length.")
    return value


def _required_text(raw: Mapping[str, Any], key: str, maximum: int) -> str:
    if key not in raw:
        raise ValidationError(INVALID_PAYLOAD, f"Payload field '{key}' is required.")
    return _required_text_value(raw[key], key, maximum)


def _required_text_value(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or not 1 <= _text_length(value) <= maximum:
        raise ValidationError(INVALID_PAYLOAD, f"{field} must contain 1-{maximum} characters.")
    return value


def _optional_required_text(raw: Mapping[str, Any], key: str, maximum: int) -> str | None:
    # DataContractJsonSerializer treats an explicit JSON null like an omitted
    # optional reference member.  Emitters still canonicalize it by omission.
    if key not in raw or raw[key] is None:
        return None
    return _required_text_value(raw[key], key, maximum)


def _require_int(raw: Mapping[str, Any], key: str, *, minimum: int, maximum: int, scope: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(INVALID_FIELD, f"{scope} field '{key}' must be an integer in range.")
    return value


def _require_uuid(
    value: str,
    field: str,
    *,
    allow_empty: bool,
    error_type: type[ProtocolError] = ValidationError,
) -> None:
    if allow_empty and value == "":
        return
    if not isinstance(value, str) or len(value) != 32 or not _is_canonical_hex(value, 32):
        raise error_type(INVALID_FIELD, f"{field} must be a lowercase N-format UUID.")
    # Every 32-digit hexadecimal value is accepted by Guid.TryParseExact("N").


def _require_hex(
    value: str,
    field: str,
    *,
    error_type: type[ProtocolError] = ValidationError,
) -> None:
    if not _is_canonical_hex(value, 64):
        raise error_type(INVALID_PAYLOAD, f"{field} must be 64 lowercase hexadecimal characters.")


def _is_canonical_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all("0" <= character <= "9" or "a" <= character <= "f" for character in value)
    )


def _text_length(value: str) -> int:
    try:
        return len(value.encode("utf-16-le", "strict")) // 2
    except UnicodeEncodeError:
        raise ValidationError(INVALID_PAYLOAD, "Text contains an invalid Unicode scalar.") from None


def _encode_utf8(value: str, message: str, error_type: type[ProtocolError]) -> bytes:
    try:
        return value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise error_type(INVALID_PAYLOAD, message) from None


def _dto_dict(value: Any) -> dict[str, Any]:
    if not is_dataclass(value):
        raise TypeError("value must be a protocol dataclass")
    result: dict[str, Any] = {}
    for field in fields(value):
        item = getattr(value, field.name)
        if item is None:
            continue
        if is_dataclass(item):
            item = _dto_dict(item)
        elif isinstance(item, tuple):
            item = [_dto_dict(entry) if is_dataclass(entry) else entry for entry in item]
        result[field.name] = item
    return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))


_NO_TIMEOUT: Final = object()
