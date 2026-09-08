from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest

from plugin.plugins.neko_rimworld.bridge import (
    BridgeQueryTimeout,
    BridgeServer,
)
from plugin.plugins.neko_rimworld.protocol import (
    AUTHENTICATE,
    CHALLENGE,
    HEARTBEAT,
    HELLO,
    OK,
    QUERY_REQUEST,
    QUERY_RESULT,
    SESSION_OPEN,
    WELCOME,
    Direction,
    Payload,
    SessionDescriptor,
    compute_client_proof,
    compute_server_proof,
    make_envelope,
    read_frame,
    verify_proof,
    write_frame,
)
from plugin.plugins.neko_rimworld.state import BridgeStateStore, DiagnosticRing

TOKEN = "0123456789abcdef0123456789abcdef"


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level: str, message: object, *args: object) -> None:
        text = str(message)
        for value in args:
            text = text.replace("{}", str(value), 1)
        self.records.append((level, text))

    def info(self, message: object, *args: object) -> None:
        self._record("info", message, *args)

    def warning(self, message: object, *args: object) -> None:
        self._record("warning", message, *args)

    def error(self, message: object, *args: object) -> None:
        self._record("error", message, *args)

    def exception(self, message: object, *args: object) -> None:
        self._record("exception", message, *args)


def test_start_stop_is_idempotent_and_releases_port() -> None:
    server = BridgeServer(TOKEN, port=0)
    host, port = server.start()
    assert host == "127.0.0.1"
    assert port > 0
    assert server.running
    assert server.start() == (host, port)
    assert server.stop(timeout=2)
    assert server.stop(timeout=2)
    assert not server.running

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    finally:
        probe.close()


def test_repeated_start_stop_leaves_no_bridge_worker() -> None:
    server = BridgeServer(TOKEN, port=0)
    for _ in range(10):
        server.start()
        assert server.running
        assert server.stop(timeout=2)
    assert not any(thread.is_alive() and thread.name == "NekoRimWorld.Bridge" for thread in threading.enumerate())


def test_authenticated_session_and_digest_query_round_trip() -> None:
    logger = RecordingLogger()
    statuses: list[dict[str, Any]] = []
    store = BridgeStateStore(DiagnosticRing(max_events=50))
    server = BridgeServer(TOKEN, store, logger, port=0, report_status=statuses.append)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port)
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("2" * 32, "3" * 32, 123, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: store.snapshot(include_diagnostics=False)["session"] is not None)

        result: dict[str, Any] = {}
        failure: list[BaseException] = []

        def request() -> None:
            try:
                result.update(server.request("colony_snapshot", {}, timeout=2).to_dict())
            except BaseException as exception:  # pragma: no cover - assertion carrier
                failure.append(exception)

        requester = threading.Thread(target=request)
        requester.start()
        query = read_until(client, QUERY_REQUEST)
        assert query.payload.operation == "colony_snapshot"
        assert query.payload.arguments == {}
        advanced_session = SessionDescriptor(session.colony_id, session.load_epoch, 200, 10)
        write_frame(
            client,
            make_envelope(HEARTBEAT, hello.connection_epoch, 3, session=advanced_session),
        )
        wait_until(lambda: store.snapshot(include_diagnostics=False)["session"]["snapshot_sequence"] == 10)
        response_session = SessionDescriptor(session.colony_id, session.load_epoch, 123, 1)
        data = {"new_domain": {"value": 42, "null": None}, "rows": list(range(100))}
        response = make_envelope(
            QUERY_RESULT,
            hello.connection_epoch,
            4,
            Payload(status=OK, data=data, issues=[]),
            session=response_session,
            request_id=query.request_id,
        )
        write_frame(client, response)
        requester.join(timeout=3)

        assert not requester.is_alive()
        assert not failure
        assert result["payload"]["status"] == OK
        assert result["snapshot_sequence"] == 1
        assert result["payload"]["data"] == data
        assert store.snapshot()["session"]["snapshot_sequence"] == 10
        assert set(statuses[-1]) == {
            "status",
            "connected",
            "authenticated",
            "session_open",
            "protocol_version",
            "port",
            "last_error_code",
            "worker_alive",
        }
        snapshot = store.snapshot()
        assert snapshot["last_query"]["status"] == OK
        assert snapshot["metrics"]["pending_requests"] == 0
    finally:
        client.close()
        assert server.stop(timeout=2)


def test_bad_client_proof_is_rejected_without_secret_leakage() -> None:
    logger = RecordingLogger()
    store = BridgeStateStore(DiagnosticRing(max_events=50))
    server = BridgeServer(TOKEN, store, logger, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port)
    try:
        wrong = compute_client_proof(
            "f" * 32, hello.connection_epoch, challenge.payload.client_nonce or "", challenge.payload.server_nonce or ""
        )
        authenticate = make_envelope(
            AUTHENTICATE,
            hello.connection_epoch,
            1,
            Payload(
                client_nonce=challenge.payload.client_nonce,
                server_nonce=challenge.payload.server_nonce,
                proof=wrong,
            ),
        )
        write_frame(client, authenticate)
        wait_until(lambda: store.snapshot(include_diagnostics=False)["protocol"]["auth_failures"] >= 1)

        serialized = repr(store.snapshot()) + repr(logger.records)
        assert TOKEN not in serialized
        assert wrong not in serialized
    finally:
        client.close()
        assert server.stop(timeout=2)


def test_pending_query_times_out_and_is_removed() -> None:
    store = BridgeStateStore(DiagnosticRing(max_events=50))
    server = BridgeServer(TOKEN, store, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port)
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("5" * 32, "6" * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: store.snapshot(include_diagnostics=False)["session"] is not None)

        with pytest.raises(BridgeQueryTimeout):
            server.request("colony_snapshot", {}, timeout=0.1)
        assert store.snapshot()["metrics"]["pending_requests"] == 0
    finally:
        client.close()
        assert server.stop(timeout=2)


def test_digest_capability_is_required_before_enqueue() -> None:
    from plugin.plugins.neko_rimworld.bridge import BridgeUnsupportedError

    store = BridgeStateStore(DiagnosticRing(max_events=50))
    server = BridgeServer(TOKEN, store, port=0)
    _, port = server.start()
    client, hello, challenge = connect_and_challenge(port, capabilities=("other.capability.v1",))
    try:
        finish_authentication(client, hello, challenge, TOKEN)
        session = SessionDescriptor("8" * 32, "9" * 32, 1, 0)
        write_frame(client, make_envelope(SESSION_OPEN, hello.connection_epoch, 2, session=session))
        wait_until(lambda: store.snapshot(include_diagnostics=False)["session"] is not None)

        with pytest.raises(BridgeUnsupportedError):
            server.request("colony_snapshot", {}, timeout=0.2)
        snapshot = store.snapshot()
        assert snapshot["metrics"]["outbound_depth"] == 0
        assert snapshot["last_query"]["status"] == "unsupported"
    finally:
        client.close()
        assert server.stop(timeout=2)


def connect_and_challenge(
    port: int,
    *,
    capabilities: tuple[str, ...] = ("query.execute.v2",),
):
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    epoch = "1" * 32
    client_nonce = "a" * 64
    hello = make_envelope(
        HELLO,
        epoch,
        0,
        Payload(
            mod_version="0.1.0-dev",
            game_version="1.6.4871 rev591",
            capabilities=capabilities,
            client_nonce=client_nonce,
        ),
    )
    write_frame(client, hello)
    challenge = read_frame(
        client,
        time.monotonic() + 2,
        direction=Direction.PLUGIN_TO_MOD,
        authenticated=False,
    )
    assert challenge.type == CHALLENGE
    return client, hello, challenge


def finish_authentication(client: socket.socket, hello, challenge, token: str) -> None:
    client_nonce = challenge.payload.client_nonce or ""
    server_nonce = challenge.payload.server_nonce or ""
    expected_server = compute_server_proof(token, hello.connection_epoch, client_nonce, server_nonce)
    assert verify_proof(expected_server, challenge.payload.proof)
    client_proof = compute_client_proof(token, hello.connection_epoch, client_nonce, server_nonce)
    write_frame(
        client,
        make_envelope(
            AUTHENTICATE,
            hello.connection_epoch,
            1,
            Payload(client_nonce=client_nonce, server_nonce=server_nonce, proof=client_proof),
        ),
    )
    welcome = read_frame(
        client,
        time.monotonic() + 2,
        direction=Direction.PLUGIN_TO_MOD,
        authenticated=False,
    )
    assert welcome.type == WELCOME


def read_until(client: socket.socket, message_type: str):
    deadline = time.monotonic() + 2
    while True:
        envelope = read_frame(
            client,
            deadline,
            direction=Direction.PLUGIN_TO_MOD,
            authenticated=True,
        )
        if envelope.type == message_type:
            return envelope


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")
