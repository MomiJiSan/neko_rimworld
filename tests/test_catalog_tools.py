from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime, AssistantTurn, ToolCall
from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError
from plugin.plugins.neko_rimworld.observation import Observation
from plugin.plugins.neko_rimworld.protocol import decode_envelope
from plugin.plugins.neko_rimworld.query_tools import CatalogTools, make_game_tools

from .test_agent import FakeLogger, settings, submission_call

FIXTURES = Path(__file__).parent / "fixtures"


def test_nonreference_prompt_separates_short_conclusion_from_complete_facts():
    from plugin.plugins.neko_rimworld.agent.prompt import build_system_prompt
    prompt = build_system_prompt(4, references_supported=False)
    assert "Observation reference parts are unavailable" in prompt
    assert "detail for the complete factual report" in prompt
    assert "180 tokens" in prompt and "12000 characters" in prompt
    assert "Pure factual answers can contain only reference parts" not in prompt


def catalog():
    return json.loads((FIXTURES / "query_catalog.json").read_text(encoding="utf-8-sig"))


def response():
    return decode_envelope((FIXTURES / "template_result.json").read_bytes())


@pytest.mark.asyncio
async def test_public_query_replaces_legacy_tools_without_changing_observations():
    source = catalog()
    source["public_query"] = {"version": "public_query.v1", "parameters": {"type": "object", "properties": {"requests": {"type": "array"}}}, "rules": "canonical fields", "roots": []}
    source["public_query"]["field_notes"] = {"HealthSummary": {"health_percent": "A native aggregate, not a causal explanation."}}
    calls = []

    async def query(arguments):
        calls.append(copy.deepcopy(arguments))
        return Observation.from_response(arguments["operation"], arguments["arguments"], response())

    setup = make_game_tools(source, query)
    assert [t.name for t in setup.tools] == ["query", "describe_query"]
    assert setup.references_supported is False
    arguments = {"requests": [{"from": "things", "aggregate": {"units": {"op": "sum", "field": "stack_count"}}}]}
    observed = await setup.tools[0].handler(arguments)
    assert calls == [{"operation": "query", "arguments": arguments}]
    assert observed.value["response"] == response().to_dict()
    assert "type Pawn {" not in setup.context
    assert '"field_notes"' in setup.context and "not a causal explanation" in setup.context


def _public_source(**public):
    source = catalog()
    source["public_query"] = {"version": "public_query.v2",
                             "parameters": {"type": "object", "properties": {"requests": {"type": "array"}}},
                             "rules": "canonical fields", "roots": []}
    source["public_query"].update(public)
    return source


async def _noop_query(arguments):
    raise AssertionError("no query expected")


@pytest.mark.asyncio
async def test_fact_references_are_opt_in_not_the_shipped_default():
    """默认必须是纯文本，直到在线配对实验给出结论。

    v1.3 把引用提交发到真实运行里，用了引用的 7 份提交全部未交付。把默认打开等于
    未经验证的晋升，不符合本项目自己的止损纪律。所以：Mod 声明能力是必要条件，
    显式选择候选臂才是充分条件。
    """
    advertised = _public_source(references={"version": "observed_values.v1", "coverage": "x"})
    assert make_game_tools(advertised, _noop_query).references_supported is False
    assert make_game_tools(_public_source(), _noop_query).references_supported is False


@pytest.mark.asyncio
async def test_reference_mode_can_be_selected_per_run(monkeypatch):
    advertised = _public_source(references={"version": "observed_values.v1"})
    monkeypatch.setenv("NEKO_PUBLIC_REFERENCES", "1")
    assert make_game_tools(advertised, _noop_query).references_supported is True
    # 旧 Mod 没声明能力时，即使显式选择也不能发明引用。
    assert make_game_tools(_public_source(), _noop_query).references_supported is False
    monkeypatch.setenv("NEKO_PUBLIC_REFERENCES", "0")
    assert make_game_tools(advertised, _noop_query).references_supported is False


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["resource_evidence.v1", "resource_evidence.v2"])
async def test_resource_evidence_mode_preserves_three_tools_and_raw_observation(version):
    source = catalog()
    source["resource_parameters"] = {"type": "object", "properties": {"map_id": {"type": "string"}}}
    source["resource_contract"] = {"version": version, "scope_rules": {"stored": "overlaps ground"}}
    calls = []

    async def query(arguments):
        calls.append(arguments)
        return Observation.from_response(arguments["operation"], arguments["arguments"], response())

    setup = make_game_tools(source, query)
    assert [t.name for t in setup.tools] == ["get_game_info", "query_game", "describe_game_schema"]
    assert "resources" in setup.tools[0].parameters["properties"]
    result = await setup.tools[0].handler({"resources": {"map_id": "Map_0"}})
    assert calls == [{"operation": "resource_report", "arguments": {"map_id": "Map_0"}}]
    assert result.value["response"] == response().to_dict()
    assert version in setup.context


@pytest.mark.asyncio
async def test_mod_catalog_generates_both_tools_and_forwards_without_rewriting():
    calls = []

    async def query(arguments):
        calls.append(copy.deepcopy(arguments))
        return Observation.from_response(arguments["operation"], arguments["arguments"], response())

    setup = make_game_tools(catalog(), query)
    assert [t.name for t in setup.tools] == ["get_game_info", "query_game", "describe_game_schema"]
    templates, exact, _describe = setup.tools
    assert templates.parameters == catalog()["template_parameters"]
    schema_text = json.dumps(templates.parameters)
    for name in (
        "target",
        "sections",
        "colony",
        "colonists",
        "pawn",
        "status",
        "mood",
        "health",
        "equipment",
        "profile",
    ):
        assert f'"{name}"' in schema_text
    assert '"Pawn"' in setup.context
    assert "type Pawn {" not in setup.context
    args = {
        "requests": [
            {"target": {"type": "colony"}, "sections": ["overview"]},
            {
                "target": {"type": "pawn", "id": "pawn1"},
                "sections": ["status", "mood", "health", "equipment", "profile"],
            },
        ]
    }
    observed = await templates.handler(args)
    assert calls[-1] == {"operation": "template_query", "arguments": args}
    assert observed.value["response"] == response().to_dict()
    binding = observed.value["response"]["payload"]["execution"]["bindings"][1]
    assert binding["target"] == {"type": "pawn", "id": "pawn1"}
    assert binding["sections"] == args["requests"][1]["sections"]
    assert observed.value["response"]["payload"]["data"][binding["root"]]["id"] == "pawn1"
    precise = {
        "document": "query($id:ID!){pawn(id:$id){id name mood{level} health{summary{healthPercent}}}}",
        "variables": {"id": "pawn1"},
    }
    await exact.handler(precise)
    assert calls[-1] == {"operation": "graphql", "arguments": precise}


@pytest.mark.asyncio
async def test_object_selection_and_sections_are_mod_owned_and_forwarded_intact():
    calls = []

    async def query(arguments):
        calls.append(copy.deepcopy(arguments))
        return Observation.from_response(arguments["operation"], arguments["arguments"], response())

    source = catalog()
    setup = make_game_tools(source, query)
    # The plugin consumes the Mod's published schema without sharing mutable state.
    source["template_parameters"]["description"] = "changed after tool setup"
    assert setup.tools[0].parameters == catalog()["template_parameters"]
    arguments = {
        "requests": [
            {"target": {"type": "colonists", "name": "Walter", "first": 10}, "sections": ["summary"]},
            {"target": {"type": "pawn", "id": "pawn1"}, "sections": ["mood"]},
            {"target": {"type": "pawn", "id": "pawn1"}, "sections": ["health", "mood"]},
            {"target": {"type": "pawn", "id": "pawn2"}, "sections": ["equipment", "profile"]},
        ]
    }
    await setup.tools[0].handler(arguments)
    # Repeated targets/sections are merged in Mod; Python must preserve intent for audit.
    assert calls == [{"operation": "template_query", "arguments": arguments}]


@pytest.mark.asyncio
async def test_catalog_cache_singleflight_reconnect_and_bound_tools():
    class Bridge:
        connection_key = "1" * 32

    bridge = Bridge()
    requests = []

    async def query(arguments):
        requests.append(arguments)
        await asyncio.sleep(0)
        raw = response().to_dict()
        raw["connection_epoch"] = bridge.connection_key
        raw["payload"] = {"status": "ok", "issues": [], "data": catalog()}
        return Observation({"response": raw})

    provider = CatalogTools(lambda: bridge, query)
    a, b = await asyncio.gather(provider.load(), provider.load())
    assert len(requests) == 1
    assert a.context == b.context
    bridge.connection_key = "8" * 32
    with pytest.raises(ToolExecutionError, match="connection changed"):
        await a.tools[0].handler({"requests": []})
    await provider.load()
    assert len(requests) == 2
    bridge.connection_key = None
    offline = await provider.load()
    assert not offline.tools and "unavailable" in offline.context


@pytest.mark.asyncio
async def test_catalog_failure_does_not_publish_stale_descriptions():
    class Bridge:
        connection_key = "1" * 32

    async def query(_):
        raise ToolExecutionError("catalog_unavailable", "No game loaded.")

    provider = CatalogTools(lambda: Bridge(), query)
    setup = await provider.load()
    assert not setup.tools and "No game loaded" in setup.context


@pytest.mark.asyncio
async def test_object_sections_batch_is_one_tool_call_with_schema_in_model_context():
    calls = []

    async def query(arguments):
        calls.append(arguments)
        return Observation.from_response(arguments["operation"], arguments["arguments"], response())

    setup = make_game_tools(catalog(), query)

    class Model:
        count = 0

        async def complete(self, messages, tools, *, timeout):
            self.count += 1
            assert '"Pawn"' in messages[0]["content"]
            assert {t["function"]["name"] for t in tools} == {
                "get_game_info",
                "query_game",
                "describe_game_schema",
                "submit_response",
            }
            if self.count == 1:
                return AssistantTurn(
                    "r",
                    "m",
                    "tool_calls",
                    "",
                    (
                        ToolCall(
                            "c",
                            "get_game_info",
                            json.dumps(
                                {
                                    "requests": [
                                        {"target": {"type": "colony"}, "sections": ["overview"]},
                                        {
                                            "target": {"type": "pawn", "id": "pawn1"},
                                            "sections": ["status", "mood", "health", "equipment", "profile"],
                                        },
                                    ]
                                }
                            ),
                        ),
                    ),
                )
            payload = json.loads(messages[-1]["content"])["data"]["response"]["payload"]
            pawn = payload["data"][payload["execution"]["bindings"][1]["root"]]
            assert pawn["id"] == "pawn1"
            assert {"status", "mood", "health", "equipment", "profile"}.issubset(pawn)
            # 不要在这里写 “colonist”：这个 fixture 的观察里没有身份字段，
            # 那句话会被提交时的断言检查（正确地）拒掉，而本用例测的是工具批量调用。
            return AssistantTurn("r", "m", "stop", "I inspected the requested pawn's condition and equipment.")

    async def provider():
        return setup

    runtime = AgentRuntime(
        allow_text_completion=True,  # Explicit legacy natural-text completion fixture.
        observation_tools=[],
        tools_provider=provider,
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: Model(),
    )
    result = await runtime.run("Inspect the colony and pawn1", settings())
    assert result["status"] == "ok" and len(calls) == len(result["observations"]) == 1
    assert "execution" not in result["summary"] and "o1" not in result["summary"]


@pytest.mark.asyncio
async def test_terminal_batch_cannot_submit_before_observation_is_seen():
    async def query(args):
        return Observation.from_response(args["operation"], args["arguments"], response())

    setup = make_game_tools(catalog(), query)

    class Model:
        count = 0

        async def complete(self, messages, tools, *, timeout):
            self.count += 1
            if self.count == 1:
                return AssistantTurn(
                    "r",
                    "m",
                    "tool_calls",
                    "",
                    (
                        ToolCall("q", "query_game", '{"document":"{game{paused}}"}'),
                        submission_call("Premature", "Premature"),
                    ),
                )
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            assert results[-1]["error"]["code"] == "terminal_tool_must_be_alone"
            return AssistantTurn("r", "m", "tool_calls", "", (submission_call("Observed", "Observed"),))

    runtime = AgentRuntime(
        allow_text_completion=True,  # Explicit legacy natural-text completion fixture.
        observation_tools=list(setup.tools),
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: Model(),
    )
    result = await runtime.run("Inspect", settings())
    assert result["answer"] == "Observed" and result["audit"]["model_calls"] == 2


@pytest.mark.asyncio
async def test_schema_discovery_is_cached_metadata_and_never_fresh_evidence():
    requests = []
    spec = catalog()

    async def query(arguments):
        requests.append(copy.deepcopy(arguments))
        return Observation(
            {
                "response": {
                    "payload": {
                        "status": "ok",
                        "data": {
                            "schema_id": spec["schema_id"],
                            "metadata_only": True,
                            "types": [{"name": "Thing", "fields": [{"name": "stackCount", "type": "Int"}]}],
                        },
                    }
                }
            }
        )

    setup = make_game_tools(spec, query)
    assert spec["schema"] not in setup.context
    assert len(setup.context) < len(spec["schema"])

    class Model:
        count = 0

        async def complete(self, messages, tools, *, timeout):
            self.count += 1
            if self.count == 1:
                return AssistantTurn(
                    "r",
                    "m",
                    "tool_calls",
                    "",
                    tuple(ToolCall(str(i), "describe_game_schema", '{"types":["Thing"]}') for i in range(2)),
                )
            return AssistantTurn("r", "m", "stop", "The schema describes stackCount; no current game state was read.")

    async def provider():
        return setup

    runtime = AgentRuntime(
        allow_text_completion=True,  # Explicit legacy natural-text completion fixture.
        observation_tools=[],
        tools_provider=provider,
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: Model(),
    )
    result = await runtime.run("Describe supported fields", settings())
    assert result["status"] == "ok"
    assert result["observations"] == [] and result["freshness"] is None
    assert requests == [{"operation": "describe_schema", "arguments": {"types": ["Thing"]}}]
    first = await setup.tools[2].handler({"types": ["Thing"]})
    first["types"].clear()
    assert (await setup.tools[2].handler({"types": ["Thing"]}))["types"]


@pytest.mark.asyncio
async def test_schema_discovery_rejects_mismatched_directory_and_invalid_requests():
    calls = []

    async def query(arguments):
        calls.append(arguments)
        return Observation(
            {
                "response": {
                    "payload": {"status": "ok", "data": {"schema_id": "wrong", "metadata_only": True, "types": []}}
                }
            }
        )

    describe = make_game_tools(catalog(), query).tools[2]
    with pytest.raises(ToolExecutionError, match="does not match"):
        await describe.handler({"types": ["Thing"]})
    for arguments in (
        {"types": []},
        {"types": ["Thing"] * 9},
        {"types": [42]},
        {"types": ["Thing"], "include_templates": "yes"},
    ):
        with pytest.raises(ToolExecutionError, match="Select"):
            await describe.handler(arguments)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_v3_has_one_resource_entry_and_shared_conditions():
    source = catalog()
    source["resource_parameters"] = {"type": "object", "properties": {"definition_where": {"type": "object"}}}
    source["resource_contract"] = {"version": "resource_evidence.v3"}
    source["selection_contract"] = {"version": "schema_conditions.v1"}
    calls = []

    async def query(arguments):
        calls.append(arguments)
        return Observation.from_response(arguments["operation"], arguments["arguments"], response())

    setup = make_game_tools(source, query)
    assert [t.name for t in setup.tools] == ["read_resources", "get_game_info", "query_game", "describe_game_schema"]
    assert "resources" not in setup.tools[1].parameters["properties"]
    condition = {"definition_where": {"fields": {"IsWeapon": {"eq": True}}}}
    await setup.tools[0].handler(condition)
    assert calls == [{"operation": "resource_report", "arguments": condition}]
    assert "schema_conditions.v1" in setup.context
    assert "Default supplies" not in setup.context
