from __future__ import annotations

import asyncio
import copy
import json
import threading

import pytest

from plugin.plugins.neko_rimworld.agent import audit, handoff, tokenizer, tokenizer_work
from plugin.plugins.neko_rimworld.agent import loop as agent_loop
from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger, AgentAuditRegistry
from plugin.plugins.neko_rimworld.agent.contracts import AssistantTurn, ToolCall
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft
from plugin.plugins.neko_rimworld.agent.loop import AgentRuntime
from plugin.plugins.neko_rimworld.agent.model_client import build_agent_model_settings
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession
from plugin.plugins.neko_rimworld.agent.tokenizer import TokenCounter
from plugin.plugins.neko_rimworld.reports import ReportStore

FULL = ("A saved full report with observations and qualifications. " * 3).strip()
SHORT = "Review the saved report before acting."


class QuietLogger:
    def info(self, *args):
        pass

    warning = info
    error = info


@pytest.fixture(scope="module")
def actual_counter():
    # Construct the official offline BPE before injecting worker failures.
    return tokenizer._create_counter()


class ObservedPool(tokenizer_work._TokenizerWorkPool):
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.admissions = asyncio.Queue()
        self.waiting = asyncio.Queue()
        self.completions = asyncio.Queue()
        super().__init__(max_workers=2, capacity=4)

    def _try_admit(self, waiter):
        admitted = super()._try_admit(waiter)
        (self.admissions if admitted else self.waiting).put_nowait(waiter)
        return admitted

    def _completed(self, future=None):
        super()._completed(future)
        if future is not None:
            self.loop.call_soon_threadsafe(self.completions.put_nowait, future)


class Saturation:
    def __init__(self, pool):
        self.pool = pool
        self.release = threading.Event()
        self.started = asyncio.Queue()
        self.tasks = []

    async def fill(self):
        while not self.pool.admissions.empty():
            self.pool.admissions.get_nowait()

        def block(index):
            self.pool.loop.call_soon_threadsafe(self.started.put_nowait, index)
            if not self.release.wait(5):
                raise AssertionError("test did not release its bounded workers")
            return index

        self.tasks = [asyncio.create_task(self.pool.run(block, index)) for index in range(4)]
        for _ in range(4):
            await asyncio.wait_for(self.pool.admissions.get(), 2)
        for _ in range(2):
            await asyncio.wait_for(self.started.get(), 2)
        assert self.pool.snapshot() == {"used": 4, "waiting": 0, "capacity": 4, "closed": False}

    async def close(self):
        self.release.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.to_thread(self.pool.shutdown)
        assert self.pool.snapshot()["used"] == 0


def submit_turn(agent):
    return AssistantTurn("test-response", "offline-model", "tool_calls", "", (
        ToolCall("submit-1", "submit_response", json.dumps({"full_text": FULL, "agent_text": agent})),
    ))


def setup_runtime(monkeypatch, counter, model, tmp_path):
    sessions = []
    original_create = AgentRunSession.create.__func__

    def create(cls, **kwargs):
        session = original_create(cls, **kwargs)
        sessions.append(session)
        return session

    async def acquire():
        return counter

    store = ReportStore(tmp_path / "reports")

    async def sink(record):
        return await asyncio.to_thread(store.save, record)

    monkeypatch.setattr(AgentRunSession, "create", classmethod(create))
    monkeypatch.setattr(agent_loop, "acquire_token_counter", acquire)
    monkeypatch.setattr(agent_loop, "HANDOFF_TIMEOUT_SECONDS", 0.02)
    registry = AgentAuditRegistry()
    runner = AgentRuntime(observation_tools=[], audit_registry=registry, logger=QuietLogger(),
                          model_factory=lambda _: model, report_sink=sink)
    settings = build_agent_model_settings(base_url="http://127.0.0.1:1/v1", model="offline-model", run_budget_seconds=10)
    return runner, settings, registry, sessions, store


def assert_receipt(result, code="handoff_projection_timeout"):
    assert result["status"] == "degraded"
    assert result["agent_text"] is None
    assert result["delivery"]["error_code"] == code
    assert result["delivery"]["tokenizer"] == "utf8_bytes_upper_bound"
    assert result["delivery"]["handoff_tokens"] <= 180
    assert result["summary"] == result["reply"] == handoff._MISSING_HANDOFF


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["normal", "budget", "unexpected", "capacity"])
async def test_finalization_deadline_includes_real_pool_admission(monkeypatch, tmp_path, actual_counter, exit_kind):
    pool = ObservedPool()
    saturation = Saturation(pool)
    monkeypatch.setattr(tokenizer_work, "_get_pool", lambda: pool)
    original_apply = SubmissionDraft.apply_async

    class Model:
        calls = 0

        async def complete(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return submit_turn("界" * 500 if exit_kind in {"budget", "unexpected"} else SHORT)
            if exit_kind == "unexpected":
                raise RuntimeError("controlled model failure after saved partial draft")
            await asyncio.Event().wait()

    runner, settings, registry, sessions, store = setup_runtime(monkeypatch, actual_counter, Model(), tmp_path)
    if exit_kind == "budget":
        from dataclasses import replace

        settings = replace(settings, run_budget_seconds=0.2)

    async def observed_apply(draft, *args, **kwargs):
        try:
            validated = await original_apply(draft, *args, **kwargs)
        except handoff.HandoffValidationError:
            assert draft.full_text == FULL
            await saturation.fill()
            raise
        await saturation.fill()
        if exit_kind == "capacity":
            # The next actual audit write reaches capacity after valid submit.
            monkeypatch.setattr(audit, "MAX_EVENTS_PER_RUN", len(sessions[0].ledger._events) + 1)
        return validated

    monkeypatch.setattr(SubmissionDraft, "apply_async", observed_apply)
    task = asyncio.create_task(runner.run("review", settings))
    try:
        result = await asyncio.wait_for(asyncio.shield(task), 2)
        assert_receipt(result)
        assert not saturation.release.is_set()
        assert pool.snapshot()["used"] == 4
        assert pool.snapshot()["waiting"] == 0
        assert result["answer"] == result["draft"]["full_text"] == FULL
        assert sessions[0].token_counter is actual_counter
        if exit_kind in {"budget", "unexpected"}:
            assert result["draft"]["agent_text"] is None
            assert result["draft"]["errors"]["agent_text"]["code"] == "handoff_over_budget"
        else:
            assert result["draft"]["agent_text"] == SHORT
        expected_code = {"budget": "agent_run_budget", "unexpected": "agent_internal", "capacity": "audit_capacity_reached"}.get(exit_kind)
        assert result["audit"].get("error_code") == expected_code
        report = await asyncio.to_thread(store.read, result["report"]["report_id"])
        assert report["state"] == "draft"
        assert report["full_text"] == FULL
        assert report["agent_text"] is None
        assert registry.snapshot()["active_count"] == 0
        assert registry.snapshot()["latest_run"]["result"]["delivery"]["error_code"] == "handoff_projection_timeout"
    finally:
        saturation.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await saturation.close()


@pytest.mark.asyncio
async def test_late_real_projection_cannot_replace_timeout_receipt_or_saved_report(monkeypatch, tmp_path, actual_counter):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    armed = False

    class GateEncoding:
        def encode_ordinary(self, text):
            if armed and text == SHORT and not release.is_set():
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("test did not release its real handoff projection")
            return actual_counter.encoding.encode_ordinary(text)

    selected = TokenCounter(GateEncoding())
    pool = ObservedPool()
    monkeypatch.setattr(tokenizer_work, "_get_pool", lambda: pool)
    original_apply = SubmissionDraft.apply_async

    async def observed_apply(draft, *args, **kwargs):
        nonlocal armed
        result = await original_apply(draft, *args, **kwargs)
        armed = True
        return result

    class Model:
        async def complete(self, *args, **kwargs):
            return submit_turn(SHORT)

    monkeypatch.setattr(SubmissionDraft, "apply_async", observed_apply)
    runner, settings, registry, sessions, store = setup_runtime(monkeypatch, selected, Model(), tmp_path)
    task = asyncio.create_task(runner.run("review", settings))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        result = await asyncio.wait_for(asyncio.shield(task), 2)
        assert_receipt(result)
        assert pool.snapshot()["used"] == 1
        terminal = copy.deepcopy(registry.snapshot())
        result_before = copy.deepcopy(result)
        report_before = await asyncio.to_thread(store.read, result["report"]["report_id"])
        assert sessions[0].token_counter is selected
        release.set()
        while True:
            completed = await asyncio.wait_for(pool.completions.get(), 2)
            physical = completed.result()
            if isinstance(physical, dict) and "delivery" in physical:
                break
        assert physical["status"] == "ok"
        assert physical["agent_text"] == SHORT
        assert result == result_before
        assert registry.snapshot() == terminal
        assert await asyncio.to_thread(store.read, result["report"]["report_id"]) == report_before
        assert pool.snapshot()["used"] == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(pool.shutdown)


@pytest.mark.asyncio
async def test_oversize_fallback_preserves_answer_reference_without_loop_scanning(monkeypatch, actual_counter):
    loop_thread = threading.get_ident()

    class GuardStr(str):
        def strip(self, *args, **kwargs):
            assert threading.get_ident() != loop_thread
            return super().strip(*args, **kwargs)

        def replace(self, *args, **kwargs):
            assert threading.get_ident() != loop_thread
            return super().replace(*args, **kwargs)

        def encode(self, *args, **kwargs):
            assert threading.get_ident() != loop_thread
            return super().encode(*args, **kwargs)

    pool = ObservedPool()
    saturation = Saturation(pool)
    monkeypatch.setattr(tokenizer_work, "_get_pool", lambda: pool)
    monkeypatch.setattr(agent_loop, "HANDOFF_TIMEOUT_SECONDS", 0.02)
    runner = AgentRuntime(observation_tools=[], audit_registry=AgentAuditRegistry(), logger=QuietLogger())
    ledger = AgentAuditLedger("review")
    session = AgentRunSession.create(ledger=ledger, system_prompt="test", question="review")
    session.token_counter = actual_counter
    session.submission = SubmissionDraft(full_text=FULL, agent_text=SHORT, revision=1)
    answer = GuardStr("x" * (handoff.MAX_FULL_TEXT_CHARS + 1))
    source = {"status": "ok", "answer": answer, "agent_text": SHORT, "draft": session.submission.snapshot()}
    task = None
    try:
        await saturation.fill()
        task = asyncio.create_task(runner._attach_handoff(source, session))
        runner._active_tasks[ledger.run_id] = task
        result = await asyncio.wait_for(asyncio.shield(task), 2)
        assert_receipt(result)
        assert result["answer"] is answer
        assert result["delivery"]["full_answer_tokens"] is None
        assert result["draft"] == session.submission.snapshot()
        assert session.token_counter is actual_counter
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runner._active_tasks.clear()
        await saturation.close()


@pytest.mark.asyncio
async def test_real_worker_start_failure_returns_operational_receipt_without_submit_mock(monkeypatch, tmp_path, actual_counter):
    # No worker exists yet; the failure occurs in real ThreadPoolExecutor startup.
    created = []

    def make_pool():
        pool = tokenizer_work._TokenizerWorkPool(max_workers=2, capacity=4)
        created.append(pool)
        return pool

    class Model:
        calls = 0

        async def complete(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("model exit after genuine submit worker failure")
            return submit_turn(SHORT)

    runner, settings, registry, sessions, _store = setup_runtime(monkeypatch, actual_counter, Model(), tmp_path)
    original_start = threading.Thread.start
    attempts = []

    def failed_start(thread):
        if thread.name.startswith("rimworld-tokenizer-work"):
            attempts.append(thread)
            raise RuntimeError("controlled real worker startup failure")
        return original_start(thread)

    monkeypatch.setattr(tokenizer_work, "_get_pool", make_pool)
    monkeypatch.setattr(threading.Thread, "start", failed_start)
    try:
        result = await asyncio.wait_for(runner.run("review", settings), 2)
        assert_receipt(result, "handoff_projection_unavailable")
        assert result["audit"]["error_code"] == "agent_internal"
        assert sessions[0].token_counter is actual_counter
        assert registry.snapshot()["active_count"] == 0
        assert len(attempts) >= 2
        assert all(not thread.is_alive() for thread in attempts)
    finally:
        monkeypatch.setattr(threading.Thread, "start", original_start)
        for pool in created:
            await asyncio.to_thread(pool.shutdown)
