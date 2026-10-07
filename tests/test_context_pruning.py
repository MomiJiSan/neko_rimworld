import copy
import json

import pytest

from plugin.plugins.neko_rimworld.agent.context_pruning import ContextPruner, prune
from plugin.plugins.neko_rimworld.agent.contracts import ToolCall
from plugin.plugins.neko_rimworld.agent.model_context import make_pruner
from plugin.plugins.neko_rimworld.agent.tools import ToolRegistry, ToolSpec


def work_reading():
    from plugin.plugins.neko_rimworld.agent.object_tools import summarize_work_assignments

    facts = [{"object": {"id": "Doctor"}, "state_sample": {"tick": 8, "observation_id": "work-read"},
              "facts": {"work": {"status": "observed", "work_types_complete": True,
                  "current_job": {"definition": "GotoWander"}, "work_types": [
                      {"definition": "Doctor", "priority": 3, "natural_priority": 10, "disabled": False},
                      {"definition": "Art", "priority": None, "natural_priority": 2, "disabled": True},
                  ]}}}]
    coverage = {"listing": "complete", "matched": 1}
    return {"tool": "look", "data": {"facts": facts, "coverage": coverage,
            "work_assignments": summarize_work_assignments(facts, coverage)}}


def test_work_summary_is_omitted_only_when_visible_actor_rows_reproduce_it():
    value = work_reading()
    saved = copy.deepcopy(value)
    out, _, omissions = prune(value, {"aspects": ["work"]})
    assert value == saved and "work_assignments" not in out["data"]
    assert "duplicate_work_summary" in omissions
    restored = copy.deepcopy(out["data"]["facts"])
    work = restored[0]["facts"]["work"]
    work["work_types"] = [{"definition": key, **row} for key, row in work.pop("work_types_by_definition").items()]
    work["status"] = "observed"  # The successful record marker is implicit in the model view.
    assert restored == value["data"]["facts"]
    assert out["data"]["coverage"] == value["data"]["coverage"]
    # The unknown priority, disabled flag, actor and sample are real evidence.
    work = out["data"]["facts"][0]["facts"]["work"]
    assert work["work_types_by_definition"]["Art"]["priority"] is None and work["work_types_by_definition"]["Art"]["disabled"] is True


@pytest.mark.parametrize("change", ["cropped", "union", "different_order", "different_sample", "new_field", "different_meaning", "different_scope"])
def test_work_summary_with_independent_information_stays_visible(change):
    value = work_reading()
    data = value["data"]
    if change == "cropped":
        data["facts"] = []
    elif change == "union":
        data["work_assignments"]["source_pages"] = [{"page": 1}, {"page": 2}]
    elif change == "different_order":
        data["work_assignments"]["actor_readings"][0]["current_job"] = "DifferentJob"
    elif change == "different_sample":
        data["work_assignments"]["sample"] = {"game_tick": 7, "load_epoch": "old", "atomic": False}
    elif change == "different_meaning":
        data["work_assignments"]["meaning"] = "An independent qualification, not a duplicate explanation."
    elif change == "different_scope":
        data["work_assignments"]["scope"] = "Different selection"
    else:
        data["work_assignments"]["unknown_extension"] = {"value": None}
    out, _, _ = prune(value, {"aspects": ["work"]})
    assert out["data"]["work_assignments"] == data["work_assignments"]


@pytest.mark.asyncio
async def test_threat_projection_keeps_payload_scope_and_audit_without_extra_reads(monkeypatch):
    from plugin.plugins.neko_rimworld.observation import Observation

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    raw = {"observation_id": "threat-read", "received_at": "2026-10-05T00:00:00Z",
           "request": {"operation": "inspect_threats", "arguments": {"subjects": [{"id": "Doctor"}]}},
           "response": {"v": 2, "type": "query_result", "request_id": "wire-only", "seq": 1,
               "colony_id": "colony", "load_epoch": "load", "connection_epoch": "connection",
               "game_tick": 8, "snapshot_sequence": 9, "payload": {"status": "partial", "issues": ["unsupported"],
                   "data": {"native_active_threat_to_player": None, "scope": {"include_fogged": False},
                       "active_classification": {"status": "unavailable", "reason": "unsupported_behavior_patch"},
                       "subject_attack_checks": [{"subject": {"id": "Doctor"}, "target": {"id": "Enemy"},
                                                  "path_indices_count": 27, "can_hit": False}],
                       "extension": {"unknown": None}}}}}
    calls = []

    async def read(args):
        calls.append(args)
        return Observation(copy.deepcopy(raw))

    registry = ToolRegistry(context_pruner=make_pruner())
    registry.register(ToolSpec("inspect_threats", "Threat facts", {}, read))
    result = await registry.execute(registry.prepare(ToolCall("one", "inspect_threats", "{}")))
    visible = json.loads(result.as_message()["content"])["data"]
    assert calls == [{}] and result.observation == raw and result.value["data"] == raw
    assert visible["data"] == raw["response"]["payload"]["data"]
    assert visible["issues"] == raw["response"]["payload"]["issues"] and visible["status"] == "partial"
    assert visible["request"] == raw["request"]
    assert visible["sample"] == {k: raw["response"][k] for k in (
        "colony_id", "load_epoch", "connection_epoch", "game_tick", "snapshot_sequence")}
    assert "response" not in visible and "request_id" not in visible
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "0")
    registry = ToolRegistry()
    registry.register(ToolSpec("inspect_threats", "Threat facts", {}, read))
    unchanged = await registry.execute(registry.prepare(ToolCall("two", "inspect_threats", "{}")))
    assert unchanged.model_content is None and unchanged.value["data"] == raw


def test_unknown_native_envelope_fields_are_not_silently_lost():
    value = {"tool": "inspect_threats", "data": {"response": {"payload": {"data": {"x": 0}}, "extension": None}}}
    out, _, omissions = prune(value)
    assert out == value and omissions == []


def route():
    return {
        "ok": True,
        "tool": "inspect_route",
        "data": {
            "sample": {"load_epoch": "load", "started_tick": 4, "finished_tick": 5},
            "context": {"actor_id": "A", "user_id": "P"},
            "route": {
                "status": "found",
                "start": {"x": 1, "z": 2},
                "end": {"x": 3, "z": 4},
                "steps": 9,
                "native_cost": {"value": 126, "unit": "native_path_cost"},
                "segments": [{"door": None, "terrain": "Soil"}, {"door": {"id": "D", "passable": False}}],
            },
            "path_environment": {
                "gases": [{"type": "ToxGas", "cells": 2}],
                "coverage": {"complete": False},
                "path_environment_bands": [{"temperature": 50}],
            },
            "environment": {"gases": [], "coverage": {"complete": True}},
            "objects": [{"target": {"id": "shirt"}, "facets": {"condition": {"hit_points": 100}}}],
            "objects_summary": {"records": 1},
            "page": {"listing_complete": True},
            "extension": {"read_ms": 9, "meaning": "game data", "unknown_mod_fields": None},
        },
    }


def test_pruning_removes_incidental_inventory_without_losing_route_or_hazards():
    raw = route()
    saved = copy.deepcopy(raw)
    result, _, _ = prune(raw, {"include": ["environment"]})
    data = result["data"]
    assert raw == saved
    assert "objects" not in data and data["objects_included"] is False
    assert data["route"]["steps"] == 9
    assert data["route"]["native_cost"]["value"] == 126
    assert data["route"]["door_segments"][0]["door"]["passable"] is False
    assert data["path_environment"]["gases"] == raw["data"]["path_environment"]["gases"]
    assert data["path_environment"]["coverage"] == {"complete": False}
    assert data["sample"] == raw["data"]["sample"] and data["context"] == raw["data"]["context"]
    assert data["extension"] == raw["data"]["extension"]
    assert data["environment"]["gases"] == []  # Corridor and path never merged.


def test_same_call_opt_in_keeps_inventory_geometry_without_debug_metadata():
    raw = route()
    raw["data"]["read_ms"] = 1
    result, _, _ = prune(raw, {"include_objects": True, "include": ["environment", "geometry"]})
    expected = copy.deepcopy(raw)
    expected["data"].pop("read_ms")
    for key in ("start", "end"):
        cell = expected["data"]["route"].pop(key)
        expected["data"]["route"][key + "_xz"] = [cell["x"], cell["z"]]
    expected["data"].update(objects_included=True, environment_included=True)
    assert result == expected
    assert raw["data"]["read_ms"] == 1


def test_duplicate_identity_and_profile_copies_are_deleted_only_when_equal():
    identity = {"id": "P", "name": "Pawn", "faction": None}
    value = {
        "tool": "look",
        "data": {
            "facts": [
                {
                    "object": identity,
                    "facts": {
                        "identity": {"object": copy.deepcopy(identity)},
                        "profile": {"profile": {"age": 22, "traits": ["x"]}, "age": 22, "traits": ["x"]},
                    },
                }
            ]
        },
    }
    out, _, _ = prune(value, {"aspects": ["identity", "profile"]})
    facts = out["data"]["facts"][0]["facts"]
    assert "identity" not in facts and facts["profile"] == {"profile": {"age": 22, "traits": ["x"]}}
    value["data"]["facts"][0]["facts"]["identity"]["object"]["faction"] = "different"
    out, _, _ = prune(value, {"aspects": ["identity"]})
    assert out["data"]["facts"][0]["facts"]["identity"]["object"]["faction"] == "different"


def test_survey_explicit_categories_keep_scope_and_requested_subtotals():
    raw = {"tool": "survey_space", "data": {"region": {"map": "Map_0", "cell_count": 12},
        "environment": {"gases": [{"type": "ToxGas", "cells": 2}], "coverage": {"complete": False}},
        "objects": [{"id": "bed"}], "objects_summary": {"records": 1, "capabilities": [
            {"capability": "sleeping_place", "records": 1}, {"capability": "plant", "records": 0}]},
        "sample": {"atomic": False}, "issues": [{"code": "partial"}]}}
    saved = copy.deepcopy(raw)
    out, _, _ = prune(raw, {"include": ["environment"]})
    assert out["data"]["environment"] == raw["data"]["environment"]
    assert "objects" not in out["data"] and out["data"]["objects_included"] is False
    out, _, _ = prune(raw, {"include": ["objects"], "select": {"capabilities": ["sleeping_place"]}})
    assert "environment" not in out["data"] and out["data"]["environment_included"] is False
    assert out["data"]["objects_summary"]["capabilities"] == [{"capability": "sleeping_place", "records": 1}]
    assert out["data"]["objects_summary"]["capabilities_selection"] == ["sleeping_place"]
    for key in ("region", "sample", "issues"):
        assert out["data"][key] == raw["data"][key]
    assert raw == saved
    assert prune(raw)[0] == raw
    diagnostic = prune(raw, {"include": ["objects", "diagnostics"], "select": {"capabilities": ["sleeping_place"]}})[0]["data"]
    assert diagnostic == out["data"]


def test_hazard_cell_pairs_keep_every_coordinate_order_and_distinct_scope():
    cells = [{"kind": "cell", "map": "Map_0", "x": x, "z": 5} for x in (1, 2, 1)]
    raw = {"tool": "survey_space", "data": {
        "environment": {"attack_influences": [{"example_cells": cells, "path_indices_count": 27,
            "status": "partial", "affected_observed_cells": 27}]},
        "path_environment": {"attack_influences": [{"example_cells": cells[:1], "path_indices_count": 1}]},
        "extension": {"example_cells": cells}}}
    saved = copy.deepcopy(raw)
    out = prune(raw)[0]["data"]
    hazard = out["environment"]["attack_influences"][0]
    assert hazard["example_cells_xz"] == [[1, 5], [2, 5], [1, 5]] and hazard["example_cells_map"] == "Map_0"
    assert hazard["path_indices_count"] == 27 and hazard["status"] == "partial"
    assert out["path_environment"]["attack_influences"][0]["example_cells_xz"] == [[1, 5]]
    assert out["extension"] == raw["data"]["extension"] and raw == saved
    diagnostic = prune(raw, {"include": ["environment", "diagnostics"]})[0]["data"]
    assert diagnostic == prune(raw, {"include": ["environment"]})[0]["data"]
    raw["data"]["environment"]["attack_influences"][0]["example_cells"][1]["map"] = "Map_1"
    assert "example_cells" in prune(raw)[0]["data"]["environment"]["attack_influences"][0]


def test_sample_aliases_and_known_format_prose_only_are_removed():
    from plugin.plugins.neko_rimworld.agent.context_pruning import FORMAT_EXPLANATIONS

    raw = {"tool": "look", "data": {"sample": {"started_tick": 8, "finished_tick": 9, "atomic": False},
        "observation_started_tick": 8, "observation_finished_tick": 10,
        "consistency": FORMAT_EXPLANATIONS["consistency"], "notes": ["Decision-specific limitation."],
        "extension": {"consistency": FORMAT_EXPLANATIONS["consistency"]}}}
    out = prune(raw)[0]["data"]
    assert "observation_started_tick" not in out and out["observation_finished_tick"] == 10
    assert "consistency" not in out and out["notes"] == raw["data"]["notes"]
    assert out["extension"] == raw["data"]["extension"] and out["sample"] == raw["data"]["sample"]
    raw["data"]["consistency"] = "Different snapshot qualification."
    assert prune(raw)[0]["data"]["consistency"] == raw["data"]["consistency"]


def test_pruned_work_input_retains_zero_priority_rule_in_its_model_policy(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.context_tools import task_prompt
    from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger
    from plugin.plugins.neko_rimworld.agent.session import AgentRunSession

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    session = AgentRunSession.create(ledger=AgentAuditLedger("q"),
        system_prompt=task_prompt(4, references_supported=False), question="Compare Doctor work priorities.")
    value = work_reading()
    value["data"]["facts"][0]["facts"]["work"]["work_types"][0]["priority"] = 0
    value["data"].pop("work_assignments")
    visible = prune(value, {"aspects": ["work"]})[0]
    session.append_tool_message({"role": "tool", "tool_call_id": "work", "content": json.dumps(visible)})
    messages = session.request_messages("Continue the consultation.")
    assert "priority 0 is unassigned for autonomous work" in messages[0]["content"]
    assert "assigned priorities 1..4" in messages[0]["content"]
    doctor = json.loads(messages[2]["content"])["data"]["facts"][0]["facts"]["work"]["work_types_by_definition"]["Doctor"]
    assert doctor["priority"] == 0 and doctor["disabled"] is False


def test_identity_reuse_is_local_exact_and_never_merges_hazard_counts():
    pawn = {"id": "A", "name": "Near", "position_xz": [1, 2]}
    enemy = {"id": "E", "name": "Raider", "position_xz": [3, 4]}
    raw = {"tool": "evaluate_activity", "data": {"observations": [{"kind": "move", "actor": {"id": "A"},
        "observation": {"route": {"actor": pawn}, "environment_contexts": [{"pawn": pawn, "harmed_by_vacuum": True}],
            "environment": {"attack_influences": [{"source": enemy, "affected_observed_cells": 40}]},
            "path_environment": {"attack_influences": [{"source": enemy, "path_indices_count": 27, "status": "partial"}]}}}]}}
    saved = copy.deepcopy(raw)
    result = prune(raw)[0]["data"]["observations"][0]["observation"]
    assert result["route"]["actor"] == pawn
    assert result["environment_contexts"] == [{"pawn": {"id": "A"}, "harmed_by_vacuum": True}]
    assert result["environment"]["attack_influences"][0]["source"] == enemy
    assert result["path_environment"]["attack_influences"][0] == {"source": {"id": "E"}, "path_indices_count": 27, "status": "partial"}
    assert raw == saved
    raw["data"]["observations"][0]["observation"]["path_environment"]["attack_influences"][0]["source"] = {**enemy, "position_xz": [9, 9]}
    result = prune(raw)[0]["data"]["observations"][0]["observation"]
    assert result["path_environment"]["attack_influences"][0]["source"]["position_xz"] == [9, 9]


def test_health_keeps_all_exception_values_unknowns_and_explicit_anatomy_option():
    health = {
        "conditions": [{"part": 2, "tended": False}],
        "damaged_present_parts": [],
        "missing_parts": [{"index": 3}],
        "capacities": [{"definition": "Moving", "level": 0}],
        "pain_fraction": None,
        "body_parts": [{"index": 1, "health": 10}, {"index": 2, "health": 4}],
        "body_parts_complete": True,
        "body_parts_total": 3,
        "field_states": {"pain_fraction": {"status": "unavailable"}},
    }
    raw = {"tool": "look", "data": {"facts": [{"object": {"id": "A"}, "facts": {"health": health}}]}}
    out, _, _ = prune(raw, {"aspects": ["health"]})
    result = out["data"]["facts"][0]["facts"]["health"]
    assert result["condition_part_details"] == [{"index": 2, "health": 4}]
    assert result["body_parts_included"] is False and "body_parts_complete" not in result
    for key in ("conditions", "missing_parts", "pain_fraction", "field_states"):
        assert result[key] == health[key]
    assert result["capacities_by_definition"] == {"Moving": {"level": 0}}
    out, _, _ = prune(raw, {"aspects": ["health"], "include": ["body_parts"]})
    result = out["data"]["facts"][0]["facts"]["health"]
    assert result["body_parts"] == health["body_parts"] and result["body_parts_complete"] is True
    assert result["capacities_by_definition"] == {"Moving": {"level": 0}}


def test_focused_skills_do_not_turn_missing_names_into_zero_or_complete_population():
    raw = {
        "tool": "look",
        "data": {
            "facts": [
                {
                    "object": {"id": "P"},
                    "facts": {
                        "skills": {
                            "skills": [{"definition": "Medicine", "level": 0}, {"definition": "Cooking", "level": 12}],
                            "skills_complete": True,
                            "skills_total": 2,
                        }
                    },
                }
            ]
        },
    }
    out, _, _ = prune(raw, {"aspects": ["skills"], "focus": {"skills": ["Medicine", "ModAdded"]}})
    result = out["data"]["facts"][0]["facts"]["skills"]
    assert result["skills_by_definition"] == {"Medicine": {"level": 0}}
    assert result["skills_selection"]["unmatched"] == ["ModAdded"]
    assert result["skills_complete"] is True and len(result["skills_by_definition"]) == 1
    assert "skills_total" not in result  # Equal complete count reconstructs from the selected dictionary.
    assert result["skills_selection"]["requested"] == ["Medicine", "ModAdded"]
    assert len(raw["data"]["facts"][0]["facts"]["skills"]["skills"]) == 2


def test_notes_are_kept_once_without_references_and_failed_compiles_do_not_consume_them():
    compiler = ContextPruner()
    raw = {"tool": "look", "data": {"semantics": ["one rule", "one rule"], "value": None}}
    with pytest.raises(ValueError, match="evidence_view_limit"):
        compiler.compile(raw, max_view_chars=1)
    first = json.loads(compiler.compile(raw).content)
    second = json.loads(compiler.compile(raw).content)
    assert first["data"]["semantics"] == ["one rule"]
    assert "semantics" not in second["data"] and second["data"]["value"] is None
    assert "_neko_evidence_format" not in first


def test_failed_activity_result_remains_an_error_without_cycles():
    raw = {"ok": False, "tool": "evaluate_activity", "error": {"code": "timeout"}}
    assert json.loads(ContextPruner().compile(raw).content) == raw


@pytest.mark.asyncio
async def test_actual_registry_passes_inclusion_arguments_and_keeps_raw_result(monkeypatch):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    registry = ToolRegistry(context_pruner=make_pruner())

    async def read(args):
        return route()["data"]

    registry.register(ToolSpec("inspect_route", "path", {"type": "object"}, read))
    raw = await registry.execute(
        registry.prepare(ToolCall("r", "inspect_route", '{"include_objects":true,"include":["geometry"]}'))
    )
    assert "objects" in raw.value["data"] and "objects" in json.loads(raw.as_message()["content"])["data"]
    assert raw.model_view_metrics["format"] == "json"


@pytest.mark.asyncio
async def test_activity_default_reads_only_requested_paths_and_optional_reads_stay_one_call(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.activity import SemanticActivityDataSource, evaluate_activity
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    arguments = {
        "kind": "transfer",
        "actors": [{"kind": "pawn", "id": x, "map": "Map_0"} for x in ("a", "b", "c")],
        "subject": {"kind": "pawn", "id": "patient", "map": "Map_0"},
        "destinations": [{"kind": "thing", "id": f"bed{i}", "map": "Map_0"} for i in (1, 2, 3)],
    }
    native = NativeInteraction()
    result = await evaluate_activity(arguments, SemanticActivityDataSource(native))
    assert len(native.calls) == 9 and all(op == "inspect_route" for op, _ in native.calls)
    assert len(result["observations"]) == 9
    assert all(row["kind"] == "transfer" and row["origin"]["id"] == "patient" for row in result["observations"])
    assert all(
        row["observation"]["environment_included"] is False and "path_environment" not in row["observation"]
        for row in result["observations"]
    )
    native = NativeInteraction()
    result = await evaluate_activity(
        {
            **arguments,
            "include": ["approach", "destination_state", "environment"],
            "actor_aspects": ["health", "skills", "work"],
        },
        SemanticActivityDataSource(native),
    )
    assert len(native.calls) == 24
    assert len(result["observations"]) == 24
    assert {r["kind"] for r in result["observations"]} == {"actor_state", "approach", "transfer", "destination_state"}
    assert all("read" not in row for row in result["observations"])
    assert any(row["observation"].get("path_environment", {}).get("gases") for row in result["observations"])




def test_selector_nesting_repair_preserves_explicit_values_and_rejects_conflicts(monkeypatch):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    registry = ToolRegistry()
    registry.register(
        ToolSpec("count", "count", {"properties": {"object": {}, "metric": {}}, "additionalProperties": False}, None)
    )
    args = {"object": {"kind": "things"}, "metric": "records", "include_held": False, "include_fogged": True}
    call = registry.prepare(ToolCall("c", "count", json.dumps(args)))
    assert call.arguments == {
        "object": {"kind": "things", "include_held": False, "include_fogged": True},
        "metric": "records",
    }
    args["object"]["include_held"] = True
    rejected = registry.prepare(ToolCall("d", "count", json.dumps(args)))
    assert rejected.ok is False


def test_script_outputs_are_not_reinterpreted_as_native_metadata():
    raw = {"tool": "execute_python", "data": {"read_ms": 12, "body_parts": [], "meaning": "literal output"}}
    assert json.loads(ContextPruner().compile(raw).content) == raw




@pytest.mark.asyncio
async def test_state_no_longer_smuggles_all_pawn_facts_and_selected_health_still_arrives(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.object_tools import ObjectReader
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    native = NativeInteraction()
    result = await ObjectReader(native).look({"object": {"kind": "pawn", "id": "a"}, "aspects": ["state", "health"]})
    facts = result["facts"][0]["facts"]
    assert "pawn" not in facts["state"]["facets"] and "work" not in facts["state"]["facets"]
    assert facts["health"]["health_summary_fraction"] == 0.9
    assert "skills" not in facts["health"]
    assert len(native.calls) == 1


@pytest.mark.parametrize("pruning", ["1"])
def test_length_only_repair_does_not_replay_world_or_detail_and_never_hides_claim_errors(monkeypatch, pruning):
    from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger
    from plugin.plugins.neko_rimworld.agent.session import AgentRunSession

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", pruning)
    session = AgentRunSession.create(ledger=AgentAuditLedger("question"), system_prompt="policy", question="question")
    session.messages.append({"role": "tool", "content": "LARGE WORLD DATA"})
    session.submission.answer = [{"text": "A conditional answer; B remains unknown."}]
    session.submission.detail = "SAVED FULL REPORT"
    session.submission.errors = {"delivery": {"code": "handoff_over_budget"}}
    before = copy.deepcopy(session.messages)
    repair = session.length_repair_messages()
    assert "LARGE WORLD DATA" not in json.dumps(repair) and "SAVED FULL REPORT" not in json.dumps(repair)
    assert repair[2]["content"] == session.submission.answer[0]["text"]
    assert session.messages == before and session.submission.detail == "SAVED FULL REPORT"
    session.submission.errors = {"delivery": {"code": "unsupported_claim"}}
    assert session.length_repair_messages() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pruning", ["1"])
async def test_runtime_length_repair_uses_one_small_request_and_preserves_full_report(monkeypatch, pruning):
    from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime
    from plugin.plugins.neko_rimworld.tests.test_agent import FakeLogger, settings, turn

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", pruning)
    full = "SAVED DETAIL " * 400
    long_answer = "This is a conditional conclusion with uncertainty. " * 40

    class Model:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools, *, timeout):
            self.calls += 1
            if self.calls == 1:
                return turn(
                    "", ToolCall("first", "submit_response", json.dumps({"answer": long_answer, "detail": full}))
                )
            assert self.calls == 2
            assert len(tools) == 1 and tools[0]["function"]["name"] == "submit_response"
            assert "SAVED DETAIL" not in json.dumps(messages)
            assert set(tools[0]["function"]["parameters"]["properties"]) == {"answer"}
            return turn(
                "", ToolCall("repair", "submit_response", '{"answer":"Conditional conclusion; uncertainty remains."}')
            )

    model = Model()
    runtime = AgentRuntime(
        observation_tools=[], audit_registry=AgentAuditRegistry(), logger=FakeLogger(), model_factory=lambda _: model
    )
    result = await runtime.run("Answer with detail.", settings())
    assert result["status"] == "ok" and model.calls == 2
    assert full.strip() in result["answer"]


@pytest.mark.asyncio
async def test_redundant_move_leg_and_explicit_recipient_do_not_need_repair_calls(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.activity import SemanticActivityDataSource, evaluate_activity
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    native = NativeInteraction()
    result = await evaluate_activity(
        {
            "kind": "move",
            "actors": [{"kind": "pawn", "id": "a", "map": "Map_0"}],
            "destinations": [{"kind": "thing", "id": "bed1", "map": "Map_0"}],
            "user": {"kind": "pawn", "id": "patient", "map": "Map_0"},
            "include": ["approach", "destination_state"],
        },
        SemanticActivityDataSource(native),
    )
    assert len(native.calls) == 2
    assert [x["kind"] for x in result["observations"]] == ["move", "destination_state"]
    assert next(args for op, args in native.calls if op == "inspect_route")["user"]["id"] == "patient"
    assert next(args for op, args in native.calls if op == "inspect_state")["user"]["id"] == "patient"


@pytest.mark.asyncio
async def test_contextual_state_matrix_does_not_read_unrequested_paths_or_actor_profiles(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.activity import SemanticActivityDataSource, evaluate_activity
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    native = NativeInteraction()
    result = await evaluate_activity(
        {
            "kind": "state",
            "actors": [{"kind": "pawn", "id": x, "map": "Map_0"} for x in ("a", "b", "c")],
            "destinations": [{"kind": "thing", "id": f"bed{i}", "map": "Map_0"} for i in (1, 2, 3)],
            "user": {"kind": "pawn", "id": "patient", "map": "Map_0"},
        },
        SemanticActivityDataSource(native),
    )
    assert len(native.calls) == 9 and all(op == "inspect_state" for op, _ in native.calls)
    assert len(result["observations"]) == 9 and all(r["kind"] == "destination_state" for r in result["observations"])
    assert all(args["user"]["id"] == "patient" for _, args in native.calls)


def test_legacy_contract_does_not_advertise_unavailable_native_state_or_activity():
    from plugin.plugins.neko_rimworld.agent.context_tools import configure_read_spec, query_guide
    from plugin.plugins.neko_rimworld.agent.semantic_surface import tool_schemas

    spec = configure_read_spec(tool_schemas()[0])
    assert "state" not in spec["parameters"]["properties"]["aspects"]["items"]["enum"]
    assert "state returns" not in spec["description"]
    assert "evaluate_activity" not in query_guide(native_state=False, activity=False)


@pytest.mark.asyncio
async def test_named_focus_includes_its_aspect_in_the_same_native_read(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.object_tools import ObjectReader
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    native = NativeInteraction()
    result = await ObjectReader(native).look(
        {"object": {"kind": "pawn", "id": "a"}, "aspects": ["health"], "focus": {"skills": ["Medicine"]}}
    )
    assert len(native.calls) == 1 and native.calls[0][1]["sections"] == ["health", "skills"]
    assert result["facts"][0]["facts"]["skills"]["skills"][0]["definition"] == "Medicine"
