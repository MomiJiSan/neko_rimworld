"""Single-worker loopback server for the RimWorld bridge protocol."""

from __future__ import annotations

import queue
import select
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .protocol import (
    AUTHENTICATE,
    HEARTBEAT,
    HELLO,
    MAX_HANDSHAKE_FRAME_BYTES,
    MAX_MOD_TO_PLUGIN_FRAME_BYTES,
    PROTOCOL_ERROR,
    QUERY_RESULT,
    READ_DEADLINE_EXCEEDED,
    SESSION_OPEN,
    SESSION_STALE,
    UNEXPECTED_END_OF_STREAM,
    WRITE_DEADLINE_EXCEEDED,
    Direction,
    Envelope,
    ProtocolError,
    SessionDescriptor,
    compute_client_proof,
    compute_server_proof,
    generate_nonce,
    make_challenge,
    make_heartbeat,
    make_protocol_error,
    make_query_request,
    make_welcome,
    read_frame,
    validate_auth_phase,
    validate_pairing_token,
    verify_proof,
    write_frame,
)
from .state import BridgeStateStore, BridgeStatus, mask_identifier, redact_text


class BridgeError(RuntimeError):
    """Base class for stable, credential-free bridge failures."""

    code = "bridge_error"

    def __init__(self, message: str, *, incident_id: str | None = None) -> None:
        self.incident_id = incident_id
        suffix = f" (incident_id={incident_id})" if incident_id else ""
        super().__init__(f"{message}{suffix}")


class BridgeStartError(BridgeError):
    code = "bridge_start_error"


class BridgeNotConnected(BridgeError):
    code = "bridge_not_connected"


class BridgeQueryTimeout(BridgeError):
    code = "bridge_query_timeout"


class BridgeProtocolError(BridgeError):
    code = "bridge_protocol_error"

    def __init__(
        self, message: str, *, protocol_code: str = "invalid_envelope", incident_id: str | None = None
    ) -> None:
        self.protocol_code = protocol_code
        super().__init__(message, incident_id=incident_id)


class BridgeAuthenticationError(BridgeError):
    code = "bridge_authentication_error"


class BridgeBusyError(BridgeError):
    code = "bridge_busy"


class BridgeUnsupportedError(BridgeError):
    code = "bridge_unsupported"


@dataclass(slots=True)
class _PendingQuery:
    connection_epoch: str
    session: SessionDescriptor
    event: threading.Event
    started_at: float
    response: Envelope | None = None
    error: BridgeError | None = None


@dataclass(frozen=True, slots=True)
class _QueryRequest:
    connection_epoch: str
    session: SessionDescriptor
    request_id: str
    operation: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Handshake:
    connection_epoch: str
    last_inbound_sequence: int
    next_outbound_sequence: int
    mod_version: str
    game_version: str
    capabilities: tuple[str, ...]


class BridgeServer:
    """Own one listener, one daemon I/O worker, and at most one game client."""

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 48919
    AUTH_TIMEOUT_SECONDS = 10.0
    FRAME_TIMEOUT_SECONDS = 2.0
    HEARTBEAT_INTERVAL_SECONDS = 2.0
    PEER_LIVENESS_SECONDS = 10.0
    POLL_SECONDS = 0.1
    QUERY_CAPABILITY = "query.execute.v2"
    AUTH_FAILURE_LOG_WINDOW_SECONDS = 10.0

    def __init__(
        self,
        token: str,
        state_store: BridgeStateStore | None = None,
        logger: Any = None,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        report_status: Callable[[dict[str, Any]], None] | None = None,
        outbound_capacity: int = 128,
        pending_capacity: int = 128,
    ) -> None:
        if host != self.DEFAULT_HOST:
            raise ValueError("BridgeServer only binds IPv4 loopback 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
            raise ValueError("port must be between 0 and 65535")
        if outbound_capacity < 1 or pending_capacity < 1:
            raise ValueError("queue capacities must be positive")
        try:
            validate_pairing_token(token)
        except ProtocolError:
            raise BridgeStartError("The pairing token must contain 32-128 characters.") from None

        self._token = token
        self._host = host
        self._port = port
        self._bound_port: int | None = None
        self._logger = logger
        self._state = state_store or BridgeStateStore()
        self._report_status = report_status
        self._pending_capacity = pending_capacity

        self._lifecycle_lock = threading.RLock()
        self._connection_lock = threading.RLock()
        self._pending_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._listener: socket.socket | None = None
        self._client: socket.socket | None = None
        self._worker: threading.Thread | None = None
        self._ready_epoch: str | None = None
        self._session: SessionDescriptor | None = None
        self._session_stale = False
        self._capabilities: frozenset[str] = frozenset()
        self._outbound: queue.Queue[_QueryRequest] = queue.Queue(maxsize=outbound_capacity)
        self._pending: dict[str, _PendingQuery] = {}
        # A caller deadline does not cancel work already running in the game.
        # Retain bounded correlation identities, never results or live waiters.
        self._expired_requests: dict[str, tuple[str, SessionDescriptor]] = {}
        self._report_lock = threading.Lock()
        self._last_report: dict[str, Any] | None = None
        self._auth_failure_lock = threading.Lock()
        self._auth_failure_last_emit = 0.0
        self._auth_failure_suppressed = 0
        self._state.update(port=port, worker_alive=False)

    def __repr__(self) -> str:
        return f"BridgeServer(host={self._host!r}, port={self._port}, running={self.running})"

    @property
    def connection_key(self) -> str | None:
        """Identity for connection-scoped schema caches; never exposes credentials."""
        with self._connection_lock:
            return self._ready_epoch

    @property
    def state_store(self) -> BridgeStateStore:
        return self._state

    @property
    def session(self) -> SessionDescriptor | None:
        """Exact authenticated game identity, independent of the masked UI view."""
        with self._connection_lock:
            return self._session if self._client is not None and not self._session_stale else None

    @property
    def running(self) -> bool:
        with self._lifecycle_lock:
            return self._worker is not None and self._worker.is_alive()

    @property
    def address(self) -> tuple[str, int]:
        with self._lifecycle_lock:
            return self._host, self._bound_port if self._bound_port is not None else self._port

    def start(self) -> tuple[str, int]:
        """Bind synchronously, then start the sole daemon worker."""

        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return self.address
            self._cleanup_dead_worker_locked()
            self._transition(BridgeStatus.STARTING, connection=None, session=None, port=self._port, worker_alive=False)
            try:
                validate_pairing_token(self._token)
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if sys.platform != "win32":
                    # An authenticated Mod connection may leave the server-side
                    # endpoint in FIN-WAIT/TIME-WAIT after a settings restart.
                    # Reuse permits an immediate same-port listener replacement;
                    # it does not permit a second live listener without
                    # SO_REUSEPORT.
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self._host, self._port))
                listener.listen(1)
                listener.settimeout(0.25)
                self._bound_port = int(listener.getsockname()[1])
            except (OSError, ProtocolError, ValueError) as exception:
                try:
                    listener.close()  # type: ignore[possibly-undefined]
                except (OSError, UnboundLocalError):
                    pass
                incident = self._record_issue(
                    "start", self._safe_exception(exception), level="error", error_code=BridgeStartError.code
                )
                self._transition(BridgeStatus.ERROR, endpoint=None, connection=None, session=None)
                raise BridgeStartError("The loopback bridge could not start.", incident_id=incident) from None

            self._listener = listener
            self._stop_event.clear()
            worker = threading.Thread(
                target=self._worker_main, args=(listener,), name="NekoRimWorld.Bridge", daemon=True
            )
            self._worker = worker
            endpoint = f"{self._host}:{self._bound_port}"
            self._transition(
                BridgeStatus.LISTENING,
                endpoint=endpoint,
                port=self._bound_port,
                worker_alive=False,
                connection=None,
                session=None,
            )
            try:
                worker.start()
            except Exception as exception:
                self._worker = None
                self._listener = None
                listener.close()
                incident = self._record_issue(
                    "start", self._safe_exception(exception), level="error", error_code=BridgeStartError.code
                )
                self._log_exception("worker_start", incident)
                self._transition(BridgeStatus.ERROR, endpoint=None)
                raise BridgeStartError("The bridge worker could not start.", incident_id=incident) from None
            self._state.set_worker_alive(True)
            self._report()
            self._event("info", "lifecycle", "Bridge listener started.")
            self._log("info", f"RimWorld bridge listening on {endpoint}")
            return self.address

    def stop(self, timeout: float = 3.0) -> bool:
        """Stop idempotently without waiting longer than three seconds."""

        wait_seconds = min(3.0, max(0.0, float(timeout)))
        with self._lifecycle_lock:
            worker = self._worker
            listener = self._listener
            with self._connection_lock:
                client = self._client
            if worker is None and listener is None and client is None:
                self._transition(BridgeStatus.STOPPED, endpoint=None, worker_alive=False, connection=None, session=None)
                return True
            self._transition(BridgeStatus.STOPPING)
            self._stop_event.set()
            self._close_socket(listener)
            self._close_socket(client)

        if worker is not None and worker is not threading.current_thread():
            worker.join(wait_seconds)
        stopped = worker is None or not worker.is_alive()
        self._fail_pending(BridgeNotConnected("The bridge has stopped."))
        self._drain_outbound()
        if stopped:
            with self._lifecycle_lock:
                self._cleanup_dead_worker_locked()
            self._transition(BridgeStatus.STOPPED, endpoint=None, worker_alive=False, connection=None, session=None)
            self._event("info", "lifecycle", "Bridge listener stopped.")
            self._log("info", "RimWorld bridge stopped")
        else:
            incident = self._record_issue(
                "stop",
                "Bridge worker did not stop within three seconds.",
                level="error",
                error_code="stop_timeout",
            )
            self._transition(BridgeStatus.ERROR, last_incident={"incident_id": incident})
        return stopped

    def request(self, operation: str, arguments: dict[str, Any], timeout: float = 3.0) -> Envelope:
        """Request one correlated game query from the active game session."""

        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        with self._connection_lock:
            epoch = self._ready_epoch
            session = self._session
            stale = self._session_stale
            capabilities = self._capabilities
        if epoch is None or session is None or stale:
            self._state.set_last_query("not_connected", 0.0, error_code=BridgeNotConnected.code)
            raise BridgeNotConnected("No authenticated, active RimWorld session is available.")
        if self.QUERY_CAPABILITY not in capabilities:
            self._state.set_last_query("unsupported", 0.0, error_code=BridgeUnsupportedError.code)
            raise BridgeUnsupportedError("The connected RimWorld Mod does not advertise generic query support.")

        request_id = uuid.uuid4().hex
        validated = make_query_request(epoch, 0, session, request_id, operation, arguments)
        arguments = validated.payload.to_dict()["arguments"]
        pending = _PendingQuery(epoch, session, threading.Event(), time.monotonic())
        with self._pending_lock:
            if len(self._pending) >= self._pending_capacity:
                self._state.set_last_query("busy", 0.0, error_code=BridgeBusyError.code)
                raise BridgeBusyError("The bridge already has too many pending requests.")
            self._pending[request_id] = pending
        try:
            self._outbound.put_nowait(_QueryRequest(epoch, session, request_id, operation, arguments))
        except queue.Full:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self._update_metrics()
            self._state.set_last_query("busy", 0.0, error_code=BridgeBusyError.code)
            raise BridgeBusyError("The bridge outbound queue is full.") from None
        self._update_metrics()

        if not pending.event.wait(float(timeout)):
            with self._pending_lock:
                timed_out = self._pending.pop(request_id, None) is pending
                if timed_out:
                    self._expired_requests[request_id] = (pending.connection_epoch, pending.session)
                    if len(self._expired_requests) > self._pending_capacity:
                        self._expired_requests.pop(next(iter(self._expired_requests)))
            self._update_metrics()
            if timed_out:
                incident = self._record_issue(
                    "query_timeout",
                    "A game query request timed out.",
                    level="warning",
                    error_code=BridgeQueryTimeout.code,
                    context={"request_id": mask_identifier(request_id)},
                )
                self._state.set_last_query(
                    "timeout",
                    (time.monotonic() - pending.started_at) * 1000.0,
                    error_code=BridgeQueryTimeout.code,
                )
                raise BridgeQueryTimeout("The RimWorld query request timed out.", incident_id=incident)
            pending.event.wait(0.05)

        if pending.error is not None:
            self._state.set_last_query(
                "error",
                (time.monotonic() - pending.started_at) * 1000.0,
                error_code=pending.error.code,
            )
            raise pending.error
        if pending.response is None:
            self._state.set_last_query(
                "error",
                (time.monotonic() - pending.started_at) * 1000.0,
                error_code=BridgeProtocolError.code,
            )
            raise BridgeProtocolError("The query completed without a response.")
        self._state.set_last_query(
            pending.response.payload.status or "unknown",
            (time.monotonic() - pending.started_at) * 1000.0,
            error_code=pending.response.payload.error_code,
        )
        return pending.response

    def __enter__(self) -> BridgeServer:
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.stop()

    def _worker_main(self, listener: socket.socket) -> None:
        fatal = False
        try:
            while not self._stop_event.is_set():
                try:
                    client, peer = listener.accept()
                except socket.timeout:
                    continue
                except OSError as exception:
                    if self._stop_event.is_set():
                        break
                    fatal = True
                    self._record_issue(
                        "listener", self._safe_exception(exception), level="error", error_code="listener_failed"
                    )
                    break

                if peer[0] != self.DEFAULT_HOST:
                    self._close_socket(client)
                    self._record_issue(
                        "peer_rejected",
                        "A non-loopback peer was rejected.",
                        level="warning",
                        error_code="peer_rejected",
                    )
                    continue
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self._connection_lock:
                    self._client = client
                self._transition(BridgeStatus.AUTHENTICATING, connection=None, session=None)
                try:
                    handshake = self._authenticate(client)
                    self._serve_authenticated(client, handshake)
                except Exception as exception:
                    if not self._stop_event.is_set():
                        self._handle_client_error(exception)
                finally:
                    self._close_socket(client)
                    self._clear_client(client)
                    self._fail_pending(BridgeNotConnected("The RimWorld connection closed."))
                    self._drain_outbound()
                    if not self._stop_event.is_set():
                        self._transition(BridgeStatus.LISTENING, connection=None, session=None)
        except Exception as exception:
            fatal = True
            if not self._stop_event.is_set():
                incident = self._record_issue(
                    "worker", self._safe_exception(exception), level="error", error_code="worker_internal"
                )
                self._log_exception("worker", incident)
        finally:
            self._close_socket(listener)
            with self._lifecycle_lock:
                if self._listener is listener:
                    self._listener = None
                if self._worker is threading.current_thread():
                    self._worker = None
            self._state.set_worker_alive(False)
            if self._stop_event.is_set():
                self._transition(BridgeStatus.STOPPED, endpoint=None, worker_alive=False, connection=None, session=None)
            elif fatal:
                self._transition(BridgeStatus.ERROR, endpoint=None, worker_alive=False, connection=None, session=None)

    def _authenticate(self, client: socket.socket) -> _Handshake:
        deadline = time.monotonic() + self.AUTH_TIMEOUT_SECONDS
        try:
            hello = self._read(client, deadline, authenticated=False, max_frame_bytes=MAX_HANDSHAKE_FRAME_BYTES)
            if hello.type != HELLO:
                raise BridgeAuthenticationError("Expected the RimWorld hello message.")
            epoch = hello.connection_epoch
            client_nonce = hello.payload.client_nonce
            if client_nonce is None:
                raise BridgeAuthenticationError("The RimWorld hello was incomplete.")

            server_nonce = generate_nonce()
            server_proof = compute_server_proof(self._token, epoch, client_nonce, server_nonce)
            self._write(client, make_challenge(hello, 0, server_nonce, server_proof), deadline=deadline)

            authentication = self._read(
                client, deadline, authenticated=False, max_frame_bytes=MAX_HANDSHAKE_FRAME_BYTES
            )
            self._check_inbound(authentication, epoch, hello.seq)
            if authentication.type != AUTHENTICATE:
                raise BridgeAuthenticationError("Expected the RimWorld authenticate message.")
            payload = authentication.payload
            if payload.client_nonce != client_nonce or payload.server_nonce != server_nonce:
                raise BridgeAuthenticationError("The authentication transcript did not match.")
            expected = compute_client_proof(self._token, epoch, client_nonce, server_nonce)
            if not verify_proof(expected, payload.proof):
                raise BridgeAuthenticationError("RimWorld client authentication failed.")
            self._write(client, make_welcome(epoch, 1), deadline=deadline)
        except BridgeAuthenticationError:
            raise
        except BridgeProtocolError as exception:
            self._state.record_protocol(invalid_frames=1)
            raise BridgeAuthenticationError(str(exception)) from None
        except ProtocolError as exception:
            self._state.record_protocol(invalid_frames=1)
            raise BridgeAuthenticationError(f"Mutual authentication failed ({exception.error_code}).") from None
        except (OSError, TimeoutError, EOFError) as exception:
            raise BridgeAuthenticationError(
                f"Mutual authentication transport failed ({type(exception).__name__})."
            ) from None

        return _Handshake(
            connection_epoch=epoch,
            last_inbound_sequence=authentication.seq,
            next_outbound_sequence=2,
            mod_version=hello.payload.mod_version or "unknown",
            game_version=hello.payload.game_version or "unknown",
            capabilities=hello.payload.capabilities or (),
        )

    def _serve_authenticated(self, client: socket.socket, handshake: _Handshake) -> None:
        epoch = handshake.connection_epoch
        inbound_sequence = handshake.last_inbound_sequence
        outbound_sequence = handshake.next_outbound_sequence
        last_inbound = time.monotonic()
        next_heartbeat = last_inbound + self.HEARTBEAT_INTERVAL_SECONDS
        with self._connection_lock:
            self._ready_epoch = epoch
            self._session = None
            self._session_stale = False
            self._capabilities = frozenset(handshake.capabilities)
        self._state.set_connection(
            connection_epoch=epoch,
            mod_version=handshake.mod_version,
            game_version=handshake.game_version,
            capabilities=list(handshake.capabilities),
        )
        self._transition(BridgeStatus.READY, session=None)
        self._event("info", "authentication", "RimWorld mutual authentication completed.")
        self._log("info", "RimWorld bridge authenticated")

        while not self._stop_event.is_set():
            outbound_sequence = self._flush_outbound(client, epoch, outbound_sequence)
            now = time.monotonic()
            if now >= next_heartbeat:
                self._write(client, make_heartbeat(epoch, outbound_sequence))
                outbound_sequence += 1
                next_heartbeat = now + self.HEARTBEAT_INTERVAL_SECONDS
            if now - last_inbound >= self.PEER_LIVENESS_SECONDS:
                raise BridgeNotConnected("The RimWorld heartbeat timed out.")

            wait = min(self.POLL_SECONDS, max(0.0, next_heartbeat - now))
            readable, _, _ = select.select([client], [], [], wait)
            if not readable:
                continue
            try:
                envelope = self._read(
                    client,
                    min(time.monotonic() + self.FRAME_TIMEOUT_SECONDS, last_inbound + self.PEER_LIVENESS_SECONDS),
                    authenticated=True,
                )
                inbound_sequence = self._check_inbound(envelope, epoch, inbound_sequence)
                self._dispatch(envelope)
                last_inbound = time.monotonic()
            except ProtocolError as exception:
                if exception.error_code in {UNEXPECTED_END_OF_STREAM, READ_DEADLINE_EXCEEDED}:
                    raise BridgeNotConnected("The RimWorld transport closed or stopped responding.") from None
                self._send_protocol_error(client, epoch, outbound_sequence, exception.error_code)
                raise BridgeProtocolError(
                    "RimWorld sent an invalid protocol message.", protocol_code=exception.error_code
                ) from None

    def _flush_outbound(self, client: socket.socket, epoch: str, sequence: int) -> int:
        for _ in range(16):
            try:
                item = self._outbound.get_nowait()
            except queue.Empty:
                break
            with self._pending_lock:
                pending = self._pending.get(item.request_id)
            with self._connection_lock:
                current = (
                    self._ready_epoch == item.connection_epoch == epoch
                    and self._same_session(self._session, item.session)
                    and not self._session_stale
                )
            if pending is None:
                continue
            if not current:
                self._complete_pending(item.request_id, error=BridgeNotConnected("The game session changed."))
                continue
            envelope = make_query_request(
                epoch, sequence, item.session, item.request_id, item.operation, item.arguments
            )
            self._write(client, envelope)
            sequence += 1
        self._update_metrics()
        return sequence

    def _dispatch(self, envelope: Envelope) -> None:
        validate_auth_phase(envelope, authenticated=True, direction=Direction.MOD_TO_PLUGIN)
        if envelope.type == HEARTBEAT:
            self._handle_heartbeat(envelope)
        elif envelope.type == SESSION_OPEN:
            self._handle_session_open(envelope)
        elif envelope.type == SESSION_STALE:
            self._handle_session_stale(envelope)
        elif envelope.type == QUERY_RESULT:
            self._handle_query_result(envelope)
        elif envelope.type == PROTOCOL_ERROR:
            code = envelope.payload.error_code or "unknown"
            self._record_issue(
                "remote_protocol_error",
                "RimWorld reported a protocol error.",
                level="warning",
                error_code=code,
                context={"error_code": code},
            )
        else:
            raise BridgeProtocolError("RimWorld sent an unsupported message type.")

    def _handle_session_open(self, envelope: Envelope) -> None:
        session = envelope.session
        if session is None:
            raise BridgeProtocolError("session_open did not contain a session.")
        with self._connection_lock:
            old_session = self._session
            if (
                self._same_session(old_session, session)
                and old_session is not None
                and self._waterline_regresses(session, old_session)
            ):
                raise BridgeProtocolError("session_open attempted to move the snapshot waterline backward.")
            self._session = session
            self._session_stale = False
        if old_session is not None and not self._same_session(old_session, session):
            self._fail_pending(BridgeNotConnected("The loaded RimWorld session changed."))
        self._state.set_session(**self._session_kwargs(session), stale=False)
        self._transition(BridgeStatus.READY)
        self._event("info", "session", "RimWorld game session opened.")

    def _handle_session_stale(self, envelope: Envelope) -> None:
        session = envelope.session
        if session is None:
            raise BridgeProtocolError("session_stale did not contain a session.")
        with self._connection_lock:
            if (
                self._same_session(self._session, session)
                and self._session is not None
                and self._waterline_regresses(session, self._session)
            ):
                raise BridgeProtocolError("session_stale attempted to move the snapshot waterline backward.")
            self._session = session
            self._session_stale = True
        self._fail_pending(BridgeNotConnected("The RimWorld game session became stale."))
        self._state.set_session(**self._session_kwargs(session), stale=True)
        self._transition(BridgeStatus.STALE)
        self._event("warning", "session", "RimWorld game session became stale.")

    def _handle_heartbeat(self, envelope: Envelope) -> None:
        session = envelope.session
        with self._connection_lock:
            current = self._session
            stale = self._session_stale
            if session is not None and current is not None:
                if session.colony_id != current.colony_id or session.load_epoch != current.load_epoch:
                    raise BridgeProtocolError("Heartbeat session identity did not match session_open.")
                if self._waterline_regresses(session, current):
                    raise BridgeProtocolError("Heartbeat attempted to move the snapshot waterline backward.")
                self._session = session
                current = session
        if session is not None and current is None:
            raise BridgeProtocolError("Heartbeat arrived before session_open.")
        if current is not None:
            self._state.set_session(**self._session_kwargs(current), stale=stale)

    def _handle_query_result(self, envelope: Envelope) -> None:
        with self._pending_lock:
            pending = self._pending.get(envelope.request_id)
            expired = self._expired_requests.pop(envelope.request_id, None) if pending is None else None
        if pending is None:
            if expired is not None:
                epoch, session = expired
                if (envelope.connection_epoch == epoch
                    and self._same_session(envelope.session, session)
                    and envelope.session is not None
                    and envelope.session.snapshot_sequence >= session.snapshot_sequence):
                    # Ignore exactly the expired response. Do not complete a
                    # newer request or move the active session's waterline.
                    return
            raise BridgeProtocolError(
                "RimWorld returned a query result without a matching pending request.",
                protocol_code="invalid_correlation",
            )
        response_session = envelope.session
        if (
            envelope.connection_epoch != pending.connection_epoch
            or not self._same_session(response_session, pending.session)
            or response_session is None
            or response_session.snapshot_sequence < pending.session.snapshot_sequence
        ):
            error = BridgeProtocolError(
                "Query result correlation did not match the request.",
                protocol_code="invalid_correlation",
            )
            self._complete_pending(envelope.request_id, error=error)
            raise error

        update_session = False
        with self._connection_lock:
            current = self._session
            if not self._same_session(current, response_session) or self._session_stale:
                error = BridgeProtocolError(
                    "Query result no longer matched the active game session.",
                    protocol_code="invalid_correlation",
                )
                self._complete_pending(envelope.request_id, error=error)
                raise error
            assert current is not None and response_session is not None
            if response_session.snapshot_sequence > current.snapshot_sequence or (
                response_session.snapshot_sequence == current.snapshot_sequence
                and response_session.game_tick >= current.game_tick
            ):
                self._session = response_session
                update_session = True
        self._complete_pending(envelope.request_id, response=envelope)
        if update_session:
            self._state.set_session(**self._session_kwargs(response_session), stale=False)

    def _complete_pending(
        self,
        request_id: str,
        *,
        response: Envelope | None = None,
        error: BridgeError | None = None,
    ) -> None:
        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                return
            pending.response = response
            pending.error = error
            pending.event.set()
        self._update_metrics()

    def _fail_pending(self, error: BridgeError) -> None:
        with self._pending_lock:
            pending = list(self._pending.items())
            self._pending.clear()
            for request_id,item in pending:
                self._expired_requests[request_id]=(item.connection_epoch,item.session)
                if len(self._expired_requests)>self._pending_capacity:
                    self._expired_requests.pop(next(iter(self._expired_requests)))
                item.error = type(error)(str(error))
                item.event.set()
        self._update_metrics()

    @staticmethod
    def _check_inbound(envelope: Envelope, epoch: str, previous_sequence: int) -> int:
        if envelope.connection_epoch != epoch:
            raise BridgeProtocolError("RimWorld connection epoch changed unexpectedly.")
        if envelope.seq <= previous_sequence:
            raise BridgeProtocolError("RimWorld message sequence did not increase.")
        return envelope.seq

    @staticmethod
    def _session_kwargs(session: SessionDescriptor) -> dict[str, Any]:
        return {
            "colony_id": session.colony_id,
            "load_epoch": session.load_epoch,
            "game_tick": session.game_tick,
            "snapshot_sequence": session.snapshot_sequence,
        }

    @staticmethod
    def _same_session(left: SessionDescriptor | None, right: SessionDescriptor | None) -> bool:
        return (
            left is not None
            and right is not None
            and left.colony_id == right.colony_id
            and left.load_epoch == right.load_epoch
        )

    @staticmethod
    def _waterline_regresses(candidate: SessionDescriptor, current: SessionDescriptor) -> bool:
        return candidate.snapshot_sequence < current.snapshot_sequence or (
            candidate.snapshot_sequence == current.snapshot_sequence and candidate.game_tick < current.game_tick
        )

    def _read(
        self,
        client: socket.socket,
        deadline: float,
        *,
        authenticated: bool,
        max_frame_bytes: int = MAX_MOD_TO_PLUGIN_FRAME_BYTES,
    ) -> Envelope:
        envelope = read_frame(
            client,
            deadline,
            max_frame_bytes=max_frame_bytes,
            direction=Direction.MOD_TO_PLUGIN,
            authenticated=authenticated,
        )
        self._state.record_protocol(rx_frames=1)
        return envelope

    def _write(self, client: socket.socket, envelope: Envelope, *, deadline: float | None = None) -> None:
        write_frame(
            client,
            envelope,
            deadline=deadline if deadline is not None else time.monotonic() + self.FRAME_TIMEOUT_SECONDS,
        )
        self._state.record_protocol(tx_frames=1)

    def _send_protocol_error(self, client: socket.socket, epoch: str, sequence: int, error_code: str) -> None:
        try:
            self._write(
                client,
                make_protocol_error(epoch, sequence, error_code, "The inbound message was rejected."),
            )
        except (OSError, ProtocolError):
            pass

    def _clear_client(self, client: socket.socket) -> None:
        with self._connection_lock:
            if self._client is client:
                self._client = None
            self._ready_epoch = None
            self._session = None
            self._session_stale = False
            self._capabilities = frozenset()
        with self._pending_lock:
            self._expired_requests.clear()

    def _drain_outbound(self) -> None:
        while True:
            try:
                self._outbound.get_nowait()
            except queue.Empty:
                break
        self._update_metrics()

    def _update_metrics(self) -> None:
        with self._pending_lock:
            pending = len(self._pending)
        self._state.set_metrics(pending_requests=pending, outbound_depth=self._outbound.qsize())

    def _cleanup_dead_worker_locked(self) -> None:
        if self._worker is not None and not self._worker.is_alive():
            self._worker = None
        if self._listener is not None and self._worker is None:
            self._close_socket(self._listener)
            self._listener = None
        if self._worker is None:
            self._bound_port = None

    @staticmethod
    def _close_socket(value: socket.socket | None) -> None:
        if value is None:
            return
        try:
            value.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            value.close()
        except OSError:
            pass

    def _transition(self, status: str, **fields: object) -> None:
        self._state.transition(status, **fields)
        self._report()

    def _report(self) -> None:
        if self._report_status is None:
            return
        current = self._state.coarse_status()
        with self._report_lock:
            if current == self._last_report:
                return
            self._last_report = dict(current)
        try:
            self._report_status(current)
        except Exception:
            self._log("warning", "RimWorld bridge status callback failed")

    def _event(
        self,
        level: str,
        category: str,
        message: str,
        *,
        context: dict[str, object] | None = None,
    ) -> str:
        return self._state.diagnostics.record(level, category, message, context=context)

    def _record_issue(
        self,
        category: str,
        message: str,
        *,
        level: str,
        error_code: str | None = None,
        context: dict[str, object] | None = None,
    ) -> str:
        incident = self._state.record_incident(category, message, level=level, error_code=error_code, context=context)
        self._log(level, f"RimWorld bridge {category} incident={incident}: {message}")
        self._report()
        return incident

    def _handle_client_error(self, exception: Exception) -> None:
        context: dict[str, object] = {"exception_type": type(exception).__name__}
        error_code = "client_error"
        if isinstance(exception, BridgeError):
            context["bridge_code"] = exception.code
            error_code = exception.code
        if isinstance(exception, BridgeAuthenticationError):
            self._record_auth_failure(context)
            return
        elif isinstance(exception, BridgeProtocolError):
            category, level = "protocol", "warning"
            context["protocol_code"] = exception.protocol_code
            error_code = exception.protocol_code
            self._state.record_protocol(invalid_frames=1)
        elif isinstance(exception, ProtocolError):
            context["protocol_code"] = exception.error_code
            error_code = exception.error_code
            if exception.error_code in {WRITE_DEADLINE_EXCEEDED, UNEXPECTED_END_OF_STREAM}:
                category, level = "connection", "warning"
            else:
                category, level = "protocol", "warning"
                self._state.record_protocol(invalid_frames=1)
        elif isinstance(exception, (BridgeNotConnected, ConnectionError, EOFError, TimeoutError, socket.timeout)):
            category, level = "connection", "warning"
            if not isinstance(exception, BridgeError):
                error_code = "connection_lost"
        elif isinstance(exception, OSError):
            category, level = "network", "warning"
            context["errno"] = exception.errno
            error_code = "network_error"
        else:
            category, level = "client_internal", "error"
            error_code = "client_internal"
        incident = self._record_issue(
            category,
            self._safe_exception(exception),
            level=level,
            error_code=error_code,
            context=context,
        )
        if category == "client_internal":
            self._log_exception("client", incident)

    def _record_auth_failure(self, context: dict[str, object]) -> None:
        self._state.record_protocol(auth_failures=1)
        now = time.monotonic()
        with self._auth_failure_lock:
            if now - self._auth_failure_last_emit < self.AUTH_FAILURE_LOG_WINDOW_SECONDS:
                self._auth_failure_suppressed += 1
                return
            attempts = self._auth_failure_suppressed + 1
            self._auth_failure_suppressed = 0
            self._auth_failure_last_emit = now
        safe_context = dict(context)
        safe_context["attempts_since_last_report"] = attempts
        self._record_issue(
            "authentication",
            "RimWorld mutual authentication failed.",
            level="warning",
            error_code=BridgeAuthenticationError.code,
            context=safe_context,
        )

    @staticmethod
    def _safe_exception(exception: BaseException) -> str:
        if isinstance(exception, ProtocolError):
            return exception.safe_summary
        if isinstance(exception, BridgeError):
            return redact_text(exception, 256)
        if isinstance(exception, OSError):
            return f"{type(exception).__name__}(errno={exception.errno})"
        return type(exception).__name__

    def _log(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        method = getattr(self._logger, level, None)
        if not callable(method):
            return
        try:
            method(redact_text(message, 512))
        except Exception:
            pass

    def _log_exception(self, scope: str, incident_id: str) -> None:
        """Log the active traceback with a fixed, credential-free message."""

        if self._logger is None:
            return
        method = getattr(self._logger, "exception", None)
        if not callable(method):
            return
        try:
            method(f"RimWorld bridge internal {scope} failure; incident_id={incident_id}")
        except Exception:
            pass
