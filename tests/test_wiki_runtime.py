"""Definition knowledge survives delivery without becoming live evidence."""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from plugin.plugins.neko_rimworld.agent import AgentAuditLedger, AgentAuditRegistry, AgentRuntime, ToolCall
from plugin.plugins.neko_rimworld.agent.semantic_tools import ENVIRONMENT_SWITCH, semantic_interaction_factory
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession
from plugin.plugins.neko_rimworld.agent.tools import ToolRegistry, ToolSetup, ToolSpec
from plugin.plugins.neko_rimworld.agent.wiki_tools import WIKI_GUIDANCE
from plugin.plugins.neko_rimworld.reports import ReportStore

from .test_agent import FakeLogger, settings, submission_call, turn
from .test_semantic_tools import setup_for


class ScriptedModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.tools = []

    async def complete(self, messages, tools, *, timeout):
        assert timeout > 0
        self.requests.append(copy.deepcopy(messages))
        self.tools.append({tool["function"]["name"] for tool in tools})
        assert self.turns, "runtime requested an unexpected model turn"
        return self.turns.pop(0)


def definition():
    return {
        "status": "found",
        "knowledge_kind": "definition",
        "snapshot": {"format": "neko.defs.v1", "sha256": "a" * 64},
        "applicability": {"status": "unverified", "current_game_match_verified": False},
        "entries": [{
            "key": "Verse.ThingDef/Steel", "def_name": "Steel", "label": "steel",
            "description": "Definition data only.",
            "facts": [{"name": "stackLimit", "value": "75"}],
        }],
    }


def wiki_tool(data, after_read=None):
    async def read(arguments):
        assert isinstance(arguments["query"], str)
        if after_read is not None:
            after_read()
        return copy.deepcopy(data)

    return ToolSpec(
        "wiki", WIKI_GUIDANCE,
        {"type": "object", "properties": {"query": {"type": "string"}},
         "required": ["query"], "additionalProperties": False},
        read,
    )


@pytest.fixture(params=["offline", "semantic"])
def runtime_factory(request, monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_SWITCH, raising=False)

    def create(model, tool):
        setup = setup_for() if request.param == "semantic" else ToolSetup((), context=WIKI_GUIDANCE)
        setup = replace(setup, tools=(*setup.tools, tool))
        store = ReportStore()
        audit = AgentAuditRegistry()

        async def provide_tools():
            return setup

        async def save(record):
            return store.save(record)

        runtime = AgentRuntime(
            observation_tools=[], tools_provider=provide_tools,
            audit_registry=audit, logger=FakeLogger(), model_factory=lambda _: model,
            interaction_factory=semantic_interaction_factory, report_sink=save,
        )
        return runtime, store, audit

    return create


@pytest.mark.asyncio
async def test_wiki_then_submit_retains_knowledge_in_result_and_report(runtime_factory):
    data = definition()
    model = ScriptedModel([
        turn("", ToolCall("wiki-1", "wiki", '{"query":"Steel"}')),
        turn("", submission_call("Steel 的定义堆叠上限为 75；当前存档状态未读取。", "已查到 Steel 的定义。")),
    ])
    runtime, store, audit = runtime_factory(model, wiki_tool(data))
    result = await runtime.run("查询 Steel 的定义。", settings())

    expected = [{"query": "Steel", "result": data}]
    assert result["status"] == "ok"
    assert result["submission_mode"] == "submit_response"
    assert result["knowledge"] == expected
    assert result["knowledge_omitted"] == 0
    assert result["observations"] == []
    assert result["freshness"] is None
    assert result["report"]["saved"] is True
    saved = store.read(result["report"]["report_id"])
    assert saved["state"] == "complete"
    assert saved["knowledge"] == expected
    assert saved["knowledge_omitted"] == 0
    assert saved["observations"] == []
    assert saved["observation_count"] == 0
    assert saved["samples"] == []
    assert saved.get("freshness") is None
    assert saved["provenance"]["sources"] == []
    message = next(message for message in model.requests[1] if message.get("tool_call_id") == "wiki-1")
    assert json.loads(message["content"])["data"] == data
    assert "wiki" in model.tools[0]
    assert audit.snapshot()["latest_run"]["tool_calls"] == 2


@pytest.mark.asyncio
async def test_research_close_removes_wiki_and_keeps_obtained_knowledge(runtime_factory, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(AgentAuditLedger, "elapsed_seconds", property(lambda _: elapsed[0]))

    def close_research():
        elapsed[0] = 6.0

    model = ScriptedModel([
        turn("", ToolCall("wiki-1", "wiki", '{"query":"Steel"}')),
        turn("", ToolCall("wiki-closed", "wiki", '{"query":"Gold"}')),
        turn("", submission_call("已读取 Steel 定义。", "当前存档状态未读取。")),
    ])
    runtime, store, audit = runtime_factory(model, wiki_tool(definition(), close_research))
    result = await runtime.run("查询定义。", replace(settings(), finalization_reserve_seconds=5.0))

    assert result["status"] == "ok"
    assert "wiki" in model.tools[0]
    assert model.tools[1:] == [{"submit_response"}, {"submit_response"}]
    closed = next(message for message in model.requests[2] if message.get("tool_call_id") == "wiki-closed")
    assert json.loads(closed["content"])["error"]["code"] == "unknown_tool"
    assert result["knowledge"] == [{"query": "Steel", "result": definition()}]
    assert result["observations"] == []
    assert result["freshness"] is None
    assert store.read(result["report"]["report_id"])["knowledge"] == result["knowledge"]
    assert any(event["event"] == "research_closed" for event in audit.snapshot()["latest_run"]["events"])


@pytest.mark.asyncio
@pytest.mark.parametrize("claim,code", [
    ("Alice 是殖民者。", "identity_claim"),
    ("Steel 是我们的物资。", "ownership_claim"),
])
async def test_wiki_does_not_license_identity_or_ownership(runtime_factory, claim, code):
    data = definition()
    # Even evidence-shaped fields in a definition cannot grant live claims.
    data["entries"][0].update(is_free_colonist=True, faction={"is_player": True},
                              held_by_faction={"is_player": True})
    model = ScriptedModel([
        turn("", ToolCall("wiki-1", "wiki", '{"query":"Steel"}')),
        turn("", submission_call(claim, claim, "unsupported")),
        turn("", submission_call("仅查到定义，身份和归属未确认。", "当前存档未读取。", "corrected")),
    ])
    runtime, store, _ = runtime_factory(model, wiki_tool(data))
    result = await runtime.run("确认身份和归属。", settings())

    rejected = next(message for message in model.requests[2] if message.get("tool_call_id") == "unsupported")
    error = json.loads(rejected["content"])["error"]
    assert error["code"] == code
    assert code in {item["code"] for item in error["details"]["rejected_claims"]}
    assert result["status"] == "ok"
    assert result["observations"] == []
    assert result["freshness"] is None
    assert result["knowledge"] == [{"query": "Steel", "result": data}]
    assert claim not in result["answer"]
    assert claim not in store.read(result["report"]["report_id"])["full_text"]


@pytest.mark.asyncio
async def test_session_retains_first_sixteen_knowledge_results_and_counts_omissions():
    session = AgentRunSession.create(ledger=AgentAuditLedger("definitions"), system_prompt="test", question="test")
    registry = ToolRegistry()
    registry.register(wiki_tool(definition()))
    for index in range(19):
        call = ToolCall(str(index), "wiki", json.dumps({"query": f"Steel-{index}"}))
        result = await registry.execute(registry.prepare(call))
        session.retain_knowledge(result)
        result.value["data"]["snapshot"]["sha256"] = "mutated after retention"

    assert len(session.knowledge) == 16
    assert session.knowledge_omitted == 3
    assert [item["query"] for item in session.knowledge] == [f"Steel-{index}" for index in range(16)]
    assert all(item["result"] == definition() for item in session.knowledge)
    assert session.observations == []
    failed = registry.error_result(
        ToolCall("bad", "wiki", "{}"), "invalid_arguments", "missing query", recoverable=True,
    )
    session.retain_knowledge(failed)
    assert session.knowledge_omitted == 3


@pytest.mark.asyncio
async def test_audit_capacity_on_wiki_completion_keeps_knowledge(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import audit

    monkeypatch.setattr(audit, "MAX_EVENTS_PER_RUN", 8)
    model = ScriptedModel([turn("", ToolCall("wiki-1", "wiki", '{"query":"Steel"}'))])
    runtime = AgentRuntime(
        observation_tools=[wiki_tool(definition())], audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(), model_factory=lambda _: model,
    )
    result = await runtime.run("查询 Steel。", settings())

    assert result["status"] == "degraded"
    assert result["audit"]["error_code"] == "audit_capacity_reached"
    assert result["knowledge"] == [{"query": "Steel", "result": definition()}]
    assert result["knowledge_omitted"] == 0
    assert result["observations"] == []
    assert result["freshness"] is None
