"""Source identity, bounded reading and production evidence retention."""

import copy
import hashlib
import json

import pytest

from plugin.plugins.neko_rimworld.agent.mechanism_tools import compose_implementation_knowledge
from plugin.plugins.neko_rimworld.agent.semantic_tools import semantic_interaction_factory
from plugin.plugins.neko_rimworld.mechanisms.index import build_index
from plugin.plugins.neko_rimworld.mechanisms.store import MechanismStore
from plugin.plugins.neko_rimworld.query_tools import make_game_tools

from .test_agent import FakeLogger, settings, submission_call, turn
from .test_capability_composition import Script, native_observation
from .test_semantic_tools import public_source


def library(tmp_path):
    assembly = tmp_path / "Assembly-CSharp.dll"
    assembly.write_bytes(b"test-game-implementation")
    sources = tmp_path / "source"
    sources.mkdir()
    (sources / "Equip.cs").write_text(
        "namespace RimWorld;\npublic class Equip {\n public bool CanEquip() {\n  return true;\n }\n public void OrderedEquip() {\n  target.SetForbidden(false);\n }\n}\n", encoding="utf-8"
    )
    target = tmp_path / "generation"
    metadata = build_index(sources, assembly, target)
    implementation = {"format": "game_implementation.v1", "assembly_sha256": metadata["assembly_sha256"], "patched_methods": [], "patch_coverage": ["RimWorld.Equip"]}
    return target, metadata, implementation


def test_source_lookup_requires_same_game_and_preserves_omission(tmp_path):
    target, metadata, implementation = library(tmp_path)
    store = MechanismStore(target, implementation)
    reference = store.search("装备 禁止")["matches"][0]["source_ref"]
    data = store.read(reference, member="OrderedEquip", line_count=2)
    assert "target.SetForbidden(false)" in data["code"]
    assert not data["read_complete"] and data["not_read"]["before_line"] > 0
    assert data["knowledge_kind"] == "implementation"
    assert data["applicability"]["assembly_match_verified"]
    assert not data["applicability"]["instance_conditions_verified"]
    assert store.read("0" * 64 + "/RimWorld.Equip")["reason"] == "source_identity_mismatch"
    mismatch = MechanismStore(target, {**implementation, "assembly_sha256": "f" * 64})
    assert mismatch.search("Equip")["reason"] == "game_implementation_mismatch"
    assert mismatch.read(reference)["status"] == "unavailable"


def test_tampered_generation_cannot_be_read(tmp_path):
    target, _, implementation = library(tmp_path)
    with (target / "sources.sqlite").open("ab") as stream:
        stream.write(b"changed")
    assert MechanismStore(target, implementation).metadata is None


def test_source_window_and_search_are_data_not_execution(tmp_path):
    target, metadata, implementation = library(tmp_path)
    store = MechanismStore(target, implementation)
    assert store.search('" OR DELETE FROM sources --')["status"] in {"ok", "no_match"}
    with pytest.raises(ValueError):
        store.read(metadata["assembly_sha256"] + "/RimWorld.Equip", line_count=0)
    assert store.read(metadata["assembly_sha256"] + "/RimWorld.Equip", member="Unknown")["status"] == "member_not_found"
    before = hashlib.sha256((target / "sources.sqlite").read_bytes()).hexdigest()
    store.search("Equip")
    store.read(metadata["assembly_sha256"] + "/RimWorld.Equip")
    assert hashlib.sha256((target / "sources.sqlite").read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_mechanisms_and_native_actions_use_production_runtime_and_report(tmp_path):
    from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime, ToolCall
    from plugin.plugins.neko_rimworld.reports import ReportStore

    target, metadata, implementation = library(tmp_path)
    source = copy.deepcopy(public_source())
    source["game_implementation"] = implementation
    source["native_behavior"] = {"version": "native_behavior.v1", "tools": [
        {"name": name, "operation": name, "description": "Native inspected behavior", "parameters": {"type": "object"}} for name in ("inspect_actions", "inspect_threats")
    ]}
    requests = []

    async def native(request):
        requests.append(request)
        return native_observation(request)

    setup = await compose_implementation_knowledge(make_game_tools(source, native), target)
    reference = metadata["assembly_sha256"] + "/RimWorld.Equip"
    model = Script([
        turn("", ToolCall("a", "inspect_actions", '{"actor":{"name":"Lumi"}}')),
        turn("", ToolCall("b", "wiki", json.dumps({"query": "source:" + reference}))),
        turn("", submission_call("Manual equip and automatic behavior need separate conditions.", "Manual equip and automatic behavior need separate conditions.")),
    ])
    reports = ReportStore()

    async def provide():
        return setup

    async def save(record):
        return reports.save(record)

    runtime = AgentRuntime(observation_tools=[], audit_registry=AgentAuditRegistry(), logger=FakeLogger(), tools_provider=provide, model_factory=lambda _: model, interaction_factory=semantic_interaction_factory, report_sink=save)
    result = await runtime.run("Can Lumi equip this?", settings())
    assert result["status"] == "ok"
    assert requests == [{"operation": "inspect_actions", "arguments": {"actor": {"name": "Lumi"}}}]
    assert len(result["observations"]) == 1
    assert result["knowledge"][0]["tool"] == "wiki"
    assert result["knowledge"][0]["arguments"]["query"] == "source:" + reference
    assert reports.read(result["report"]["report_id"])["knowledge"] == result["knowledge"]
    for request in model.requests:
        names = {t["function"]["name"] for t in request["tools"]}
        assert {"inspect_actions", "inspect_threats", "wiki"} <= names
        assert not ({"read_mechanism", "search_mechanisms", "saved_reports", "workspace_checkpoint"} & names)
        assert "query" not in names


@pytest.mark.asyncio
async def test_missing_index_does_not_disable_current_tools(tmp_path):
    source = public_source()
    source["game_implementation"] = {"format": "game_implementation.v1", "assembly_sha256": "a" * 64}
    setup = make_game_tools(source, lambda _: None)
    result = await compose_implementation_knowledge(setup, tmp_path)
    assert result is setup


@pytest.mark.asyncio
async def test_wiki_preserves_definitions_and_exact_source_windows(tmp_path):
    from plugin.plugins.neko_rimworld.agent.tool_contracts import ToolSetup, ToolSpec, ToolExecutionError

    target, metadata, implementation = library(tmp_path)
    definition_queries = []

    async def definition(args):
        definition_queries.append(args)
        return {"knowledge_kind": "definition", "value": False, "nullable": None}

    setup = ToolSetup((ToolSpec("wiki", "Definitions", {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    }, definition),), directory={"game_implementation": implementation})
    composed = await compose_implementation_knowledge(setup, target)
    assert [tool.name for tool in composed.tools] == ["wiki"]
    wiki = composed.tools[0]
    assert list(wiki.parameters["properties"]) == ["query"]
    assert await wiki.handler({"query": "ThingDef/Steel"}) == {
        "knowledge_kind": "definition", "value": False, "nullable": None,
    }
    search = await wiki.handler({"query": "source:Equip"})
    key = search["matches"][0]["key"]
    expected = MechanismStore(target, implementation).read(key[len("source:"):], member="OrderedEquip", line_count=2)
    assert await wiki.handler({"query": key + " member=OrderedEquip lines=1:2"}) == expected
    mismatch = await wiki.handler({"query": "source:" + "0" * 64 + "/RimWorld.Equip"})
    assert mismatch["reason"] == "source_identity_mismatch"
    assert definition_queries == [{"query": "ThingDef/Steel"}]
    for suffix in (" lines=1:0", " lines=0:2", " lines=1:121", " unexpected=1"):
        with pytest.raises(ToolExecutionError):
            await wiki.handler({"query": key + suffix})


@pytest.mark.asyncio
async def test_missing_source_index_preserves_the_definition_tool(tmp_path):
    from plugin.plugins.neko_rimworld.agent.tool_contracts import ToolSetup, ToolSpec

    async def definition(args):
        return {"knowledge_kind": "definition", "query": args["query"]}

    setup = ToolSetup((ToolSpec("wiki", "Definitions", {}, definition),), directory={
        "game_implementation": {"format": "game_implementation.v1", "assembly_sha256": "a" * 64},
    })
    result = await compose_implementation_knowledge(setup, tmp_path)
    assert result is setup
    assert (await result.tools[0].handler({"query": "Steel"}))["knowledge_kind"] == "definition"
