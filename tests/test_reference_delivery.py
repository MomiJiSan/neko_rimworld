"""Reference delivery at the runtime and unchanged host's text boundaries.

Only the model/game/network boundaries are scripted. These are offline transport
regressions, not a claim about a live host, game, or the persona model's answer.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from utils.result_parser import parse_plugin_result

from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime, ToolCall
from plugin.plugins.neko_rimworld.agent.handoff import (
    HandoffValidationError,
    SubmissionDraft,
    SubmissionPatchError,
    attach_agent_handoff,
    count_handoff_tokens,
    make_submission_tool,
    validate_submission,
)
from plugin.plugins.neko_rimworld.agent.tools import ToolSetup
from plugin.plugins.neko_rimworld.observation import Observation

from .test_agent import FakeLogger, ScriptedModel, query_tool, settings, turn
from .test_presentation import observed, part


@pytest.fixture
def host_transport(monkeypatch):
    """Execute unchanged host emitter/renderer; replace only network and clock.

    Isolated package loading avoids starting main_server or model services. The
    real tokenizer, budgets, result parser, emitter and callback renderer run.
    """
    root = Path(__file__).resolve().parents[4]
    namespace = "_rimworld_delivery_host"
    package = ModuleType(namespace)
    package.__path__ = []
    monkeypatch.setitem(sys.modules, namespace, package)
    events = []

    class Bridge:
        async def emit_to_main(self, event):
            events.append(event)
            return True

    shared = ModuleType(namespace + "._shared")
    shared.logger = logging.getLogger(namespace)
    shared.Modules = SimpleNamespace(agent_bridge=Bridge())
    monkeypatch.setitem(sys.modules, shared.__name__, shared)
    registry = ModuleType(namespace + ".registry")
    registry._now_iso = lambda: "2026-09-19T00:00:00Z"
    monkeypatch.setitem(sys.modules, registry.__name__, registry)

    def load(name, relative):
        spec = importlib.util.spec_from_file_location(namespace + "." + name, root / relative)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        return module

    emitter = load("results", "app/agent_server/results.py")
    renderer = load("callback_render", "main_logic/core/callback_render.py")
    return emitter, renderer, events


@pytest.mark.asyncio
async def test_accepted_reference_survives_runtime_and_host_projection(host_transport):
    # Independently chosen text puts this reference handoff in (180, 200]. It
    # must preserve both interpretation and the explicit unknown/lower bound.
    conclusion = "结论：" + "判断。" * 80
    model = ScriptedModel(
        [
            turn("", ToolCall("read", "query_game", "{}")),
            turn(
                "",
                ToolCall(
                    "submit",
                    "submit_response",
                    json.dumps(
                        {
                            "answer": [{"text": conclusion}, part()],
                            "detail": "这是完整报告的额外说明，不应投影给宿主。",
                        },
                        ensure_ascii=False,
                    ),
                ),
            ),
        ]
    )
    observation = observed()
    observation["received_at"] = "2026-09-19T00:00:00Z"
    observation["request"] = {"operation": "query", "arguments": {}}

    async def read(_):
        return Observation(observation)

    async def setup():
        return ToolSetup((query_tool(read),), references_supported=True)

    runtime = AgentRuntime(
        allow_text_completion=True,
        observation_tools=[],
        tools_provider=setup,
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
    )
    result = await runtime.run("报告读数及其限制。", settings())

    assert result["status"] == "ok"
    assert 180 < count_handoff_tokens(result["agent_text"])[0] <= 200
    assert result["summary"] == result["reply"] == result["agent_text"]
    assert conclusion in result["summary"] and "精确值未知，至少12" in result["summary"]
    assert result["delivery"]["fact_tokens"] > 0
    assert result["delivery"]["host_truncation_expected"] is False
    assert "error_code" not in result["delivery"]
    projected = parse_plugin_result(result, llm_result_fields=["summary"], lang="zh")
    assert projected == result["agent_text"]
    assert "额外说明" not in projected

    emitter, renderer, events = host_transport
    # user_plugin passes the projected text as BOTH summary and detail.
    await emitter._emit_task_result(
        "小天",
        channel="user_plugin",
        task_id="test",
        success=True,
        summary=projected,
        detail=projected,
        source_kind="plugin",
        source_name="RimWorld",
    )
    (event,) = events
    assert event["summary"] == event["detail"] == projected
    callback = {**event, "origin": "task_result"}
    from config import AGENT_CALLBACK_TEXT_MAX_TOKENS, AGENT_CALLBACK_TOTAL_MAX_TOKENS

    assert count_handoff_tokens(projected)[0] <= AGENT_CALLBACK_TEXT_MAX_TOKENS
    selected, deferred = renderer._select_callbacks_within_token_budget([callback], AGENT_CALLBACK_TOTAL_MAX_TOKENS)
    assert not deferred
    prompt = renderer._build_callback_instruction(
        selected, lang="zh", lanlan_name="小天", master_name="测试者", passive=True
    )
    assert projected in prompt
    assert "额外说明" not in prompt and "callback-safe response" not in prompt
    voice = renderer._render_pending_extra_replies_by_origin(
        selected, lang="zh", lanlan_name="小天", master_name="测试者"
    )
    assert projected in voice


@pytest.mark.asyncio
async def test_oversized_reference_is_repaired_before_host_delivery():
    observation = observed()
    observation["received_at"] = "2026-09-19T00:00:00Z"
    observation["request"] = {"operation": "query", "arguments": {}}
    payload = observation["response"]["payload"]
    payload["data"]["extra"] = "background " * 250
    payload["execution"]["evidence"]["nodes"]["/extra"] = {"kind": "scalar", "name": "Extra"}
    model = ScriptedModel(
        [
            turn("", ToolCall("read", "query_game", "{}")),
            turn(
                "",
                ToolCall(
                    "too-wide",
                    "submit_response",
                    json.dumps(
                        {
                            "answer": [part(paths=["/reading", "/extra"])],
                        }
                    ),
                ),
            ),
            turn(
                "",
                ToolCall(
                    "narrow",
                    "submit_response",
                    json.dumps(
                        {
                            "answer": [part()],
                        }
                    ),
                ),
            ),
        ]
    )

    async def read(_):
        return Observation(observation)

    async def setup():
        return ToolSetup((query_tool(read),), references_supported=True)

    runtime = AgentRuntime(
        allow_text_completion=True,
        observation_tools=[],
        tools_provider=setup,
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
    )
    result = await runtime.run("报告读数及其限制，无需背景字段。", settings())

    assert len(model.requests) == 3, "oversized reference must reach the model as a repairable rejection"
    feedback = json.loads(next(m["content"] for m in model.requests[2] if m.get("tool_call_id") == "too-wide"))
    assert feedback["error"]["code"] == "handoff_transport_over_budget"
    assert "200" in feedback["error"]["message"]
    assert result["status"] == "ok"
    assert "精确值未知，至少12" in result["summary"]
    assert "background" not in result["summary"]
    assert result["summary"] == result["agent_text"]
    assert result["delivery"]["host_truncation_expected"] is False
    assert parse_plugin_result(result, llm_result_fields=["summary"], lang="zh") == result["summary"]


def scalar_observation():
    value = observed()
    payload = value["response"]["payload"]
    payload["data"] = {chr(97 + i): 0 for i in range(17)}
    payload["execution"]["evidence"]["nodes"] = {"/" + key: {"kind": "scalar", "name": key} for key in payload["data"]}
    return value


def test_reference_limit_applies_across_separated_parts_of_one_observation():
    observation = scalar_observation()
    paths = ["/" + key for key in observation["response"]["payload"]["data"]]
    schema = make_submission_tool(SubmissionDraft(), [observation]).schema()
    reference_schema = schema["function"]["parameters"]["properties"]["answer"]["items"]["oneOf"][1]
    assert reference_schema["properties"]["paths"]["maxItems"] == 16
    # One legal 16-path selection can be split/interleaved without changing the
    # per-observation limit. A seventeenth path cannot bypass it by adding text.
    accepted = SubmissionDraft().apply(
        {
            "answer": [
                part(paths=paths[:8]),
                {"text": "说明。"},
                part(paths=paths[8:16]),
            ]
        },
        observations=[observation],
    )
    assert "p=0" in accepted["agent_text"]
    with pytest.raises(SubmissionPatchError, match="16"):
        SubmissionDraft().apply(
            {
                "answer": [
                    part(paths=paths[:8]),
                    {"text": "说明。"},
                    part(paths=paths[8:]),
                ]
            },
            observations=[observation],
        )


@pytest.mark.asyncio
async def test_plain_text_detail_repair_preserves_saved_reference_share():
    observation = observed()
    observation["received_at"] = "2026-09-19T00:00:00Z"
    observation["request"] = {"operation": "query", "arguments": {}}
    observation["response"]["payload"]["execution"]["evidence"]["nodes"]["/reading"]["context"]["scope"] = (
        "scope " * 160
    )
    detail = "补充说明。" * 100
    model = ScriptedModel(
        [
            turn("", ToolCall("read", "query_game", "{}")),
            turn(
                "",
                ToolCall(
                    "draft",
                    "submit_response",
                    json.dumps(
                        {
                            "answer": [part()],
                            "detail": "x" * 12001,
                        }
                    ),
                ),
            ),
            turn(detail),
        ]
    )

    async def read(_):
        return Observation(observation)

    async def setup():
        return ToolSetup((query_tool(read),), references_supported=True)

    runtime = AgentRuntime(
        allow_text_completion=True,
        observation_tools=[],
        tools_provider=setup,
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
    )
    result = await runtime.run("报告读数及其限制。", settings())
    assert result["status"] == "ok"
    assert result["submission_mode"] == "text_repair"
    assert 180 < count_handoff_tokens(result["agent_text"])[0] <= 200
    assert result["summary"] == result["agent_text"]
    assert result["delivery"]["fact_tokens"] > 180
    assert "精确值未知，至少12" in result["summary"]
    assert detail in result["answer"] and detail not in result["summary"]
    assert parse_plugin_result(result, llm_result_fields=["summary"], lang="zh") == result["summary"]


@pytest.mark.parametrize("paths", [[], ["/a"] * 17])
def test_reference_part_rejects_paths_outside_its_public_schema(paths):
    with pytest.raises(SubmissionPatchError, match="1..16"):
        SubmissionDraft().apply(
            {"answer": [{"observation_id": "one", "paths": paths}]}, observations=[scalar_observation()]
        )


def test_reference_prompt_and_tool_describe_actual_budget_layers():
    from plugin.plugins.neko_rimworld.agent.prompt import build_finalization_prompt, build_system_prompt

    tool = make_submission_tool(SubmissionDraft(), [], references_supported=True)
    for text in [tool.description, build_system_prompt(4), build_finalization_prompt(4)]:
        assert "180 tokens" in text and "200-token transport limit" in text
        assert "combined answer parts must fit 180" not in text
    assert "16" in json.dumps(tool.schema())
    assert "32" in json.dumps(tool.schema())


def test_host_transport_limit_matches_real_parser_without_weakening_text_budget():
    from config import TASK_DETAIL_MAX_TOKENS

    from plugin.plugins.neko_rimworld.agent.handoff import HANDOFF_TRANSPORT_TOKEN_BUDGET

    assert HANDOFF_TRANSPORT_TOKEN_BUDGET <= TASK_DETAIL_MAX_TOKENS
    exact = " x" * 200
    assert count_handoff_tokens(exact.strip())[0] == 200
    accepted = validate_submission("完整报告", exact, fact_tokens=200)
    projected = parse_plugin_result({"summary": accepted["agent_text"]}, llm_result_fields=["summary"], lang="zh")
    assert projected == accepted["agent_text"]
    with pytest.raises(HandoffValidationError) as rejected:
        validate_submission("完整报告", " x" * 201, fact_tokens=201)
    assert rejected.value.code == "handoff_transport_over_budget"
    with pytest.raises(HandoffValidationError) as model_text:
        validate_submission("完整报告", " x" * 181)
    assert model_text.value.code == "handoff_over_budget"


def test_failed_final_projection_cannot_remain_successful():
    result = attach_agent_handoff(
        {"status": "ok", "answer": "完整报告", "agent_text": " x" * 201, "delivery": {"fact_tokens": 201}}
    )
    assert result["status"] == "degraded"
    assert result["delivery"]["submission_mode"] == "operational_fallback"
    assert result["delivery"]["error_code"] == "handoff_transport_over_budget"
    assert result["summary"] != result["agent_text"]
