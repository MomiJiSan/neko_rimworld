import copy
import json

import pytest

from plugin.plugins.neko_rimworld.agent.contracts import ToolCall
from plugin.plugins.neko_rimworld.agent.evidence_aggressive import (
    FORMAT,
    AggressiveCompiler,
    AggressiveDecoder,
    project,
)
from plugin.plugins.neko_rimworld.agent.evidence_compiler import EvidenceDecoder, canonical, make_compiler
from plugin.plugins.neko_rimworld.agent.evidence_view import review_evidence
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession
from plugin.plugins.neko_rimworld.agent.tools import ToolRegistry, ToolSpec


def data(tick=4, actor="A", gas=2):
    return {
        "ok": True,
        "tool": "inspect_route",
        "data": {
            "sample": {"load_epoch": "load", "started_tick": tick, "finished_tick": tick, "atomic": False},
            "context": {"actor_id": actor, "user_id": "patient"},
            "route": {
                "status": "found",
                "steps": 12,
                "start": {"x": 1, "z": 2},
                "end": {"x": 8, "z": 9},
                "hypothetical_start": True,
                "native_cost": {"value": 145, "unit": "native_path_cost"},
                "segments": [{"door": None, "terrain": "Soil", "first_index": i, "last_index": i} for i in range(80)],
            },
            "path_environment": {
                "scope": "observed_path_cells",
                "gases": [{"type": "ToxGas", "cells": gas}],
                "coverage": {"complete": False, "unknown": ["future combat"]},
                "path_environment_bands": [{"first_index": i, "last_index": i, "value": i} for i in range(80)],
            },
            "extension": {
                "unknown_mod_fields": {"$ref": "@123", "$value": ["#2", "*3", "~4", "!"], "empty": [], "nullable": None}
            },
            "rows": [
                {
                    "id": str(i),
                    "disabled": False,
                    "priority": i % 4,
                    "meaning": "Observed configuration, not a promise of successful action. " * 4,
                }
                for i in range(30)
            ],
        },
    }


def test_projection_is_explicit_and_preserves_business_facts_without_mutating_raw():
    raw = data()
    saved = copy.deepcopy(raw)
    view, omitted = project(raw)
    assert raw == saved
    assert view["data"]["sample"] == raw["data"]["sample"]
    assert view["data"]["context"] == raw["data"]["context"]
    assert view["data"]["path_environment"]["gases"] == raw["data"]["path_environment"]["gases"]
    assert view["data"]["path_environment"]["coverage"] == raw["data"]["path_environment"]["coverage"]
    assert "segments" not in view["data"]["route"]
    assert "segments" in view["data"]["route"]["not_shown"]
    assert view["data"]["extension"] == raw["data"]["extension"]
    assert len(omitted) == 2


def test_projected_stream_roundtrip_keeps_actor_changes_and_literal_markers():
    compiler = AggressiveCompiler()
    decoder = EvidenceDecoder()
    for raw in [data(), data(5, "B", 0), data(6, "C", 1)]:
        view = compiler.compile(raw)
        assert view.encoded
        assert decoder.decode(json.loads(view.content)) == project(raw)[0]
        assert not view.metrics()["raw_json_lossless"]
        assert view.metrics()["omissions"]


def test_shared_template_deltas_preserve_null_false_numbers_and_key_order():
    compiler = AggressiveCompiler()
    decoder = EvidenceDecoder()
    contents = []
    for number in range(4):
        raw = {
            "rows": [
                {
                    "id": str(i),
                    "a": False,
                    "b": 0,
                    "c": None,
                    "amount": number if i == 3 else i,
                    "text": "Some stable description for the current observation." * 4,
                }
                for i in range(25)
            ]
        }
        view = compiler.compile(raw)
        contents.append(view.content)
        assert canonical(decoder.decode(json.loads(view.content))) == canonical(raw)
    assert any('"~' in x for x in contents)


@pytest.mark.parametrize(
    "value",
    [
        ["@1", "#2", "*3", "~4", "!"],
        ("#2", ("@1", None, False, 0)),
        {"$value": False, "0": [None, 0, "0"]},
        {"not_shown": {"custom": True}, "id": "x", "position": {}, "runtime_type": "keep me"},
    ],
)
def test_unrecognized_extensions_and_marker_collisions_are_preserved(value):
    raw = {"custom": [copy.deepcopy(value) for _ in range(80)]}
    view = AggressiveCompiler().compile(raw)
    assert EvidenceDecoder().decode(json.loads(view.content)) == project(json.loads(canonical(raw)))[0]
    if isinstance(value, dict) and "not_shown" in value:
        assert project(value) == (value, [])


def test_failed_compile_does_not_publish_schemas_or_definitions():
    compiler = AggressiveCompiler()
    with pytest.raises(ValueError, match="view_limit"):
        compiler.compile(data(), max_view_chars=10)
    view = compiler.compile(data())
    assert EvidenceDecoder().decode(json.loads(view.content)) == project(data())[0]


@pytest.mark.parametrize("value", ["@1", ["#1", 4], ["~1", {"missing": [3]}]])
def test_missing_reference_schema_and_patch_target_fail_closed(value):
    decoder = AggressiveDecoder()
    packet = {"_neko_evidence_format": FORMAT, "schemas": {}, "shared": {}, "value": value}
    with pytest.raises(ValueError):
        decoder.decode(packet)
    assert not decoder.definitions and not decoder.schemas


def test_cycles_and_definition_replacement_are_rejected():
    decoder = AggressiveDecoder()
    with pytest.raises(ValueError):
        decoder.decode({"schemas": {}, "shared": {"1": "@1"}, "value": "@1"})
    decoder.decode({"schemas": {}, "shared": {"1": 5}, "value": "@1"})
    with pytest.raises(ValueError, match="replaced"):
        decoder.decode({"schemas": {}, "shared": {"1": 6}, "value": "@1"})


@pytest.mark.asyncio
async def test_aggressive_tool_delivery_and_review_do_not_get_hidden_detail(monkeypatch):
    monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", "2")
    session = AgentRunSession.create(ledger=None, system_prompt="s", question="q")
    registry = ToolRegistry(evidence_compiler=session.evidence_compiler)
    calls = []

    async def handler(args):
        calls.append(args)
        return data()["data"]

    registry.register(ToolSpec("inspect_route", "d", {"type": "object", "properties": {}}, handler))
    call = ToolCall("c", "inspect_route", "{}")
    result = await registry.execute(registry.prepare(call))
    assert result.ok and len(calls) == 1 and result.value == data()
    message = session.tool_message(result)
    readings = review_evidence([{"role": "assistant", "tool_calls": [call.as_message_value()]}, message])
    assert readings[0]["result"] == project(data())[0]
    assert "segments" not in readings[0]["result"]["data"]["route"]
    assert session.evidence_views[0]["projection"] == "display_details"
    assert isinstance(make_compiler(), AggressiveCompiler)


def test_identity_only_reads_keep_full_type_and_holder_information():
    identity = {
        "id": "item",
        "position": {"x": 1, "z": 2},
        "runtime_type": "Custom.Item",
        "holder_type": "Pawn_InventoryTracker",
        "holder_chain": {"rows": [{"runtime_type": "Verse.Map", "thing_id": None}], "complete": True},
    }
    raw = {
        "ok": True,
        "tool": "look",
        "data": {"facts": [{"object": identity, "facts": {"identity": {"object": identity}}}]},
    }
    assert project(raw) == (raw, [])


@pytest.mark.asyncio
async def test_real_runtime_aggressive_mode_adds_no_calls(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import (
        AgentAuditRegistry,
        AgentRuntime,
        AssistantTurn,
        build_agent_model_settings,
    )

    class Logger:
        def info(self, *args):
            pass

        warning = error = info

    counts = []
    for mode in ("0", "2"):
        monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", mode)
        calls = []
        requests = []
        raw = data()

        async def handler(args):
            calls.append(args)
            return raw["data"]

        class Model:
            async def complete(self, messages, tools, *, timeout):
                requests.append(copy.deepcopy(messages))
                call = (
                    ToolCall("read", "inspect_route", "{}")
                    if len(requests) == 1
                    else ToolCall(
                        "done",
                        "submit_response",
                        json.dumps({"answer": "Read completed.", "detail": "Read completed."}),
                    )
                )
                return AssistantTurn("r", "script", "tool_calls", "", (call,), reasoning_content="private continuation")

        runtime = AgentRuntime(
            observation_tools=[ToolSpec("inspect_route", "read", {"type": "object", "properties": {}}, handler)],
            audit_registry=AgentAuditRegistry(),
            logger=Logger(),
            model_factory=lambda _: Model(),
        )
        result = await runtime.run(
            "Read the data.",
            build_agent_model_settings(base_url="http://127.0.0.1:1", model="script", run_budget_seconds=90),
        )
        assert result["status"] == "ok"
        payload = json.loads(next(m["content"] for m in requests[1] if m["role"] == "tool"))
        assert EvidenceDecoder().decode(payload) == (project(raw)[0] if mode == "2" else raw)
        assert raw == data()
        counts.append((len(calls), len(requests)))
    assert counts == [(1, 2), (1, 2)]


def test_review_encoding_does_not_remove_additional_author_visible_fields(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import stage_review

    monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", "2")
    call = ToolCall("c", "inspect_route", "{}")
    messages = [
        {"role": "assistant", "tool_calls": [call.as_message_value()]},
        {"role": "tool", "tool_call_id": "c", "content": json.dumps(data())},
    ]
    review, evidence = stage_review.request_messages("q", {"answer": "a"}, messages)
    expanded = EvidenceDecoder().decode(json.loads(review[1]["content"]))
    assert expanded["evidence"] == evidence
    assert expanded["evidence"][0]["result"] == data()
