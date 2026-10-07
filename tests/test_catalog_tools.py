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
    assert "detail holds the remaining requested findings and evidence" in prompt
    assert "180 tokens" in prompt and "12000 characters" in prompt
    assert "Pure factual answers can contain only reference parts" not in prompt


def catalog():
    return json.loads((FIXTURES / "query_catalog.json").read_text(encoding="utf-8-sig"))


def response():
    return decode_envelope((FIXTURES / "template_result.json").read_bytes())


@pytest.mark.asyncio
async def test_public_query_replaces_legacy_tools_without_changing_observations():
    source = catalog()
    source["public_query"] = {
        "version": "public_query.v1",
        "parameters": {"type": "object", "properties": {"requests": {"type": "array"}}},
        "rules": "canonical fields",
        "roots": [],
    }
    source["public_query"]["field_notes"] = {
        "HealthSummary": {"health_percent": "A native aggregate, not a causal explanation."}
    }
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
    source["public_query"] = {
        "version": "public_query.v2",
        "parameters": {"type": "object", "properties": {"requests": {"type": "array"}}},
        "rules": "canonical fields",
        "roots": [],
    }
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
async def test_live_reference_mode_remains_disabled_even_when_advertised(monkeypatch):
    advertised = _public_source(references={"version": "observed_values.v1"})
    monkeypatch.setenv("NEKO_PUBLIC_REFERENCES", "1")
    assert make_game_tools(advertised, _noop_query).references_supported is False
    # 旧 Mod 没声明能力时，即使显式选择也不能发明引用。
    assert make_game_tools(_public_source(), _noop_query).references_supported is False
    monkeypatch.setenv("NEKO_PUBLIC_REFERENCES", "0")
    assert make_game_tools(advertised, _noop_query).references_supported is False








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
        raw["payload"] = {"status": "ok", "issues": [], "data": _public_source()}
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
async def test_terminal_batch_cannot_submit_before_observation_is_seen():
    async def query(args):
        return Observation.from_response(args["operation"], args["arguments"], response())

    setup = make_game_tools(_public_source(), query)

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
                        ToolCall("q", "query", '{"document":"{game{paused}}"}'),
                        submission_call("Premature", "Premature"),
                    ),
                )
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            assert results[-1]["error"]["code"] == "terminal_tool_must_be_alone"
            return AssistantTurn("r", "m", "tool_calls", "", (submission_call("Observed", "Observed"),))

    runtime = AgentRuntime(
        observation_tools=list(setup.tools),
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: Model(),
    )
    result = await runtime.run("Inspect", settings())
    assert result["answer"] == "Observed" and result["audit"]["model_calls"] == 2








def test_retired_catalogs_fail_without_querying_or_publishing_tools():
    for source in (catalog(), _public_source(version="unsupported")):
        with pytest.raises(ToolExecutionError) as caught:
            make_game_tools(source, _noop_query)
        assert caught.value.code == "semantic_surface_unavailable"
        assert caught.value.recoverable is False


def test_public_catalog_does_not_require_retired_template_contracts():
    source = _public_source()
    for key in ("schema", "query_rules", "template_parameters", "query_parameters", "type_index", "query_roots"):
        source.pop(key, None)
    CatalogTools._validate(source)
    assert [tool.name for tool in make_game_tools(source, _noop_query).tools] == ["query", "describe_query"]
