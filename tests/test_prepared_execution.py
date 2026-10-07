"""Caller-prepared work uses the same execution and delivery as single queries."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger, AgentAuditRegistry
from plugin.plugins.neko_rimworld.agent.contracts import AssistantTurn, ToolCall
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionPatchError
from plugin.plugins.neko_rimworld.agent.loop import AgentRuntime
from plugin.plugins.neko_rimworld.agent.model_client import build_agent_model_settings
from plugin.plugins.neko_rimworld.agent.prompt import build_system_prompt
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession

LOG = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)


class Script:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def complete(self, messages, tools, *, timeout):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        return self.turns.pop(0)


def submit(text):
    return AssistantTurn(
        "offline", "offline", "tool_calls", "", (ToolCall("submit", "submit_response", json.dumps({"answer": text})),)
    )


def settings():
    return build_agent_model_settings(
        base_url="http://127.0.0.1:1", model="offline", run_budget_seconds=10, request_timeout_seconds=3
    )


def prepare(question, context=()):
    config = settings()
    return AgentRunSession.create(
        ledger=AgentAuditLedger(question),
        question=question,
        system_prompt=build_system_prompt(config.preferred_cycles),
        context_messages=context,
    )


@pytest.mark.asyncio
async def test_caller_context_is_detached_and_does_not_replace_current_question():
    history = {"role": "user", "content": "Historical actor Near; not a fresh observation."}
    session = prepare("Compare Doctor now.", (history,))
    history["content"] = "Changed outside the execution."
    model = Script([submit("The requested comparison remains conditional.")])
    runtime = AgentRuntime(
        observation_tools=[], audit_registry=AgentAuditRegistry(), model_factory=lambda _: model, logger=LOG
    )
    result = await runtime.execute(session, settings())
    assert result["status"] == "ok" and session.result is result
    messages = model.requests[0]["messages"]
    assert messages[1]["content"].startswith("Historical actor Near")
    assert messages[2] == {"role": "user", "content": "Compare Doctor now."}
    assert result["observations"] == []


@pytest.mark.asyncio
async def test_single_question_and_prepared_execution_have_identical_dispatch(monkeypatch):
    monkeypatch.setattr(AgentAuditLedger, "elapsed_seconds", property(lambda _: 0.0))
    direct = Script([submit("Conditional recommendation.")])
    prepared = Script([submit("Conditional recommendation.")])
    original = AgentRuntime(
        observation_tools=[], audit_registry=AgentAuditRegistry(), model_factory=lambda _: direct, logger=LOG
    )
    shared = AgentRuntime(
        observation_tools=[], audit_registry=AgentAuditRegistry(), model_factory=lambda _: prepared, logger=LOG
    )
    first = await original.run("Compare routes.", settings())
    second = await shared.execute(prepare("Compare routes."), settings())
    assert direct.requests == prepared.requests
    assert first["answer"] == second["answer"]
    assert first["submission_mode"] == second["submission_mode"]


@pytest.mark.asyncio
async def test_completed_execution_cannot_be_replayed_as_new_work():
    session = prepare("Read once.")
    model = Script([submit("Read completed.")])
    runtime = AgentRuntime(
        observation_tools=[], audit_registry=AgentAuditRegistry(), model_factory=lambda _: model, logger=LOG
    )
    await runtime.execute(session, settings())
    with pytest.raises(ValueError, match="fresh"):
        await runtime.execute(session, settings())
    assert len(model.requests) == 1


def test_length_edit_uses_current_question_with_historical_user_messages(monkeypatch):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    session = prepare(
        "Compare the transfer stage.", ({"role": "user", "content": "Earlier discussion of the approach stage."},)
    )
    with pytest.raises(SubmissionPatchError):
        session.submission.apply(
            {"answer": [{"text": "word " * 250}], "detail": "Preserved transfer evidence."}, references_supported=False
        )
    repair = session.length_repair_messages()
    assert repair[1] == {"role": "user", "content": "Compare the transfer stage."}
    assert session.submission.detail == "Preserved transfer evidence."


@pytest.mark.asyncio
async def test_cancelled_execution_exposes_its_result_without_an_audit_projection():
    started = asyncio.Event()

    class Waiting:
        async def complete(self, messages, tools, *, timeout):
            started.set()
            await asyncio.Event().wait()

    session = prepare("Wait for a result.")
    runtime = AgentRuntime(
        observation_tools=[], audit_registry=AgentAuditRegistry(), model_factory=lambda _: Waiting(), logger=LOG
    )
    task = asyncio.create_task(runtime.execute(session, settings()))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.result["status"] == "cancelled"
    assert runtime.snapshot()["active_count"] == 0


@pytest.mark.asyncio
async def test_preparing_execution_has_one_owner_before_first_model_request():
    preparing = asyncio.Event()
    release = asyncio.Event()

    async def setup():
        from plugin.plugins.neko_rimworld.agent.tools import ToolSetup

        preparing.set()
        await release.wait()
        return ToolSetup(())

    session = prepare("Prepare once.")
    model = Script([submit("Prepared execution completed.")])
    runtime = AgentRuntime(
        observation_tools=[],
        tools_provider=setup,
        audit_registry=AgentAuditRegistry(),
        model_factory=lambda _: model,
        logger=LOG,
    )
    first = asyncio.create_task(runtime.execute(session, settings()))
    await asyncio.wait_for(preparing.wait(), 2)
    with pytest.raises(ValueError, match="fresh"):
        await runtime.execute(session, settings())
    release.set()
    assert (await first)["status"] == "ok"
    assert len(model.requests) == 1
