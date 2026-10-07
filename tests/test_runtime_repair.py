"""Current completion protocol, private provider continuation and stale-world exits."""

import json
from types import SimpleNamespace

import httpx
import pytest

from plugin.plugins.neko_rimworld.agent.audit import AgentAuditRegistry, audit_safe, private_protocol_redacted
from plugin.plugins.neko_rimworld.agent.contracts import AssistantTurn, ToolCall
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, SubmissionPatchError
from plugin.plugins.neko_rimworld.agent.loop import AgentRuntime
from plugin.plugins.neko_rimworld.agent.model_client import OpenAICompatibleModelClient, build_agent_model_settings
from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError, ToolSpec

LOG = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)


def settings():
    return build_agent_model_settings(
        base_url="http://127.0.0.1:1", model="offline", run_budget_seconds=10, request_timeout_seconds=3
    )


def submit(text="Verified report."):
    return AssistantTurn(
        "offline", "offline", "tool_calls", "", (ToolCall("submit", "submit_response", json.dumps({"answer": text})),)
    )


class Script:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0

    async def complete(self, messages, tools, *, timeout):
        self.calls += 1
        return self.turns.pop(0)


@pytest.mark.asyncio
async def test_progress_text_is_not_a_successful_submission():
    model = Script([AssistantTurn("offline", "offline", "stop", "Next I will inspect the route risks."), submit()])
    registry = AgentAuditRegistry()
    runtime = AgentRuntime(observation_tools=[], audit_registry=registry, logger=LOG, model_factory=lambda _: model)
    result = await runtime.run("Compare routes.", settings())
    assert model.calls == 2 and result["status"] == "ok"
    assert result["submission_mode"] == "submit_response" and result["agent_text"] == "Verified report."
    assert any(e["event"] == "explicit_submission_required" for e in registry.snapshot()["latest_run"]["events"])




@pytest.mark.asyncio
async def test_world_changed_stops_the_batch_and_prevents_next_model_call():
    reads = []

    async def changed(_):
        reads.append("changed")
        raise ToolExecutionError("world_changed", "Reload detected", recoverable=False)

    async def forbidden(_):
        reads.append("should-not-run")
        return {}

    calls = tuple(ToolCall(str(i), n, "{}") for i, n in enumerate(["changed", "later"]))
    model = Script([AssistantTurn("offline", "offline", "tool_calls", "", calls), submit("Stale claim.")])
    runtime = AgentRuntime(
        observation_tools=[ToolSpec("changed", "test", {}, changed), ToolSpec("later", "test", {}, forbidden)],
        audit_registry=AgentAuditRegistry(),
        logger=LOG,
        model_factory=lambda _: model,
    )
    result = await runtime.run("Current world?", settings())
    assert reads == ["changed"] and model.calls == 1
    assert result["status"] == "degraded" and result["audit"]["error_code"] == "world_changed"
    assert "stale" in result["answer"]


@pytest.mark.asyncio
async def test_provider_continuation_roundtrip_is_private_in_audit():
    marker = "SYNTHETIC_PRIVATE_CONTINUATION_42"
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            deltas = [
                {"reasoning_content": marker[:12]},
                {
                    "reasoning_content": marker[12:],
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "read",
                            "type": "function",
                            "function": {"name": "read_value", "arguments": "{}"},
                        }
                    ],
                },
            ]
        else:
            prior = next(m for m in body["messages"] if m["role"] == "assistant")
            assert prior["reasoning_content"] == marker
            deltas = [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "submit",
                            "type": "function",
                            "function": {
                                "name": "submit_response",
                                "arguments": json.dumps({"answer": "Read completed."}),
                            },
                        }
                    ]
                }
            ]
        chunks = [
            {
                "id": "offline",
                "model": "offline",
                "choices": [{"index": 0, "delta": d, "finish_reason": "tool_calls" if i == len(deltas) - 1 else None}],
            }
            for i, d in enumerate(deltas)
        ]
        content = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())

    async def read(_):
        return {"value": 1}

    registry = AgentAuditRegistry()
    runtime = AgentRuntime(
        observation_tools=[ToolSpec("read_value", "test", {}, read)],
        audit_registry=registry,
        logger=LOG,
        model_factory=lambda s: OpenAICompatibleModelClient(s, transport=httpx.MockTransport(respond)),
    )
    result = await runtime.run("Read value.", settings())
    assert result["status"] == "ok" and len(requests) == 2
    assert marker not in json.dumps(registry.snapshot(), ensure_ascii=False)
    public = private_protocol_redacted(requests)
    assert marker not in json.dumps(public)
    assert private_protocol_redacted(public) == public
    assert audit_safe(public) == audit_safe(requests)
    assert marker not in repr(AssistantTurn("x", "y", "stop", "", reasoning_content=marker))


def test_length_repair_retains_detail_without_retransmission():
    draft = SubmissionDraft()
    detail = "The complete requested evidence stays saved."
    with pytest.raises(SubmissionPatchError, match="maximum"):
        draft.apply({"answer": [{"text": "word " * 250}], "detail": detail}, references_supported=False)
    result = draft.apply(
        {"answer": [{"text": "Main conclusion; conditions are in the full report."}]}, references_supported=False
    )
    assert draft.detail == detail and detail in result["full_text"]
    assert draft.snapshot()["complete"]
