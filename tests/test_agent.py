from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from plugin.plugins.neko_rimworld.agent import (
    AgentAuditCapacityError,
    AgentAuditLedger,
    AgentAuditRegistry,
    AgentModelSettings,
    AgentRunState,
    AgentRuntime,
    AssistantTurn,
    ModelClientError,
    OpenAICompatibleModelClient,
    ToolCall,
    build_agent_model_settings,
    parse_agent_model_settings,
)
from plugin.plugins.neko_rimworld.agent import loop as agent_loop
from plugin.plugins.neko_rimworld.agent.audit import MAX_EVENTS_PER_RUN
from plugin.plugins.neko_rimworld.agent.tools import ToolSpec
from plugin.plugins.neko_rimworld.observation import Observation


def query_tool(handler):
    return ToolSpec("query_game", "Test observation", {"type": "object", "properties": {}}, handler)


def sse_response(payload):
    chunks = []
    for choice in payload.get("choices", []):
        delta = dict(choice["message"])
        if delta.get("tool_calls"):
            delta["tool_calls"] = [{**call, "index": i} for i, call in enumerate(delta["tool_calls"])]
        chunks.append(
            {
                "id": payload.get("id"),
                "model": payload.get("model"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": choice["finish_reason"]}],
            }
        )
    if payload.get("usage") is not None:
        chunks.append({"choices": [], "usage": payload["usage"]})
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level: str, message: object, *args: object) -> None:
        rendered = str(message)
        for value in args:
            rendered = rendered.replace("{}", str(value), 1)
        self.records.append((level, rendered))

    def info(self, message: object, *args: object) -> None:
        self._record("info", message, *args)

    def warning(self, message: object, *args: object) -> None:
        self._record("warning", message, *args)

    def error(self, message: object, *args: object) -> None:
        self._record("error", message, *args)


class ScriptedModel:
    def __init__(self, turns: list[AssistantTurn | Exception]) -> None:
        self.turns = list(turns)
        self.requests: list[list[dict[str, Any]]] = []

    async def complete(self, messages, tools, *, timeout: float) -> AssistantTurn:
        assert timeout > 0
        assert tools and tools[0]["function"]["name"] == "query_game"
        self.requests.append([dict(message) for message in messages])
        item = self.turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def settings(*, preferred_cycles: int = 4, run_budget_seconds: float = 10) -> AgentModelSettings:
    return build_agent_model_settings(
        base_url="http://127.0.0.1:11434/v1",
        model="test-model",
        preferred_cycles=preferred_cycles,
        request_timeout_seconds=3,
        run_budget_seconds=run_budget_seconds,
    )


def turn(content: str = "", *calls: ToolCall) -> AssistantTurn:
    return AssistantTurn(
        provider_request_id="response-1",
        model="test-model",
        finish_reason="tool_calls" if calls else "stop",
        content=content,
        tool_calls=tuple(calls),
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


def submission_call(full_text: str, agent_text: str, call_id: str = "submit-1") -> ToolCall:
    return ToolCall(
        call_id,
        "submit_response",
        json.dumps({"detail": full_text, "answer": [{"text": agent_text}]}, ensure_ascii=False),
    )


def digest_view() -> Observation:
    return Observation(
        {
            "observation_id": "test-observation",
            "received_at": "2026-09-04T08:00:00.000Z",
            "request": {"operation": "colony_snapshot", "arguments": {}},
            "response": {
                "colony_id": "2" * 32,
                "load_epoch": "3" * 32,
                "game_tick": 123,
                "snapshot_sequence": 1,
                "payload": {"status": "ok", "data": {"maps": [], "new_field": {"nullable": None}}, "issues": []},
            },
        }
    )


@pytest.mark.asyncio
async def test_agent_can_answer_without_calling_a_tool() -> None:
    model = ScriptedModel([turn("No live game data was needed for this answer.")])
    registry = AgentAuditRegistry()

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("digest should not be called")

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("Explain what a colony is.", settings())

    assert result["status"] == "ok"
    assert result["answer"].startswith("No live")
    assert result["audit"]["cycles"] == 1
    assert result["audit"]["tool_calls"] == 0
    latest = registry.snapshot()["latest_run"]
    assert latest["state"] == AgentRunState.COMPLETED
    assert [event["event"] for event in latest["events"]] == [
        "run_created",
        "model_request_started",
        "model_response_received",
        "final_received",
        "run_completed",
        "handoff_built",
    ]


@pytest.mark.asyncio
async def test_agent_tool_call_round_trip_is_auditable() -> None:
    model = ScriptedModel(
        [
            turn("", ToolCall("call-1", "query_game", "{}")),
            turn("The colony currently has no visible threat."),
        ]
    )
    registry = AgentAuditRegistry()

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        return digest_view()

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("Is the colony safe?", settings())

    assert result["status"] == "ok"
    assert result["observations"][0]["response"]["snapshot_sequence"] == 1
    assert result["audit"]["cycles"] == 2
    assert result["audit"]["tool_calls"] == 1
    second_request = model.requests[1]
    assert any(message.get("role") == "assistant" and message.get("tool_calls") for message in second_request)
    tool_message = next(message for message in second_request if message.get("role") == "tool")
    tool_payload = json.loads(tool_message["content"])
    assert tool_payload["ok"] is True
    assert tool_payload["data"]["response"]["game_tick"] == 123

    latest = registry.snapshot()["latest_run"]
    event_names = [event["event"] for event in latest["events"]]
    assert event_names.index("model_response_received") < event_names.index("tool_call_requested")
    assert event_names.index("tool_started") < event_names.index("tool_completed")
    model_event = next(event for event in latest["events"] if event["event"] == "model_response_received")
    assert model_event["data"]["tool_calls"][0]["function"]["name"] == "query_game"


@pytest.mark.asyncio
async def test_agent_reuses_summary_and_appends_detail() -> None:
    full_text = "完整报告。" * 80
    agent_text = "殖民地目前稳定；完整报告已保存在插件结果中。"
    model = ScriptedModel([turn("", submission_call(full_text, agent_text))])
    registry = AgentAuditRegistry()

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("digest should not be called")

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("Write a detailed report.", settings())

    assert result["answer"] == agent_text + "\n\n" + full_text
    assert result["agent_text"] == agent_text
    assert result["summary"] == agent_text
    assert result["reply"] == agent_text
    assert result["submission_mode"] == "submit_response"
    assert result["delivery"]["format"] == "natural_text"
    assert result["audit"]["tool_calls"] == 1
    assert "RWA" not in result["summary"]
    latest = registry.snapshot()["latest_run"]
    assert any(event["event"] == "tool_completed" for event in latest["events"])


@pytest.mark.asyncio
async def test_over_budget_submission_is_returned_to_the_model_for_revision() -> None:
    over_budget = "这是一段应由内部Agent重新组织的自然语言。" * 100
    final_text = "已重新组织为可直接使用的简短回答。"
    model = ScriptedModel(
        [
            turn("", submission_call("完整报告", over_budget, "submit-too-long")),
            turn("", submission_call("完整报告", final_text, "submit-valid")),
        ]
    )

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("digest should not be called")

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("Answer with detail.", settings())

    tool_message = next(message for message in model.requests[1] if message.get("role") == "tool")
    assert json.loads(tool_message["content"])["error"]["code"] == "handoff_over_budget"
    assert result["summary"] == final_text
    assert result["audit"]["cycles"] == 2


@pytest.mark.asyncio
async def test_long_plain_text_gets_prompt_feedback_instead_of_code_summarization() -> None:
    long_text = "模型生成的完整报告。" * 100
    agent_text = "这是模型重写后的自然语言交接。"
    model = ScriptedModel(
        [
            turn(long_text),
            turn("", submission_call(long_text, agent_text)),
        ]
    )

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("digest should not be called")

    registry = AgentAuditRegistry()
    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("Write a detailed report.", settings())

    assert result["answer"] == agent_text + "\n\n" + long_text
    assert result["summary"] == agent_text
    assert any(
        "only required/changed fields" in str(message.get("content"))
        for message in model.requests[1]
        if message.get("role") == "user"
    )
    assert any(event["event"] == "final_text_rejected" for event in registry.snapshot()["latest_run"]["events"])


@pytest.mark.asyncio
async def test_prompt_cycle_target_is_soft_not_a_hard_step_limit() -> None:
    calls = [turn("", ToolCall(f"call-{index}", "query_game", "{}")) for index in range(4)]
    model = ScriptedModel([*calls, turn("Finished after the suggested cycle target.")])

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        return digest_view()

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("Keep checking until you are satisfied.", settings(preferred_cycles=2))

    assert result["status"] == "ok"
    assert result["audit"]["cycles"] == 5
    assert result["audit"]["tool_calls"] == 4


@pytest.mark.asyncio
async def test_unknown_tool_is_returned_to_model_instead_of_blocking_the_loop() -> None:
    model = ScriptedModel(
        [
            turn("", ToolCall("call-bad", "search_wiki", '{"query":"x"}')),
            turn("Search is unavailable in this version."),
        ]
    )

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        return digest_view()

    registry = AgentAuditRegistry()
    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("Search the wiki.", settings())

    assert result["status"] == "ok"
    tool_message = next(message for message in model.requests[1] if message.get("role") == "tool")
    assert json.loads(tool_message["content"])["error"]["code"] == "unknown_tool"
    latest = registry.snapshot()["latest_run"]
    assert latest["tool_failures"] == 1


@pytest.mark.asyncio
async def test_empty_model_turn_gets_auditable_recovery_prompt() -> None:
    model = ScriptedModel([turn(), turn("Recovered final answer.")])

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        return digest_view()

    registry = AgentAuditRegistry()
    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("Answer even after an empty turn.", settings())

    assert result["status"] == "ok"
    assert result["audit"]["cycles"] == 2
    assert any(event["event"] == "recovery_prompt_appended" for event in registry.snapshot()["latest_run"]["events"])


@pytest.mark.asyncio
async def test_model_failure_returns_degraded_result_without_stopping_runtime() -> None:
    secret = "private-provider-key"
    model = ScriptedModel(
        [
            ModelClientError(
                "model_stream_error",
                "The provider reported a stream error.",
                diagnostics={
                    "request_id": "request-123",
                    "phase": "first_response",
                    "provider_error": {
                        "code": "quota_exceeded",
                        "type": "insufficient_quota",
                        "message": f"quota exhausted; api_key={secret}",
                    },
                },
            )
        ]
    )

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        return digest_view()

    registry = AgentAuditRegistry()
    logger = FakeLogger()
    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=logger,
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("How is the colony?", settings())

    assert result["status"] == "degraded"
    assert result["audit"]["error_code"] == "model_stream_error"
    latest = registry.snapshot()["latest_run"]
    assert latest["state"] == "completed"
    assert any(event["event"] == "model_request_failed" for event in latest["events"])
    rendered_logs = json.dumps(logger.records)
    assert "provider_code=quota_exceeded" in rendered_logs
    assert "provider_type=insufficient_quota" in rendered_logs
    assert "provider_message=quota exhausted; api_key=<redacted>" in rendered_logs
    assert "request_id=request-123" in rendered_logs
    assert secret not in rendered_logs


@pytest.mark.asyncio
async def test_transient_provider_failure_is_retried_before_any_useful_delta() -> None:
    transient = ModelClientError(
        "model_stream_error",
        "The provider reported a stream error.",
        diagnostics={
            "request_id": "request-overloaded",
            "phase": "first_response",
            "provider_error": {
                "type": "upstream_error",
                "message": "Our servers are currently overloaded. Please try again later.",
            },
        },
    )
    model = ScriptedModel([transient, turn("The colony is available after retry.")])
    registry = AgentAuditRegistry()

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no game tool should run")

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("How is the colony?", settings(run_budget_seconds=30))

    assert result["status"] == "ok"
    assert result["answer"] == "The colony is available after retry."
    assert len(model.requests) == 2
    assert model.requests[0] == model.requests[1]
    latest = registry.snapshot()["latest_run"]
    assert latest["model_calls"] == 2
    assert latest["model_failures"] == 1
    assert [event["event"] for event in latest["events"]].count("model_retry_scheduled") == 1


@pytest.mark.asyncio
async def test_transient_failure_after_useful_delta_is_not_retried() -> None:
    transient_after_output = ModelClientError(
        "model_stream_error",
        "The provider reported a stream error.",
        diagnostics={
            "request_id": "request-partial",
            "phase": "stream_read",
            "first_useful_delta_ms": 50.0,
            "provider_error": {"type": "upstream_error", "message": "overloaded"},
        },
    )
    model = ScriptedModel([transient_after_output, turn("must not be used")])
    registry = AgentAuditRegistry()

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no game tool should run")

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("How is the colony?", settings())

    assert result["status"] == "degraded"
    assert len(model.requests) == 1
    assert not any(event["event"] == "model_retry_scheduled" for event in registry.snapshot()["latest_run"]["events"])


@pytest.mark.asyncio
async def test_non_retryable_http_failure_is_not_retried() -> None:
    permanent = ModelClientError(
        "model_http_error",
        "The agent model endpoint returned HTTP 400.",
        diagnostics={"request_id": "request-invalid", "phase": "response_headers", "http_status": 400},
    )
    model = ScriptedModel([permanent, turn("must not be used")])
    registry = AgentAuditRegistry()

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no game tool should run")

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("How is the colony?", settings(run_budget_seconds=30))

    assert result["status"] == "degraded"
    assert len(model.requests) == 1
    failure = next(
        event for event in registry.snapshot()["latest_run"]["events"] if event["event"] == "model_request_failed"
    )
    assert failure["data"]["retryable"] is False


@pytest.mark.asyncio
async def test_transient_retries_are_bounded_and_usage_is_accumulated(monkeypatch) -> None:
    monkeypatch.setattr(agent_loop, "MODEL_RETRY_DELAYS_SECONDS", (0.0, 0.0))

    def overloaded(request_id: str, tokens: int) -> ModelClientError:
        return ModelClientError(
            "model_stream_error",
            "The provider reported a stream error.",
            diagnostics={
                "request_id": request_id,
                "phase": "first_response",
                "provider_error": {"type": "upstream_error", "message": "servers overloaded"},
            },
            usage={"prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens},
        )

    model = ScriptedModel([overloaded("request-1", 2), overloaded("request-2", 3), overloaded("request-3", 5)])
    registry = AgentAuditRegistry()

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no game tool should run")

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: model,
    )
    result = await runtime.run("How is the colony?", settings(run_budget_seconds=30))

    assert result["status"] == "degraded"
    assert len(model.requests) == agent_loop.MAX_MODEL_ATTEMPTS == 3
    latest = registry.snapshot()["latest_run"]
    assert latest["model_calls"] == 3
    assert latest["model_failures"] == 3
    assert latest["usage"]["total_tokens"] == 10
    assert [event["event"] for event in latest["events"]].count("model_retry_scheduled") == 2
    assert latest["failure_summary"] == {
        "finish_reason": None,
        "max_output_tokens": None,
        "usage": {"completion_tokens": 0, "prompt_tokens": 5, "total_tokens": 5},
        "error_code": "model_stream_error",
        "message": "The provider reported a stream error.",
        "phase": "first_response",
        "attempts": 3,
        "current_cycle_attempts": 3,
        "failed_attempts": 3,
        "retries": 2,
        "provider_failures": 3,
        "provider_error": {"type": "upstream_error", "message": "servers overloaded"},
    }


@pytest.mark.asyncio
async def test_wall_time_fuse_returns_collected_evidence_instead_of_a_hard_failure() -> None:
    class SlowToolModel:
        calls = 0

        async def complete(self, messages, tools, *, timeout: float) -> AssistantTurn:
            self.calls += 1
            if self.calls > 1:
                await asyncio.sleep(0.2)
            return turn("", ToolCall("call-slow", "query_game", "{}"))

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        return digest_view()

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _settings: SlowToolModel(),
    )
    tiny_budget = AgentModelSettings(
        base_url="http://127.0.0.1:11434/v1",
        model="test-model",
        request_timeout_seconds=1,
        run_budget_seconds=0.05,
    )

    result = await runtime.run("Inspect the colony.", tiny_budget)

    assert result["status"] == "degraded"
    assert result["answer"] == "The internal RimWorld agent could not complete a normal answer."
    assert result["observations"][0]["response"]["snapshot_sequence"] == 1
    assert result["audit"]["error_code"] == "agent_run_budget"


@pytest.mark.asyncio
async def test_concurrent_runs_have_independent_message_sessions() -> None:
    seen_questions: list[str] = []

    class EchoModel:
        async def complete(self, messages, tools, *, timeout: float) -> AssistantTurn:
            await asyncio.sleep(0.01)
            question = next(message["content"] for message in messages if message.get("role") == "user")
            seen_questions.append(question)
            return turn(f"answer:{question}")

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        return digest_view()

    registry = AgentAuditRegistry()
    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: EchoModel(),
    )
    first, second = await asyncio.gather(
        runtime.run("question-one", settings()),
        runtime.run("question-two", settings()),
    )

    assert first["answer"] == "answer:question-one"
    assert second["answer"] == "answer:question-two"
    assert first["audit"]["run_id"] != second["audit"]["run_id"]
    assert sorted(seen_questions) == ["question-one", "question-two"]
    assert registry.snapshot()["recent_count"] == 2


@pytest.mark.asyncio
async def test_cancellation_is_recorded_and_does_not_leave_an_active_run() -> None:
    started = asyncio.Event()

    class WaitingModel:
        async def complete(self, messages, tools, *, timeout: float) -> AssistantTurn:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        return digest_view()

    registry = AgentAuditRegistry()
    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _settings: WaitingModel(),
    )
    task = asyncio.create_task(runtime.run("wait", settings()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = registry.snapshot()
    assert snapshot["active_count"] == 0
    assert snapshot["latest_run"]["state"] == AgentRunState.CANCELLED
    assert snapshot["latest_run"]["events"][-1]["event"] == "run_cancelled"


def test_audit_redacts_credentials_but_keeps_model_output_and_transitions() -> None:
    ledger = AgentAuditLedger("api_key=super-secret")
    ledger.transition(AgentRunState.MODEL_REQUESTING, "model_request_started", {"api_key": "super-secret"})
    ledger.transition(
        AgentRunState.MODEL_RESPONDED,
        "model_response_received",
        {"content": "visible model output", "tool_calls": []},
    )
    snapshot = ledger.snapshot()
    encoded = json.dumps(snapshot, ensure_ascii=False)

    assert "super-secret" not in encoded
    assert "visible model output" in encoded
    assert snapshot["events"][1]["state_before"] == "created"
    assert snapshot["events"][1]["state_after"] == "model_requesting"


def test_audit_capacity_records_its_terminal_reason() -> None:
    ledger = AgentAuditLedger("bounded audit")
    for index in range(MAX_EVENTS_PER_RUN - 2):
        ledger.record("observation", {"index": index})

    with pytest.raises(AgentAuditCapacityError):
        ledger.record("one-too-many")

    snapshot = ledger.snapshot()
    assert snapshot["state"] == "failed"
    assert snapshot["events"][-1]["event"] == "audit_capacity_reached"


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["empty", "provider_failure", "success"])
@pytest.mark.parametrize("capacity", [22, 23, 24, 25])
async def test_runtime_audit_capacity_retains_observations_draft_and_result(monkeypatch, ending, capacity):
    from plugin.plugins.neko_rimworld.agent import audit
    from plugin.plugins.neko_rimworld.reports import ReportStore

    monkeypatch.setattr(audit, "MAX_EVENTS_PER_RUN", capacity)
    registry = AgentAuditRegistry()
    store = ReportStore()

    class Model:
        count = 0

        async def complete(self, *args, **kwargs):
            self.count += 1
            if self.count == 1:
                return turn("", ToolCall("read", "query_game", "{}"))
            if self.count == 2:
                return turn("", ToolCall("draft", "submit_response", '{"detail":"Retained evidence report"}'))
            if ending == "provider_failure":
                raise ModelClientError("model_http_error", "HTTP 400")
            return turn("Brief answer" if ending == "success" else "")

    async def read(_):
        return digest_view()

    async def save(record):
        return store.save(record)

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(read)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _: Model(),
        report_sink=save,
    )
    result = await runtime.run("Inspect", settings())
    latest = registry.snapshot()["latest_run"]
    assert latest["result"] is not None
    assert result["observations"] == [digest_view().to_dict()]
    assert result["answer"].endswith("Retained evidence report")
    assert registry.snapshot()["active_count"] == 0
    assert len(latest["events"]) <= capacity
    if latest["events"][-1]["event"] == "audit_capacity_reached":
        assert result["status"] == "degraded"
        assert result["audit"]["error_code"] == "audit_capacity_reached"
        assert store.read(result["report"]["report_id"])["state"] == "draft"


def test_agent_settings_are_independent_and_safely_validated() -> None:
    empty, empty_warnings = parse_agent_model_settings(
        {"neko_rimworld": {"agent": {"base_url": "", "model": "", "api_key": "", "preferred_cycles": 4}}}
    )
    parsed, warnings = parse_agent_model_settings(
        {
            "neko_rimworld": {
                "agent": {
                    "base_url": "http://127.0.0.1:11434/v1/",
                    "model": "local-model",
                    "api_key": "",
                }
            }
        }
    )
    unsafe, unsafe_warnings = parse_agent_model_settings(
        {
            "neko_rimworld": {
                "agent": {
                    "base_url": "http://example.com/v1",
                    "model": "remote-model",
                    "api_key": "secret",
                }
            }
        }
    )

    assert not empty.configured
    assert empty_warnings == []
    assert warnings == []
    assert parsed.configured
    assert parsed.base_url == "http://127.0.0.1:11434/v1"
    assert not unsafe.configured
    assert unsafe_warnings
    assert "secret" not in " ".join(unsafe_warnings)


@pytest.mark.asyncio
async def test_openai_compatible_client_parses_content_and_tool_calls() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = json.loads(request.content)
        return sse_response(
            {
                "id": "resp-1",
                "model": "served-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "I will inspect the colony.",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "query_game", "arguments": "{}"},
                                }
                            ],
                            "reasoning_content": "hidden reasoning must not be copied",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            },
        )

    client = OpenAICompatibleModelClient(
        settings(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        [{"role": "user", "content": "status?"}],
        [{"type": "function", "function": {"name": "query_game", "parameters": {}}}],
        timeout=3,
    )

    assert captured["request"]["tool_choice"] == "auto"
    assert result.content == "I will inspect the colony."
    assert result.tool_calls[0].name == "query_game"
    assert result.reasoning_metadata["present"] is True
    assert "hidden reasoning" not in json.dumps(result.as_audit_value())


@pytest.mark.asyncio
async def test_http_error_does_not_disclose_key_or_response_body() -> None:
    api_key = "private-agent-key"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"authorization failed for {api_key}")

    model_settings = build_agent_model_settings(
        base_url="https://example.com/v1",
        model="test-model",
        api_key=api_key,
        request_timeout_seconds=3,
        run_budget_seconds=10,
    )
    client = OpenAICompatibleModelClient(model_settings, transport=httpx.MockTransport(handler))

    with pytest.raises(ModelClientError) as caught:
        await client.complete([{"role": "user", "content": "test"}], [], timeout=3)

    assert caught.value.code == "model_http_error"
    assert api_key not in str(caught.value)


@pytest.mark.asyncio
async def test_http_model_tool_loop_end_to_end() -> None:
    request_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if len(request_bodies) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-live-1",
                        "type": "function",
                        "function": {"name": "query_game", "arguments": "{}"},
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            assert any(item.get("role") == "tool" for item in body["messages"])
            message = {"role": "assistant", "content": "The live colony is stable."}
            finish_reason = "stop"
        return sse_response(
            {
                "id": f"resp-{len(request_bodies)}",
                "model": "test-model",
                "choices": [{"finish_reason": finish_reason, "message": message}],
            },
        )

    transport = httpx.MockTransport(handler)

    async def digest(_args: dict[str, Any]) -> dict[str, Any]:
        return digest_view()

    registry = AgentAuditRegistry()
    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(digest)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda model_settings: OpenAICompatibleModelClient(
            model_settings,
            transport=transport,
        ),
    )
    result = await runtime.run("Inspect my colony.", settings())

    assert result["status"] == "ok"
    assert result["answer"] == "The live colony is stable."
    assert result["audit"]["model_calls"] == 2
    assert result["audit"]["tool_calls"] == 1
    assert len(request_bodies) == 2


@pytest.mark.asyncio
async def test_audit_limit_on_tool_completion_keeps_just_read_observation(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import audit

    monkeypatch.setattr(audit, "MAX_EVENTS_PER_RUN", 8)
    registry = AgentAuditRegistry()

    async def read(_):
        return digest_view()

    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[query_tool(read)],
        audit_registry=registry,
        logger=FakeLogger(),
        model_factory=lambda _: ScriptedModel([turn("", ToolCall("q", "query_game", "{}"))]),
    )
    result = await runtime.run("Inspect", settings())
    assert result["audit"]["error_code"] == "audit_capacity_reached"
    assert result["observations"] == [digest_view().to_dict()]


@pytest.mark.asyncio
async def test_cancellation_at_audit_limit_still_propagates(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import audit

    monkeypatch.setattr(audit, "MAX_EVENTS_PER_RUN", 3)
    started = asyncio.Event()

    class Model:
        async def complete(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

    registry = AgentAuditRegistry()
    runtime = AgentRuntime(
        allow_text_completion=True,  # This legacy loop fixture explicitly tests natural-text completion.
        observation_tools=[], audit_registry=registry, logger=FakeLogger(), model_factory=lambda _: Model()
    )
    task = asyncio.create_task(runtime.run("Cancel", settings()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = registry.snapshot()
    assert snapshot["active_count"] == 0
    assert snapshot["latest_run"]["result"]["status"] == "cancelled"
