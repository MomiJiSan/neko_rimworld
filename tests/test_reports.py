from __future__ import annotations

import asyncio
import json

import pytest

from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime, ModelClientError, ToolCall
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, SubmissionPatchError, count_handoff_tokens
from plugin.plugins.neko_rimworld.reports import ReportError, ReportStore

from .test_agent import FakeLogger, ScriptedModel, query_tool, settings, turn
from .test_plugin import make_plugin


def record(index=1, **extra):
    return {
        "report_id": f"{index:032x}",
        "state": "complete",
        "question": "Question",
        "full_text": "A full historical report.",
        "observations": [],
        **extra,
    }


def test_patch_keeps_detail_and_retries_summary_with_explicit_confirmation():
    draft = SubmissionDraft()
    with pytest.raises(SubmissionPatchError) as caught:
        draft.apply({"detail": "Detailed original", "answer": [{"text": "超限内容。" * 300}]})
    assert caught.value.details["saved_fields"] == ["answer", "detail"]
    assert caught.value.details["required_fields"] == ["answer"]
    with pytest.raises(SubmissionPatchError) as confirmation:
        draft.apply({"answer": [{"text": "Brief"}]})
    assert confirmation.value.details["required_fields"] == ["detail"]
    result = draft.apply({"detail": "Detailed original"})
    assert result["full_text"] == "Brief\n\nDetailed original"
    assert draft.snapshot()["complete"]


def test_invalid_replacements_preserve_valid_inputs_and_clearing_is_explicit():
    draft = SubmissionDraft()
    assert draft.apply({"answer": [{"text": "Brief"}], "detail": "Original"})["agent_text"] == "Brief"
    with pytest.raises(SubmissionPatchError):
        draft.apply({"detail": 7})
    assert draft.detail == "Original"
    with pytest.raises(SubmissionPatchError):
        draft.apply({"answer": [{"text": "New brief"}]})
    assert draft.answer == [{"text": "New brief"}] and draft.detail == "Original"
    result = draft.apply({"detail": None})
    assert result["full_text"] == result["agent_text"] == "New brief"
    with pytest.raises(SubmissionPatchError):
        draft.apply({"full_text": "old alias"})
    with pytest.raises(SubmissionPatchError):
        draft.apply({})


def test_report_store_survives_restart_is_immutable_and_bounded(tmp_path):
    store = ReportStore(tmp_path, capacity=2)
    store.save(record(1, created_at="2026-01-01"))
    store.save(record(1, full_text="Overwrite attempt"))
    assert ReportStore(tmp_path).read(f"{1:032x}")["full_text"] == "A full historical report."
    store.save(record(2, created_at="2026-01-02"))
    store.save(record(3, created_at="2026-01-03"))
    assert store.list()["total"] == 2
    with pytest.raises(ReportError):
        store.read(f"{1:032x}")
    with pytest.raises(ReportError):
        store.read("../secrets")
    (tmp_path / ("a" * 32 + ".json")).write_text("not json")
    assert ReportStore(tmp_path).list()["total"] == 2


def test_large_evidence_does_not_discard_report_body():
    store = ReportStore()
    saved = store.save(record(observations=[{"response": {"game_tick": i}, "data": "x" * 100000} for i in range(12)]))
    full = store.read(saved["report_id"])
    assert full["full_text"] == "A full historical report."
    assert full["evidence_status"] == "omitted_size_limit"
    assert full["observation_count"] == 12
    assert full["samples"][-1]["game_tick"] == 11
    assert full["observations"] == []


async def unused(_args):
    raise AssertionError("Unexpected game query")


def runtime_for(model, store):
    async def sink(value):
        return store.save(value)

    return AgentRuntime(
        observation_tools=[query_tool(unused)],
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        report_sink=sink,
    )


@pytest.mark.asyncio
async def test_runtime_saves_long_plain_body_then_accepts_short_plain_repair():
    body = "完整报告与调查细节。" * 100
    model = ScriptedModel([turn(body), turn("简短交接。")])
    store = ReportStore()
    result = await runtime_for(model, store).run("Detailed report", settings())
    assert result["answer"] == "简短交接。\n\n" + body
    assert result["summary"] == "简短交接。"
    assert result["delivery"]["submission_mode"] == "text_repair"
    assert store.read(result["report"]["report_id"])["state"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [ModelClientError("test_failure", "Model failed"), RuntimeError("unexpected"), TimeoutError()]
)
async def test_failed_correction_retains_draft_as_draft(error):
    body = "已完成的调查。" * 100
    model = ScriptedModel([turn("", ToolCall("1", "submit_response", json.dumps({"detail": body}))), error])
    store = ReportStore()
    runtime = runtime_for(model, store)
    result = await runtime.run("Research", settings())
    assert result["answer"] == body
    assert result["delivery"]["submission_mode"] == "operational_fallback"
    saved = store.read(result["report"]["report_id"])
    assert saved["state"] == "draft"
    assert saved["full_text"] == body
    assert saved["agent_text"] is None


@pytest.mark.asyncio
async def test_cancel_after_draft_preserves_saved_body():
    blocked = asyncio.Event()

    class Model:
        count = 0

        async def complete(self, *args, **kwargs):
            self.count += 1
            if self.count == 1:
                return turn("", ToolCall("1", "submit_response", json.dumps({"detail": "Saved draft"})))
            blocked.set()
            await asyncio.Event().wait()

    store = ReportStore()
    runtime = runtime_for(Model(), store)
    task = asyncio.create_task(runtime.run("Research", settings()))
    await asyncio.wait_for(blocked.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = store.read(store.list()["reports"][0]["report_id"])
    assert saved["state"] == "draft"
    assert saved["full_text"] == "Saved draft"


@pytest.mark.asyncio
async def test_explicit_reference_and_evidence_are_historical_not_live():
    reference = record(
        created_at="2026-01-01", observations=[{"request": {"operation": "old query"}, "response": {"game_tick": 12}}]
    )
    model = ScriptedModel(
        [turn("", ToolCall("1", "read_report_evidence", '{"index":0}')), turn("Historical explanation")]
    )
    store = ReportStore()
    result = await runtime_for(model, store).run("Explain old report", settings(), reference_report=reference)
    assert "A full historical report." in str(model.requests[0])
    assert "historical" in str(model.requests[1])
    assert result["observations"] == []
    assert result["freshness"] is None
    assert store.read(result["report"]["report_id"])["reference_report_id"] == reference["report_id"]
    fresh = ScriptedModel([turn("Fresh")])
    await runtime_for(fresh, store).run("Fresh", settings())
    assert "A full historical report." not in str(fresh.requests[0])


@pytest.mark.asyncio
async def test_report_entry_full_retrieval_short_projection_and_export_failure():
    from utils.result_parser import parse_plugin_result

    plugin, _ = make_plugin({})

    async def failing_export(**kwargs):
        raise RuntimeError("host export offline")

    plugin.export_push = failing_export
    for i in range(20):
        plugin._reports.save(record(i))
    listed = (await plugin.rimworld_report(action="list", limit=20)).value
    assert count_handoff_tokens(listed["summary"])[0] <= 180
    result = (await plugin.rimworld_report(action="read", report_id=f"{1:032x}", _ctx={"run_id": "host-run"})).value
    assert result["report"]["full_text"] == "A full historical report."
    assert result["export"]["status"] == "unavailable"
    projected = parse_plugin_result(result, llm_result_fields=["summary"], plugin_message="done")
    assert "A full historical report." not in projected
    assert f"{1:032x}" in projected
    assert count_handoff_tokens(projected)[0] <= 180


@pytest.mark.asyncio
async def test_report_storage_failure_keeps_complete_result():
    async def fail_save(_record):
        raise OSError("Storage unavailable")

    model = ScriptedModel([turn("Complete answer")])
    runtime = AgentRuntime(
        observation_tools=[query_tool(unused)],
        audit_registry=AgentAuditRegistry(),
        logger=FakeLogger(),
        model_factory=lambda _: model,
        report_sink=fail_save,
    )
    result = await runtime.run("Research", settings())
    assert result["status"] == "ok"
    assert result["answer"] == result["summary"] == "Complete answer"
    assert result["report"]["saved"] is False


@pytest.mark.asyncio
async def test_public_consult_requires_exact_explicit_reference():
    plugin, _ = make_plugin({})
    plugin._agent_settings = settings()
    plugin._reports.save(record())
    received = []

    class Runtime:
        async def run(self, question, model_settings, **kwargs):
            received.append(kwargs)
            return {"status": "ok", "answer": "Answer"}

    plugin._agent_runtime = Runtime()
    await plugin.rimworld_consult(question="New question")
    assert received[-1] == {}
    await plugin.rimworld_consult(question="Old report detail", report_id=f"{1:032x}")
    assert received[-1]["reference_report"]["full_text"] == "A full historical report."
    failed = await plugin.rimworld_consult(question="Missing report", report_id=f"{42:032x}")
    assert failed.is_err()
    assert len(received) == 2


@pytest.mark.asyncio
async def test_successful_native_export_contains_unshortened_body_and_silent_delivery():
    plugin, _ = make_plugin({})
    body = "报告内容。" * 400
    plugin._reports.save(record(full_text=body))
    sent = []

    async def export(**kwargs):
        sent.append(kwargs)
        return {"export_id": "native-artifact"}

    plugin.export_push = export
    result = (await plugin.rimworld_report(action="read", report_id=f"{1:032x}", _ctx={"run_id": "host-run"})).value
    assert result["export"]["status"] == "exported"
    assert body in sent[0]["text"]
    assert sent[0]["delivery"] == "silent"
    assert sent[0]["run_id"] == "host-run"
