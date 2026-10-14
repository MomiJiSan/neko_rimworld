from __future__ import annotations

import asyncio
import threading

import pytest

from plugin.plugins import neko_rimworld as facade
from plugin.plugins.neko_rimworld.agent import handoff
from plugin.plugins.neko_rimworld.agent.model_client import build_agent_model_settings
from plugin.plugins.neko_rimworld.agent.tokenizer import TokenCounter

from .test_plugin import make_plugin


class ByteEncoding:
    """Controlled metric; canonical o200k equivalence lives in helper tests."""

    def encode_ordinary(self, text):
        return list(text.encode("utf-8", "replace"))


class ShortEncoding:
    def encode_ordinary(self, text):
        return [1] * len(text.split())


class GateEncoding:
    def __init__(self, encoding):
        self.encoding = encoding
        self.loop = asyncio.get_running_loop()
        self.owner_thread = threading.get_ident()
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()
        self.operation_finished = None
        self.release = threading.Event()
        self.worker_threads = []

    def encode_ordinary(self, text):
        ident = threading.get_ident()
        self.worker_threads.append(ident)
        assert ident != self.owner_thread, "token counting ran on plugin event loop"
        self.loop.call_soon_threadsafe(self.entered.set)
        assert self.release.wait(5), "probe did not release worker"
        try:
            return self.encoding.encode_ordinary(text)
        finally:
            self.loop.call_soon_threadsafe(self.finished.set)

    def encode(self, text, **kwargs):
        return self.encode_ordinary(text)

    def observe_operation(self, handler):
        self.operation_finished = asyncio.Event()

        def observed(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            finally:
                self.loop.call_soon_threadsafe(self.operation_finished.set)

        return observed

    async def close(self):
        self.release.set()
        if self.worker_threads:
            completed = self.operation_finished or self.finished
            await asyncio.wait_for(completed.wait(), 2)


def configured_plugin():
    plugin, _ = make_plugin({})
    plugin._agent_settings = build_agent_model_settings(base_url="http://127.0.0.1:11434/v1", model="fixture")
    return plugin


@pytest.fixture
def encoding():
    return ByteEncoding()


@pytest.mark.asyncio
async def test_consult_preserves_fallback_projection_after_encoding_recovery(monkeypatch, encoding):
    fallback = TokenCounter()
    projected = handoff.attach_agent_handoff(
        {"status": "ok", "answer": "完整面板报告", "agent_text": "殖民地目前稳定。", "submission_mode": "submit_response"},
        counter=fallback,
    )
    old_metric = projected["delivery"].copy()
    warm = TokenCounter(ShortEncoding())
    assert warm.count(projected["agent_text"])[0] != old_metric["handoff_tokens"]
    monkeypatch.setattr(handoff, "get_token_counter", lambda: warm)

    async def forbidden_acquire():
        raise AssertionError("formal runtime projection must not reacquire a counter")

    monkeypatch.setattr(facade, "acquire_token_counter", forbidden_acquire)
    plugin = configured_plugin()

    class Runtime:
        async def run(self, *args, **kwargs):
            return projected

    plugin._agent_runtime = Runtime()
    result = await plugin.rimworld_consult(question="Colony status?")
    assert result.value is projected
    assert result.value["delivery"] == old_metric
    assert result.value["summary"] == "殖民地目前稳定。"


@pytest.mark.asyncio
async def test_legacy_consult_projection_is_counted_offloop_and_dashboard_advances(monkeypatch, encoding):
    gate = GateEncoding(encoding)
    counter = TokenCounter(gate)
    monkeypatch.setattr(facade, "attach_agent_handoff", gate.observe_operation(facade.attach_agent_handoff))

    async def acquire():
        return counter

    monkeypatch.setattr(facade, "acquire_token_counter", acquire)
    plugin = configured_plugin()

    class Runtime:
        async def run(self, *args, **kwargs):
            return {"status": "ok", "answer": "A literal <|endoftext|> is ordinary report text."}

        def snapshot(self, **kwargs):
            return plugin._agent_audit.snapshot(**kwargs)

    plugin._agent_runtime = Runtime()
    task = asyncio.create_task(plugin.rimworld_consult(question="Colony status?"))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not task.done()
        dashboard = await asyncio.wait_for(plugin.dashboard_ui_context(), 0.5)
        assert dashboard["configuration"]["agent_configured"]
        gate.release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.value["summary"] == "A literal <|endoftext|> is ordinary report text."
        assert result.value["delivery"]["tokenizer"] == "o200k_base"
    finally:
        await gate.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def populate_reports(plugin):
    for index in range(6):
        plugin._reports.save({"report_id": f"{index:032x}", "state": "complete", "question": "Question <|endoftext|> " + "长标题" * 24,
                              "full_text": "Saved complete report", "observations": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_report_list_counter_offloop_with_snapshot_policy_and_cancel(monkeypatch, encoding, cancel):
    gate = GateEncoding(encoding)
    counter = TokenCounter(gate)
    monkeypatch.setattr(facade, "_report_list_summary", gate.observe_operation(facade._report_list_summary))
    acquisitions = []

    async def acquire():
        acquisitions.append(counter)
        return counter

    monkeypatch.setattr(facade, "acquire_token_counter", acquire)
    plugin = configured_plugin()
    populate_reports(plugin)
    before = plugin._reports.list(0, 6)
    task = asyncio.create_task(plugin.rimworld_report(action="list", offset=0, limit=6))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not task.done()
        assert await asyncio.wait_for(plugin.dashboard_ui_context(), 0.5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gate.release.set()
            await gate.close()
        else:
            gate.release.set()
            result = await asyncio.wait_for(task, 2)
            assert result.value["reports"] == before["reports"]
            assert len(result.value["reports"]) == 6
            assert result.value["total"] == 6
            assert (await asyncio.to_thread(counter.count, result.value["summary"]))[0] <= 180
            assert "Continue list at offset" in result.value["summary"]
        assert acquisitions == [counter]
        assert plugin._reports.list(0, 6) == before
    finally:
        await gate.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
