from __future__ import annotations

import json
import threading

from plugin.plugins.neko_rimworld.state import (
    BridgeStateStore,
    BridgeStatus,
    DiagnosticRing,
    json_safe_copy,
)


def test_diagnostic_ring_is_bounded_and_json_safe() -> None:
    ring = DiagnosticRing(max_events=3)
    for index in range(5):
        ring.record("info", "test_event", f"event {index}", context={"index": index})

    snapshot = ring.snapshot()
    assert [item["sequence"] for item in snapshot] == [3, 4, 5]
    assert json.loads(json.dumps(snapshot, allow_nan=False)) == snapshot


def test_diagnostics_redact_sensitive_fields_and_assignments() -> None:
    safe = json_safe_copy(
        {
            "pairing_token": "must-not-leak",
            "nested": {
                "proof": "must-not-leak",
                "message": "token=must-not-leak connection refused",
                "service_api_key": "must-not-leak",
                "header": "Authorization: Bearer must-not-leak",
                "spaced": "pairing_token = must-not-leak with spaces",
            },
        }
    )

    encoded = json.dumps(safe)
    assert "must-not-leak" not in encoded
    assert "<redacted>" in encoded


def test_state_snapshot_is_atomic_under_concurrent_updates() -> None:
    store = BridgeStateStore(DiagnosticRing(max_events=20))
    failures: list[BaseException] = []

    def update(worker: int) -> None:
        try:
            for index in range(100):
                store.transition(
                    BridgeStatus.LISTENING,
                    endpoint=f"127.0.0.1:{48919 + worker}",
                    worker={"id": worker, "iteration": index},
                )
                json.dumps(store.snapshot(), allow_nan=False)
        except BaseException as exc:  # pragma: no cover - assertion carrier
            failures.append(exc)

    threads = [threading.Thread(target=update, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    snapshot = store.snapshot()
    assert snapshot["status"] == BridgeStatus.LISTENING
    assert len(snapshot["diagnostics"]) <= 20
