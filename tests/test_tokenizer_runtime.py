from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from plugin.plugins.neko_rimworld.agent import handoff
from plugin.plugins.neko_rimworld.agent import loop as agent_loop
from plugin.plugins.neko_rimworld.agent.audit import AgentAuditRegistry
from plugin.plugins.neko_rimworld.agent.contracts import AssistantTurn, ToolCall
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, SubmissionPatchError
from plugin.plugins.neko_rimworld.agent.loop import AgentRuntime
from plugin.plugins.neko_rimworld.agent.model_client import build_agent_model_settings
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession
from plugin.plugins.neko_rimworld.agent.tokenizer import TokenCounter


class WordEncoding:
    def encode(self, text: str, *, disallowed_special=()) -> list[int]:
        assert disallowed_special == ()
        return self.encode_ordinary(text)

    def encode_ordinary(self, text: str) -> list[int]:
        return [1] * len(text.split())


class ApplyGate(WordEncoding):
    """Pause actual clone.apply after its full_text mutation, not a mocked apply."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, text: str = "new agent") -> None:
        self.loop = asyncio.get_running_loop()
        self.text = text
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = threading.Event()
        self.clone: SubmissionDraft | None = None
        self.worker_thread: int | None = None
        original = SubmissionDraft.apply

        def observed_apply(draft: SubmissionDraft, *args, **kwargs):
            if threading.get_ident() != self.loop_thread:
                self.clone = draft
                self.worker_thread = threading.get_ident()
                try:
                    return original(draft, *args, **kwargs)
                finally:
                    self.loop.call_soon_threadsafe(self.finished.set)
            return original(draft, *args, **kwargs)

        self.loop_thread = threading.get_ident()
        monkeypatch.setattr(SubmissionDraft, "apply", observed_apply)

    def encode_ordinary(self, text: str) -> list[int]:
        if text == self.text and not self.release.is_set():
            self.loop.call_soon_threadsafe(self.entered.set)
            if not self.release.wait(5):
                raise AssertionError("test did not release its tokenizer worker")
        return super().encode_ordinary(text)

    async def cleanup(self) -> None:
        self.release.set()
        if self.clone is not None:
            await asyncio.wait_for(self.finished.wait(), 2)


def initial_draft() -> SubmissionDraft:
    return SubmissionDraft(full_text="old report", agent_text="old agent", revision=2)


@pytest.mark.asyncio
async def test_cancelled_apply_cannot_write_live_draft_after_worker_finishes(monkeypatch) -> None:
    draft = initial_draft()
    draft.errors["full_text"] = {"code": "prior_error", "message": "retained error"}
    before = draft.snapshot()
    gate = ApplyGate(monkeypatch)
    task = asyncio.create_task(draft.apply_async({"full_text": "new report", "agent_text": "new agent"}, counter=TokenCounter(gate)))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert gate.clone is not draft
        assert gate.worker_thread != gate.loop_thread
        assert gate.clone.full_text == "new report"
        assert gate.clone.errors is not draft.errors
        assert draft.snapshot() == before
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert draft.snapshot() == before
        await gate.cleanup()
        assert gate.clone.agent_text == "new agent"
        assert draft.snapshot() == before
    finally:
        await gate.cleanup()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_errors_only_change_rejects_candidate_despite_unchanged_revision(monkeypatch) -> None:
    draft = initial_draft()
    draft.errors["full_text"] = {"code": "prior_error", "message": "original nested message"}
    gate = ApplyGate(monkeypatch)
    task = asyncio.create_task(draft.apply_async({"full_text": "new report", "agent_text": "new agent"}, counter=TokenCounter(gate)))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        revision = draft.revision
        draft.errors["full_text"]["message"] = "newer nested message"
        expected = draft.snapshot()
        assert draft.revision == revision
        gate.release.set()
        with pytest.raises(SubmissionPatchError) as caught:
            await task
        assert caught.value.code == "submission_superseded"
        assert draft.snapshot() == expected
        assert gate.clone.full_text == "new report"
    finally:
        await gate.cleanup()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_value_sync_takeover_invalidates_old_async_operation(monkeypatch) -> None:
    draft = initial_draft()
    before = draft.snapshot()
    gate = ApplyGate(monkeypatch)
    task = asyncio.create_task(draft.apply_async({"full_text": "new report", "agent_text": "new agent"}, counter=TokenCounter(gate)))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        draft.apply({"full_text": "old report", "agent_text": "old agent"}, counter=TokenCounter(WordEncoding()))
        assert draft.snapshot() == before
        gate.release.set()
        with pytest.raises(SubmissionPatchError) as caught:
            await task
        assert caught.value.code == "submission_superseded"
        assert draft.snapshot() == before
    finally:
        await gate.cleanup()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_async_partial_rejection_retains_valid_full_text_and_can_be_repaired() -> None:
    draft = SubmissionDraft()
    counter = TokenCounter(WordEncoding())
    with pytest.raises(SubmissionPatchError) as caught:
        await draft.apply_async({"full_text": "retained report", "agent_text": "word " * 181}, counter=counter)
    assert caught.value.code == "handoff_over_budget"
    assert draft.full_text == "retained report"
    assert draft.agent_text is None
    assert draft.revision == 1
    assert caught.value.details["saved_fields"] == ["full_text"]
    repaired = await draft.apply_async({"agent_text": "fixed agent text"}, counter=counter)
    assert repaired["full_text"] == "retained report"
    assert repaired["agent_text"] == "fixed agent text"
    assert repaired["delivery"]["handoff_tokens"] == 3
    assert draft.errors == {}


class QuietLogger:
    def info(self, *args) -> None:
        pass

    warning = info
    error = info


class Model:
    def __init__(self, turns: list[AssistantTurn | Exception]) -> None:
        self.turns = turns
        self.requests: list[list[dict]] = []

    async def complete(self, messages, tools, *, timeout) -> AssistantTurn:
        self.requests.append(messages)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def submit_turn(full: str, agent: str) -> AssistantTurn:
    return AssistantTurn("test-response", "test-model", "tool_calls", "", (
        ToolCall("submit-1", "submit_response", json.dumps({"full_text": full, "agent_text": agent})),
    ))


def runtime(model: Model, registry: AgentAuditRegistry, report_sink=None) -> AgentRuntime:
    return AgentRuntime(observation_tools=[], audit_registry=registry, logger=QuietLogger(), model_factory=lambda _: model, report_sink=report_sink)


def settings():
    return build_agent_model_settings(base_url="http://127.0.0.1:1/v1", model="offline-model", run_budget_seconds=10)


@pytest.mark.asyncio
async def test_runtime_uses_one_fallback_counter_for_repair_budget_and_final_projection(monkeypatch) -> None:
    selected = TokenCounter(None)
    acquires = 0
    seen: list[tuple[int, object]] = []
    loop_thread = threading.get_ident()
    original = handoff.count_handoff_tokens

    async def acquire():
        nonlocal acquires
        acquires += 1
        assert acquires == 1, "the same run must not select a new tokenizer after its draft was validated"
        return selected

    def counted(text, *, counter=None):
        seen.append((threading.get_ident(), counter))
        return original(text, counter=counter)

    monkeypatch.setattr(agent_loop, "acquire_token_counter", acquire)
    monkeypatch.setattr(handoff, "count_handoff_tokens", counted)
    monkeypatch.setattr(agent_loop, "count_handoff_tokens", counted)
    model = Model([submit_turn("full report", "界" * 61), AssistantTurn("repair", "test-model", "stop", "fixed text")])
    registry = AgentAuditRegistry()
    result = await runtime(model, registry).run("review", settings())
    assert acquires == 1
    assert result["answer"] == "full report"
    assert result["agent_text"] == "fixed text"
    assert result["delivery"]["tokenizer"] == "utf8_bytes_upper_bound"
    assert result["delivery"]["handoff_tokens"] == len(b"fixed text")
    assert result["delivery"]["submission_mode"] == "text_repair"
    assert seen and all(counter is selected for _, counter in seen)
    assert all(thread != loop_thread for thread, _ in seen)
    events = registry.snapshot()["latest_run"]["events"]
    rejected = next(event for event in events if event["event"] == "submission_patch_rejected")
    assert rejected["data"]["saved_fields"] == ["full_text"]


@pytest.mark.asyncio
async def test_runtime_cancellation_retires_run_before_late_clone_finishes(monkeypatch) -> None:
    gate = ApplyGate(monkeypatch)
    counter = TokenCounter(gate)
    sessions: list[AgentRunSession] = []
    reports: list[dict] = []
    original_create = AgentRunSession.create.__func__

    def create(cls, **kwargs):
        session = original_create(cls, **kwargs)
        session.submission = initial_draft()
        sessions.append(session)
        return session

    async def acquire():
        return counter

    async def report_sink(record):
        reports.append(record)
        return {"report_id": record["report_id"]}

    monkeypatch.setattr(AgentRunSession, "create", classmethod(create))
    monkeypatch.setattr(agent_loop, "acquire_token_counter", acquire)
    registry = AgentAuditRegistry()
    runner = runtime(Model([submit_turn("new report", "new agent")]), registry, report_sink)
    task = asyncio.create_task(runner.run("review", settings()))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        before = sessions[0].submission.snapshot()
        assert before["full_text"] == "old report"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        retired = registry.snapshot()
        assert retired["active_count"] == 0
        assert retired["latest_run"]["state"] == "cancelled"
        assert reports[0]["full_text"] == "old report"
        assert sessions[0].token_counter is counter
        await gate.cleanup()
        assert sessions[0].submission.snapshot() == before
        assert registry.snapshot() == retired
        assert len(reports) == 1
    finally:
        await gate.cleanup()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_during_real_handoff_worker_publishes_cancelled_terminal_result(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    current = threading.local()

    class HandoffEncoding(WordEncoding):
        def encode_ordinary(self, text):
            if text == "final agent" and getattr(current, "attaching", False):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("test did not release its handoff worker")
            return super().encode_ordinary(text)

    counter = TokenCounter(HandoffEncoding())
    original_attach = agent_loop.attach_agent_handoff

    def attach(result, *, counter):
        current.attaching = True
        normal = result.get("status") == "ok"
        try:
            return original_attach(result, counter=counter)
        finally:
            if normal:
                loop.call_soon_threadsafe(finished.set)
            current.attaching = False

    async def acquire():
        return counter

    monkeypatch.setattr(agent_loop, "acquire_token_counter", acquire)
    monkeypatch.setattr(agent_loop, "attach_agent_handoff", attach)
    registry = AgentAuditRegistry()
    runner = runtime(Model([submit_turn("final report", "final agent")]), registry)
    task = asyncio.create_task(runner.run("review", settings()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert registry.snapshot()["latest_run"]["state"] == "finalizing"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        terminal = registry.snapshot()
        assert terminal["active_count"] == 0
        assert terminal["latest_run"]["state"] == "cancelled"
        assert terminal["latest_run"]["result"]["status"] == "cancelled"
        assert not any(event["event"] == "run_completed" for event in terminal["latest_run"]["events"])
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert registry.snapshot() == terminal
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if entered.is_set():
            await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["capacity", "unexpected_error"])
async def test_cancellation_during_failure_exit_handoff_does_not_retire_without_result(monkeypatch, exit_kind) -> None:
    from plugin.plugins.neko_rimworld.agent import audit

    if exit_kind == "capacity":
        monkeypatch.setattr(audit, "MAX_EVENTS_PER_RUN", 3)
    error_code = "audit_capacity_reached" if exit_kind == "capacity" else "agent_internal"
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    current = threading.local()

    class CapacityEncoding(WordEncoding):
        def encode_ordinary(self, text):
            if getattr(current, "capacity", False) and not release.is_set():
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("test did not release its capacity projection worker")
            return super().encode_ordinary(text)

    counter = TokenCounter(CapacityEncoding())
    original_attach = agent_loop.attach_agent_handoff

    def attach(result, *, counter):
        current.capacity = result.get("audit", {}).get("error_code") == error_code and result.get("status") != "cancelled"
        gated = current.capacity
        try:
            return original_attach(result, counter=counter)
        finally:
            if gated:
                loop.call_soon_threadsafe(finished.set)
            current.capacity = False

    async def acquire():
        return counter

    monkeypatch.setattr(agent_loop, "acquire_token_counter", acquire)
    monkeypatch.setattr(agent_loop, "attach_agent_handoff", attach)
    registry = AgentAuditRegistry()
    model = Model([AssistantTurn("final", "test-model", "stop", "safe answer") if exit_kind == "capacity" else RuntimeError("controlled unexpected model error")])
    task = asyncio.create_task(runtime(model, registry).run("review", settings()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        terminal = registry.snapshot()
        assert terminal["active_count"] == 0
        assert terminal["latest_run"]["result"] is not None
        assert terminal["latest_run"]["result"]["status"] == "cancelled"
        assert terminal["latest_run"]["result"]["delivery"]["tokenizer"] == "o200k_base"
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert registry.snapshot() == terminal
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if entered.is_set():
            await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.asyncio
async def test_repeated_cancel_during_cancel_projection_keeps_terminal_result(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    current = threading.local()

    class WaitingModel:
        async def complete(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

    class CancellationEncoding(WordEncoding):
        def encode_ordinary(self, text):
            if getattr(current, "cancelled", False) and not release.is_set():
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("test did not release its cancellation projection worker")
            return super().encode_ordinary(text)

    selected = TokenCounter(CancellationEncoding())
    original_attach = agent_loop.attach_agent_handoff

    def attach(result, *, counter):
        current.cancelled = result.get("status") == "cancelled"
        try:
            return original_attach(result, counter=counter)
        finally:
            if current.cancelled:
                loop.call_soon_threadsafe(finished.set)
            current.cancelled = False

    async def acquire():
        return selected

    monkeypatch.setattr(agent_loop, "acquire_token_counter", acquire)
    monkeypatch.setattr(agent_loop, "attach_agent_handoff", attach)
    registry = AgentAuditRegistry()
    task = asyncio.create_task(runtime(WaitingModel(), registry).run("review", settings()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        terminal = registry.snapshot()
        assert terminal["active_count"] == 0
        assert terminal["latest_run"]["state"] == "cancelled"
        assert terminal["latest_run"]["result"] is not None
        assert terminal["latest_run"]["result"]["status"] == "cancelled"
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert registry.snapshot() == terminal
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if entered.is_set():
            await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.asyncio
async def test_run_deadline_does_not_wait_for_or_publish_late_submission_worker(monkeypatch) -> None:
    gate = ApplyGate(monkeypatch)
    counter = TokenCounter(gate)
    sessions: list[AgentRunSession] = []
    original_create = AgentRunSession.create.__func__

    def create(cls, **kwargs):
        session = original_create(cls, **kwargs)
        session.submission = initial_draft()
        sessions.append(session)
        return session

    async def acquire():
        return counter

    monkeypatch.setattr(AgentRunSession, "create", classmethod(create))
    monkeypatch.setattr(agent_loop, "acquire_token_counter", acquire)
    registry = AgentAuditRegistry()
    runner = runtime(Model([submit_turn("new report", "new agent")]), registry)
    deadline = replace(settings(), run_budget_seconds=0.05, finalization_reserve_seconds=0)
    task = asyncio.create_task(runner.run("review", deadline))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        before = sessions[0].submission.snapshot()
        result = await asyncio.wait_for(task, 2)
        assert not gate.release.is_set(), "the deadline must finish while the detached worker remains blocked"
        assert result["status"] == "degraded"
        assert result["audit"]["error_code"] == "agent_run_budget"
        assert result["answer"] == "old report"
        assert result["delivery"]["tokenizer"] == "o200k_base"
        assert registry.snapshot()["latest_run"]["state"] == "completed"
        terminal = registry.snapshot()
        await gate.cleanup()
        assert sessions[0].submission.snapshot() == before
        assert registry.snapshot() == terminal
    finally:
        await gate.cleanup()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    ("repair_kind", "repair", "expected_calls", "parsed_length"),
    [
        ("normal", "Brief repair.", 2, 13),
        ("leading_padding", " " * 4001 + "Brief repair.", 2, 13),
        ("internal_spacing", "A" + " " * 4500 + "B", 3, 4502),
    ],
    ids=["normal", "leading_padding", "internal_spacing"],
)
@pytest.mark.asyncio
async def test_real_plaintext_repair_preserves_saved_full_report(
    tmp_path, repair_kind, repair, expected_calls, parsed_length,
) -> None:
    """A rejected agent-only repair must not become a replacement full report.

    Use the real o200k encoder, streaming client/parser, runtime and report store;
    only the model's HTTP boundary is controlled. Leading whitespace is a
    negative control because the real model adapter strips it before runtime.
    """
    import httpx

    from plugin.plugins.neko_rimworld.agent.model_client import OpenAICompatibleModelClient
    from plugin.plugins.neko_rimworld.agent.tokenizer import acquire_token_counter
    from plugin.plugins.neko_rimworld.reports import ReportStore

    pytest.importorskip("tiktoken")
    counter = await acquire_token_counter()
    assert counter.encoding is not None
    full = "Original detailed report: the colony has3colonists,2injuries, and medicine reserved for emergencies. Preserve these details in the panel."
    short = "Brief repair."
    assert len(full) == 137
    assert counter.count("界" * 500)[0] > 180
    if repair_kind == "internal_spacing":
        assert len(repair) > handoff.MAX_AGENT_TEXT_CHARS
        assert counter.count(repair)[0] == 38
    turns = [{"full_text": full, "agent_text": "界" * 500}, repair]
    if repair_kind == "internal_spacing":
        turns.append(short)
    requests: list[dict] = []
    parsed: list[str] = []

    async def response(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        requests.append(json.loads(request.content))
        assert turns, "unexpected model recovery request"
        turn = turns.pop(0)
        if isinstance(turn, dict):
            delta = {"tool_calls": [{
                "index": 0, "id": "partial-submit", "type": "function",
                "function": {"name": "submit_response", "arguments": json.dumps(turn)},
            }]}
            finish = "tool_calls"
        else:
            delta = {"content": turn}
            finish = "stop"
        chunks = [
            {"id": "controlled", "model": "offline-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "controlled", "model": "offline-model", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    class RecordedModel(OpenAICompatibleModelClient):
        async def complete(self, messages, tools, *, timeout):
            turn = await super().complete(messages, tools, timeout=timeout)
            parsed.append(turn.content)
            return turn

    configured = settings()
    model = RecordedModel(configured, transport=httpx.MockTransport(response))
    registry = AgentAuditRegistry()
    store = ReportStore(tmp_path / "reports")

    async def save(record):
        return await asyncio.to_thread(store.save, record)

    result = await runtime(model, registry, save).run("Keep the full report; repair only its handoff", configured)
    assert result["status"] == "ok"
    assert len(requests) == expected_calls
    assert len(parsed[1]) == parsed_length
    events = registry.snapshot()["latest_run"]["events"]
    initial = next(event["data"] for event in events if event["event"] == "submission_patch_rejected")
    assert initial["saved_fields"] == ["full_text"]
    assert initial["field_errors"]["agent_text"]["code"] == "handoff_over_budget"
    if repair_kind == "internal_spacing":
        rejected = [event["data"] for event in events if event["event"] == "final_text_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["error_code"] == "submission_too_large"
    assert result["answer"] == full
    assert result["draft"]["full_text"] == full
    assert result["agent_text"] == short
    assert result["delivery"]["submission_mode"] == "text_repair"
    assert result["delivery"]["tokenizer"] == "o200k_base"
    record = await asyncio.to_thread(store.read, result["report"]["report_id"])
    assert record["full_text"] == full
    assert record["agent_text"] == short
    assert record["state"] == "complete"
