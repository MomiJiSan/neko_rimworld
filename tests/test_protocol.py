from __future__ import annotations

import io
import json
import struct
import time

import pytest

from plugin.plugins.neko_rimworld import protocol as wire

TOKEN = "0123456789abcdef0123456789abcdef"
EPOCH = "1" * 32
CLIENT_NONCE = "a" * 64
SERVER_NONCE = "b" * 64


def make_hello() -> wire.Envelope:
    return wire.make_envelope(
        wire.HELLO,
        EPOCH,
        0,
        wire.Payload(
            mod_version="0.1.0-dev",
            game_version="1.6.4871 rev591",
            capabilities=("query.execute.v2",),
            client_nonce=CLIENT_NONCE,
        ),
    )


def test_hmac_matches_csharp_cross_language_vector() -> None:
    assert wire.compute_server_proof(TOKEN, EPOCH, CLIENT_NONCE, SERVER_NONCE) == (
        "a674d3dcd9959a6a1127b7385a3c09d3fa788ab3b8821123f3b7c75f172a6d7f"
    )
    assert wire.compute_client_proof(TOKEN, EPOCH, CLIENT_NONCE, SERVER_NONCE) == (
        "919804556751bfda38f7d06832b32e288c8e96e218afe7d3f393fca9275ed172"
    )
    assert wire.verify_proof("0" * 64, "0" * 64)
    assert not wire.verify_proof("0" * 64, "1" * 64)


def test_hello_never_contains_pairing_token() -> None:
    encoded = wire.serialize_envelope(make_hello())
    assert TOKEN.encode() not in encoded
    decoded = wire.decode_envelope(
        encoded,
        direction=wire.Direction.MOD_TO_PLUGIN,
        authenticated=False,
    )
    assert decoded == make_hello()


def test_generic_query_result_preserves_unknown_fields_and_null():
    raw = {
        "v": 2,
        "type": "query_result",
        "connection_epoch": EPOCH,
        "colony_id": "2" * 32,
        "load_epoch": "3" * 32,
        "seq": 4,
        "request_id": "4" * 32,
        "game_tick": 1234,
        "snapshot_sequence": 7,
        "payload": {
            "status": "ok",
            "issues": [],
            "data": {"future_domain": {"nested": [True, None, 0.875, "2026-09-05"]}},
        },
    }
    decoded = wire.decode_envelope(json.dumps(raw).encode(), direction=wire.Direction.MOD_TO_PLUGIN, authenticated=True)
    assert decoded.to_dict() == raw
    assert decoded.payload.data["future_domain"]["nested"][1] is None


def test_unknown_fields_and_wrong_direction_are_rejected() -> None:
    raw = make_hello().to_dict()
    raw["payload"]["unexpected"] = True
    with pytest.raises(wire.ProtocolError) as unknown:
        wire.decode_envelope(json.dumps(raw).encode())
    assert unknown.value.error_code == wire.INVALID_PAYLOAD

    with pytest.raises(wire.ProtocolError) as wrong_direction:
        wire.validate_auth_phase(
            make_hello(),
            authenticated=False,
            direction=wire.Direction.PLUGIN_TO_MOD,
        )
    assert wrong_direction.value.error_code == wire.INVALID_ENVELOPE


def test_fragmented_frame_and_absolute_deadline() -> None:
    frame = wire.encode_frame(make_hello())
    decoded = wire.read_frame(
        OneByteStream(frame),
        time.monotonic() + 1,
        direction=wire.Direction.MOD_TO_PLUGIN,
        authenticated=False,
    )
    assert decoded == make_hello()

    with pytest.raises(wire.ProtocolError) as slow:
        wire.read_frame(
            SlowOneByteStream(frame, 0.02),
            time.monotonic() + 0.05,
            direction=wire.Direction.MOD_TO_PLUGIN,
            authenticated=False,
        )
    assert slow.value.error_code == wire.READ_DEADLINE_EXCEEDED

    oversized = struct.pack("!I", wire.MAX_HANDSHAKE_FRAME_BYTES + 1)
    with pytest.raises(wire.ProtocolError) as too_large:
        wire.read_frame(
            io.BytesIO(oversized),
            time.monotonic() + 1,
            direction=wire.Direction.MOD_TO_PLUGIN,
            authenticated=False,
        )
    assert too_large.value.error_code == wire.FRAME_TOO_LARGE

    with pytest.raises(wire.ProtocolError) as slow_write:
        wire.write_frame(
            SlowOneByteWriter(0.02),
            make_hello(),
            deadline=time.monotonic() + 0.05,
        )
    assert slow_write.value.error_code == wire.WRITE_DEADLINE_EXCEEDED


class OneByteStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        return super().read(1 if size < 0 else min(1, size))


class SlowOneByteStream(OneByteStream):
    def __init__(self, value: bytes, delay: float) -> None:
        super().__init__(value)
        self.delay = delay

    def read(self, size: int = -1) -> bytes:
        time.sleep(self.delay)
        return super().read(size)


class SlowOneByteWriter(io.BytesIO):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def write(self, value: bytes | bytearray | memoryview) -> int:
        time.sleep(self.delay)
        return super().write(bytes(value[:1]))
