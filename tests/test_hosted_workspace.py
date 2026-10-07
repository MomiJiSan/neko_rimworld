"""Real SDK entry, persistent SQLite and shared executor; no network or models."""

import asyncio
import copy
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime
from plugin.plugins.neko_rimworld.agent.contracts import AssistantTurn, ToolCall
from plugin.plugins.neko_rimworld.agent.handoff import count_handoff_tokens
from plugin.plugins.neko_rimworld.agent.semantic_tools import semantic_interaction_factory
from plugin.plugins.neko_rimworld.agent.tool_contracts import ToolSetup, ToolSpec
from plugin.plugins.neko_rimworld.bridge import BridgeServer
from plugin.plugins.neko_rimworld.protocol import SessionDescriptor
from plugin.plugins.neko_rimworld.query_tools import make_game_tools
from plugin.plugins.neko_rimworld.reports import ReportStore
from plugin.plugins.neko_rimworld.workspace.service import WorkspaceService
from plugin.plugins.neko_rimworld.workspace.store import WorkspaceStore
from plugin.sdk.shared.core.entry_runtime import prepare_entry_kwargs

from .test_agent import settings
from .test_capability_composition import native_observation
from .test_plugin import TOKEN, make_plugin
from .test_semantic_tools import public_source


def call(name, **args):
    return AssistantTurn("offline", "offline", "tool_calls", "", (ToolCall(name, name, json.dumps(args)),))


class Script:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def complete(self, messages, tools, *, timeout):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        return self.turns.pop(0)


async def empty_tools():
    return ToolSetup(())


def host_context(role="neko", turn="host-turn"):
    return {"lanlan_name": role, "conversation_id": turn, "run_id": turn}


def history(request):
    messages = [
        m for m in request["messages"] if "Caller-attached colony workspace history" in (m.get("content") or "")
    ]
    return json.loads(messages[0]["content"].rsplit("\n", 1)[1]) if messages else None


def configure(plugin, directory, models, provider=empty_tools):
    plugin.data_path = lambda name: directory / name
    plugin._agent_settings = settings()
    plugin._reports = ReportStore(directory / "reports")
    plugin._bridge = SimpleNamespace(
        session=SessionDescriptor("colony-A", "load-1", 10, 1), stop=lambda: True, running=True
    )
    plugin._agent_audit = AgentAuditRegistry()

    async def configured_tools():
        setup = await provider()
        if setup.directory is not None:
            return setup

        async def unused_query(_):
            raise AssertionError("Persistence fixture must not issue an unasked game query")

        from dataclasses import replace

        semantic = make_game_tools(public_source(), unused_query)
        return replace(semantic, capabilities=(*setup.capabilities, *setup.tools))

    plugin._agent_runtime = AgentRuntime(
        observation_tools=[],
        tools_provider=configured_tools,
        model_factory=lambda _: models.pop(0),
        audit_registry=plugin._agent_audit,
        logger=plugin.logger,
        report_sink=plugin._save_report,
        interaction_factory=semantic_interaction_factory,
    )

    async def export(**kwargs):
        return {"success": True}

    plugin.export_push = export


@pytest.fixture
async def plugin(tmp_path):
    value, _ = make_plugin({})
    yield value
    service = getattr(value, "_workspace_service", None)
    if service is not None:
        await service.close()


@pytest.mark.asyncio
async def test_host_entry_continues_across_distinct_turn_ids_and_preserves_reports(plugin, tmp_path):
    first = Script([call("submit_response", answer="Initial scoped conclusion.", detail="Full initial detail.")])
    second = Script(
        [
            call("workspace_history", action="list"),
            call("submit_response", answer="Continue with the changed destination."),
        ]
    )
    configure(plugin, tmp_path, [first, second])
    method = plugin.rimworld_consult

    async def invoke(question, context):
        args = prepare_entry_kwargs(
            plugin_id="neko_rimworld",
            entry_id="rimworld_consult",
            handler=method,
            meta=method.__neko_event_meta__,
            args={"question": question, "_ctx": context},
        )
        return (await method(**args)).value

    a = await invoke("Compare the original destination.", host_context(turn="one"))
    b = await invoke("The bed changed; continue.", host_context(turn="two"))
    assert a["workspace"]["workspace_id"] == b["workspace"]["workspace_id"]
    assert a["workspace"]["turn_id"] != b["workspace"]["turn_id"]
    view = history(second.requests[0])
    assert view["entries"][0]["question"] == "Compare the original destination."
    assert "Full initial detail." in view["entries"][0]["answer"]
    assert "No long-term memory" not in first.requests[0]["messages"][0]["content"]
    names = {t["function"]["name"] for t in first.requests[0]["tools"]}
    assert "submit_response" in names and "workspace_history" not in names
    assert "workspace_history" in {t["function"]["name"] for t in second.requests[0]["tools"]}
    assert a["report"]["saved"] and b["report"]["saved"]
    assert plugin._reports.read(a["report"]["report_id"])["workspace_id"] == a["workspace"]["workspace_id"]
    assert plugin._agent_audit.snapshot()["active_count"] == 0
    assert (
        await plugin._workspace_service.storage.call(
            "read", b["workspace"]["workspace_id"], "neko", b["workspace"]["turn_id"]
        )
    )["result"]["answer"] == b["answer"]
    assert len(first.requests) + len(second.requests) == 3
    (tmp_path / "hosted-dispatch.json").write_text(
        json.dumps(
            {
                "kind": "hosted-workspace-dispatch-offline-v1",
                "model_api_calls": 0,
                "native_game_runs": 0,
                "sdk_contexts": [host_context(turn="one"), host_context(turn="two")],
                "requests": [first.requests, second.requests],
                "results": [a, b],
                "quality_claim": "none; scripted clients prove dispatch and persistence only",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_profile_and_colony_isolation_use_full_identity(plugin, tmp_path):
    models = [Script([call("submit_response", answer=f"Conclusion {i}.")]) for i in range(4)]
    all_models = list(models)
    configure(plugin, tmp_path, models)
    a = (await plugin.rimworld_consult(question="Private work A", _ctx=host_context("alpha"))).value
    b = (await plugin.rimworld_consult(question="Private work B", _ctx=host_context("beta"))).value
    plugin._bridge.session = SessionDescriptor("colony-B", "load-1", 10, 1)
    c = (await plugin.rimworld_consult(question="Different colony", _ctx=host_context("alpha"))).value
    plugin._bridge.session = SessionDescriptor("colony-A", "load-2", 5, 1)
    d = (await plugin.rimworld_consult(question="Return after loading", _ctx=host_context("alpha"))).value
    assert len({v["workspace"]["workspace_id"] for v in (a, b, c)}) == 3
    assert a["workspace"]["workspace_id"] == d["workspace"]["workspace_id"]
    assert history(all_models[1].requests[0]) is None and history(all_models[2].requests[0]) is None
    assert history(all_models[3].requests[0])["entries"][0]["question"] == "Private work A"
    assert d["observations"] == [] and d["freshness"] is None


@pytest.mark.asyncio
async def test_restart_keeps_work_without_replaying_model_calls(tmp_path):
    first, _ = make_plugin({})
    model = Script([call("submit_response", answer="Retained conclusion.")])
    configure(first, tmp_path, [model])
    a = (await first.rimworld_consult(question="Before restarting", _ctx=host_context())).value
    await first.shutdown()
    assert first._workspace_service.storage.closed
    second, _ = make_plugin({})
    next_model = Script([call("submit_response", answer="Continued conclusion.")])
    configure(second, tmp_path, [next_model])
    try:
        b = (await second.rimworld_consult(question="After restarting", _ctx=host_context())).value
        assert a["workspace"]["workspace_id"] == b["workspace"]["workspace_id"]
        assert history(next_model.requests[0])["entries"][0]["question"] == "Before restarting"
        assert len(model.requests) == len(next_model.requests) == 1
    finally:
        await second._workspace_service.close()


@pytest.mark.asyncio
async def test_changed_direction_is_expressed_only_in_the_question(plugin, tmp_path):
    first = Script([call("submit_response", answer="Initial clinic findings.")])
    second = Script([call("submit_response", answer="Now follow food supplies.")])
    configure(plugin, tmp_path, [first, second])
    a = (await plugin.rimworld_consult(question="Investigate the clinic", _ctx=host_context())).value
    question = "Stop discussing the clinic; now focus on food supplies."
    b = (await plugin.rimworld_consult(question=question, _ctx=host_context())).value
    assert a["workspace"]["workspace_id"] == b["workspace"]["workspace_id"]
    assert second.requests[0]["messages"][-2]["content"] == question
    assert history(second.requests[0])["entries"][0]["question"] == "Investigate the clinic"


@pytest.mark.asyncio
async def test_historical_report_is_retrieved_inside_the_agent_from_a_question(plugin, tmp_path):
    first = Script([call("submit_response", answer="Initial answer.", detail="Original detailed investigation.")])
    configure(plugin, tmp_path, [first])
    a = (await plugin.rimworld_consult(question="Original investigation", _ctx=host_context())).value
    report_id = a["report"]["report_id"]
    next_model = Script(
        [
            call("workspace_history", action="read", report_id=report_id),
            call("submit_response", answer="Historical report analysis."),
        ]
    )
    plugin._agent_runtime._model_factory = lambda _: next_model
    result = (
        await plugin.rimworld_consult(
            question=f"Explain the original findings in report {report_id}", _ctx=host_context()
        )
    ).value
    data = next(
        json.loads(m["content"])["data"] for m in next_model.requests[1]["messages"] if m.get("name") == "workspace_history"
    )
    assert data["historical"] and not data["current_observation"]
    assert "Original detailed investigation." in data["full_text"]
    assert result["observations"] == [] and len(next_model.requests) == 2
    assert result["workspace"]["workspace_id"] == a["workspace"]["workspace_id"]


@pytest.mark.asyncio
async def test_native_surface_saves_answers_and_evidence_without_model_notes(plugin, tmp_path):
    reads = []

    async def native(args):
        reads.append(args)
        value = native_observation(args).to_dict()
        value["response"]["colony_id"] = "colony-A"
        return type(native_observation(args))(value)

    async def tools():
        return make_game_tools(public_source(), native)

    first = Script([
        call("count", object={"kind": "things"}, metric="units"),
        call("submit_response", answer="28 units sampled.", detail="Recheck the destination after changes."),
    ])
    second = Script([call("submit_response", answer="The destination remains unresolved.")])
    configure(plugin, tmp_path, [first, second], tools)
    a = (await plugin.rimworld_consult(question="Investigate current quantities", _ctx=host_context())).value
    b = (await plugin.rimworld_consult(question="What remains unresolved?", _ctx=host_context())).value
    assert a["workspace"]["checkpoint"] is None
    assert len(reads) == 1 and len(first.requests) == 2 and len(second.requests) == 1
    assert "Recheck the destination" in history(second.requests[0])["entries"][0]["answer"]
    assert b["observations"] == []
    for request in first.requests + second.requests:
        names = {t["function"]["name"] for t in request["tools"]}
        assert not names & {"saved_reports", "workspace_checkpoint", "search_mechanisms", "read_mechanism"}
        assert "workspace_checkpoint" not in request["messages"][0]["content"]
    retained = await plugin._workspace_service.storage.call(
        "read", a["workspace"]["workspace_id"], "neko", a["workspace"]["turn_id"],
        observation_id=a["observations"][0]["observation_id"],
    )
    assert retained["observation"] == a["observations"][0]


@pytest.mark.asyncio
async def test_same_work_serializes_and_the_next_call_sees_the_finished_exchange(plugin, tmp_path):
    started, proceed = asyncio.Event(), asyncio.Event()

    class Waiting(Script):
        async def complete(self, messages, tools, *, timeout):
            started.set()
            await proceed.wait()
            return await super().complete(messages, tools, timeout=timeout)

    first = Waiting([call("submit_response", answer="First conclusion.")])
    second = Script([call("submit_response", answer="Second conclusion.")])
    configure(plugin, tmp_path, [first, second])
    a = asyncio.create_task(plugin.rimworld_consult(question="First input", _ctx=host_context()))
    await asyncio.wait_for(started.wait(), 2)
    b = asyncio.create_task(plugin.rimworld_consult(question="Second input", _ctx=host_context()))
    await asyncio.sleep(0)
    assert second.requests == []
    proceed.set()
    await asyncio.gather(a, b)
    assert history(second.requests[0])["entries"][0]["question"] == "First input"


@pytest.mark.asyncio
async def test_cancelled_execution_is_settled_and_later_work_can_continue(plugin, tmp_path):
    started = asyncio.Event()

    class Waiting(Script):
        async def complete(self, messages, tools, *, timeout):
            started.set()
            await asyncio.Event().wait()

    next_model = Script([call("submit_response", answer="Continued after cancellation.")])
    configure(plugin, tmp_path, [Waiting([]), next_model])
    task = asyncio.create_task(plugin.rimworld_consult(question="Interrupted input", _ctx=host_context()))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await plugin.rimworld_consult(question="Continue later", _ctx=host_context())
    assert result.is_ok() and history(next_model.requests[0])["entries"][0]["status"] == "cancelled"
    assert plugin._agent_audit.snapshot()["active_count"] == 0


@pytest.mark.asyncio
async def test_storage_failure_does_not_silently_start_a_fresh_consultation(plugin, tmp_path, monkeypatch):
    model = Script([call("submit_response", answer="Must not dispatch")])
    configure(plugin, tmp_path, [model])

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Offline storage failure")

    monkeypatch.setattr(WorkspaceStore, "open_work", fail)
    result = await plugin.rimworld_consult(question="Follow the ongoing work", _ctx=host_context())
    assert result.is_err() and result.error.code == "workspace_unavailable"
    assert model.requests == []


def test_bridge_workspace_identity_is_unmasked_and_stale_sessions_are_unavailable():
    bridge = BridgeServer(TOKEN)
    session = SessionDescriptor("a" * 32, "b" * 32, 10, 1)
    bridge._client = object()
    bridge._session = session
    assert bridge.session == session
    bridge._session_stale = True
    assert bridge.session is None


@pytest.mark.asyncio
async def test_completed_answer_and_report_survive_unconfirmed_history_write(plugin, tmp_path, monkeypatch):
    model = Script([call("submit_response", answer="A complete scoped conclusion.", detail="Retained full findings.")])
    configure(plugin, tmp_path, [model])

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Offline full disk fixture")

    monkeypatch.setattr(WorkspaceStore, "finish", fail)
    result = (await plugin.rimworld_consult(question="Read current facts", _ctx=host_context())).value
    assert result["status"] == "ok" and "Retained full findings." in result["answer"]
    assert result["report"]["saved"] and result["workspace"]["history_saved"] is False
    assert result["summary"].startswith("History not saved.") and count_handoff_tokens(result["summary"])[0] <= 200
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_history_write_recovery_keeps_the_completed_exchange_without_rerunning_it(plugin, tmp_path, monkeypatch):
    async def native(args):
        value = native_observation(args).to_dict()
        value["response"]["colony_id"] = "colony-A"
        return type(native_observation(args))(value)

    async def tools():
        return ToolSetup((ToolSpec("read_native", "Fixture read", {}, native),))

    first = Script(
        [call("read_native"), call("submit_response", answer="Original answer.", detail="Original full findings.")]
    )
    second = Script([call("submit_response", answer="Continued after storage recovery.")])
    configure(plugin, tmp_path, [first, second], tools)
    finish = WorkspaceStore.finish

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Transient write failure")

    monkeypatch.setattr(WorkspaceStore, "finish", fail)
    a = (await plugin.rimworld_consult(question="Original investigation", _ctx=host_context())).value
    assert a["workspace"]["history_saved"] is False
    blocked = await plugin.rimworld_consult(question="Storage still unavailable", _ctx=host_context())
    assert blocked.is_err() and second.requests == []
    monkeypatch.setattr(WorkspaceStore, "finish", finish)
    b = (await plugin.rimworld_consult(question="Continue after recovery", _ctx=host_context())).value
    assert a["workspace"]["workspace_id"] == b["workspace"]["workspace_id"]
    old = history(second.requests[0])["entries"][0]
    assert old["status"] == "ok" and "Original full findings." in old["answer"]
    source = await plugin._workspace_service.storage.call(
        "read",
        a["workspace"]["workspace_id"],
        "neko",
        a["workspace"]["turn_id"],
        observation_id=a["observations"][0]["observation_id"],
    )
    assert source["observation"] == a["observations"][0]
    assert len(first.requests) == 2 and len(second.requests) == 1
    assert plugin._workspace_service.agent._pending_history == {}


@pytest.mark.asyncio
async def test_checkpoint_storage_outage_is_not_on_the_consultation_path(plugin, tmp_path, monkeypatch):
    first = Script([call("submit_response", answer="Valid answer.")])
    second = Script([call("submit_response", answer="New work still proceeds.")])
    configure(plugin, tmp_path, [first, second])

    def fail(*args, **kwargs):
        raise AssertionError("Consultation must not write optional notes")

    monkeypatch.setattr(WorkspaceStore, "save_checkpoint", fail)
    a = await plugin.rimworld_consult(question="First work", _ctx=host_context())
    b = await plugin.rimworld_consult(question="Next work", _ctx=host_context())
    assert a.is_ok() and b.is_ok()
    assert len(first.requests) == len(second.requests) == 1
    assert a.value["workspace"]["checkpoint"] is None
    assert plugin._workspace_service.agent._pending_history == {}


@pytest.mark.asyncio
async def test_offline_exchange_is_saved_and_seen_after_reconnecting(plugin, tmp_path):
    first = Script([call("submit_response", answer="Initial patient findings.")])
    offline = Script([call("submit_response", answer="Switch the ongoing investigation to power.")])
    resumed = Script([call("submit_response", answer="Continue investigating power.")])
    configure(plugin, tmp_path, [first, offline, resumed])
    a = (await plugin.rimworld_consult(question="Investigate the patient", _ctx=host_context())).value
    plugin._bridge.session = None
    b = (
        await plugin.rimworld_consult(
            question="Stop discussing the patient; now investigate power", _ctx=host_context()
        )
    ).value
    assert history(offline.requests[0])["entries"][0]["question"] == "Investigate the patient"
    assert b["workspace"]["history_saved"] and b["observations"] == []
    plugin._bridge.session = SessionDescriptor("colony-A", "load-2", 5, 1)
    c = (await plugin.rimworld_consult(question="Continue the previous investigation", _ctx=host_context())).value
    assert len({v["workspace"]["workspace_id"] for v in (a, b, c)}) == 1
    assert (
        history(resumed.requests[0])["entries"][-1]["question"] == "Stop discussing the patient; now investigate power"
    )
    assert len(first.requests) == len(offline.requests) == len(resumed.requests) == 1


@pytest.mark.asyncio
async def test_failed_cancellation_write_does_not_block_later_work(plugin, tmp_path, monkeypatch):
    started = asyncio.Event()

    class Waiting(Script):
        async def complete(self, messages, tools, *, timeout):
            started.set()
            await asyncio.Event().wait()

    next_model = Script([call("submit_response", answer="Continue after interrupted settlement.")])
    configure(plugin, tmp_path, [Waiting([]), next_model])
    abort = WorkspaceStore.abort

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Transient cancellation write failure")

    monkeypatch.setattr(WorkspaceStore, "abort", fail)
    task = asyncio.create_task(plugin.rimworld_consult(question="Interrupted work", _ctx=host_context()))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    failed = await task
    assert failed.is_err()
    monkeypatch.setattr(WorkspaceStore, "abort", abort)
    result = await plugin.rimworld_consult(question="Continue", _ctx=host_context())
    assert result.is_ok()
    assert history(next_model.requests[0])["entries"][0]["status"] == "failed"
    assert len(next_model.requests) == 1


@pytest.mark.asyncio
async def test_offline_restart_uses_each_roles_last_colony_without_crossing_roles(tmp_path):
    first, _ = make_plugin({})
    configure(first, tmp_path, [Script([call("submit_response", answer=f"Saved {i}")]) for i in range(3)])
    a = (await first.rimworld_consult(question="Alpha colony A", _ctx=host_context("alpha"))).value
    beta = (await first.rimworld_consult(question="Beta colony A", _ctx=host_context("beta"))).value
    first._bridge.session = SessionDescriptor("colony-B", "load-1", 10, 1)
    last = (await first.rimworld_consult(question="Alpha colony B", _ctx=host_context("alpha"))).value
    await first.shutdown()
    second, _ = make_plugin({})
    alpha_model = Script([call("submit_response", answer="Resume alpha B offline.")])
    beta_model = Script([call("submit_response", answer="Resume beta A offline.")])
    unknown_model = Script([call("submit_response", answer="An independent knowledge answer.")])
    configure(second, tmp_path, [alpha_model, beta_model, unknown_model])
    second._bridge.session = None
    try:
        resumed_alpha = (await second.rimworld_consult(question="Continue", _ctx=host_context("alpha"))).value
        resumed_beta = (await second.rimworld_consult(question="Continue", _ctx=host_context("beta"))).value
        unknown = (await second.rimworld_consult(question="A knowledge question", _ctx=host_context("new-role"))).value
        assert (
            resumed_alpha["workspace"]["workspace_id"]
            == last["workspace"]["workspace_id"]
            != a["workspace"]["workspace_id"]
        )
        assert resumed_beta["workspace"]["workspace_id"] == beta["workspace"]["workspace_id"]
        assert history(alpha_model.requests[0])["entries"][0]["question"] == "Alpha colony B"
        assert history(beta_model.requests[0])["entries"][0]["question"] == "Beta colony A"
        assert history(unknown_model.requests[0]) is None
        assert unknown["continuity"] == {"bound": False, "reason": "no_retained_work"}
        assert "Standalone" in unknown["summary"]
    finally:
        await second.shutdown()


@pytest.mark.asyncio
async def test_report_discovery_and_read_are_scoped_to_the_current_work(plugin, tmp_path):
    first = Script([call("submit_response", answer="Alpha's retained finding.")])
    second = Script([call("submit_response", answer="Beta's retained finding.")])
    configure(plugin, tmp_path, [first, second])
    alpha = (await plugin.rimworld_consult(question="Alpha investigation", _ctx=host_context("alpha"))).value
    beta = (await plugin.rimworld_consult(question="Beta investigation", _ctx=host_context("beta"))).value
    model = Script(
        [
            call("workspace_history", action="list"),
            call("workspace_history", action="read", report_id=beta["report"]["report_id"]),
            call("submit_response", answer="Only alpha's history is available."),
        ]
    )
    plugin._agent_runtime._model_factory = lambda _: model
    await plugin.rimworld_consult(question="Find my previous report", _ctx=host_context("alpha"))
    listing = next(
        json.loads(m["content"])["data"] for m in model.requests[1]["messages"] if m.get("name") == "workspace_history"
    )
    assert [r["report_id"] for r in listing["reports"]] == [alpha["report"]["report_id"]]
    denied = [json.loads(m["content"]) for m in model.requests[2]["messages"] if m.get("name") == "workspace_history"][-1]
    assert denied["ok"] is False and denied["error"]["code"] == "workspace_history_invalid"
    assert "Beta's retained finding" not in json.dumps(model.requests)


@pytest.mark.asyncio
async def test_database_work_does_not_block_host_dashboard(plugin, tmp_path, monkeypatch):
    started, proceed = threading.Event(), threading.Event()
    original = WorkspaceStore.open_work

    def delayed(self, *args, **kwargs):
        started.set()
        assert proceed.wait(3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(WorkspaceStore, "open_work", delayed)
    configure(plugin, tmp_path, [Script([call("submit_response", answer="Delivered")])])
    task = asyncio.create_task(plugin.rimworld_consult(question="Continue work", _ctx=host_context()))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        dashboard = await asyncio.wait_for(plugin.dashboard_ui_context(), 0.3)
        assert dashboard["agent"]["active_count"] == 0
    finally:
        proceed.set()
        await task


@pytest.mark.asyncio
async def test_cancellation_while_accepting_input_settles_the_owned_transaction(plugin, tmp_path, monkeypatch):
    started, proceed = threading.Event(), threading.Event()
    original = WorkspaceStore.accept

    def delayed(self, *args, **kwargs):
        started.set()
        assert proceed.wait(3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(WorkspaceStore, "accept", delayed)
    next_model = Script([call("submit_response", answer="Continue after cancelled acceptance.")])
    configure(plugin, tmp_path, [next_model])
    task = asyncio.create_task(plugin.rimworld_consult(question="Cancelled before execution", _ctx=host_context()))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await plugin.rimworld_consult(question="Continue again", _ctx=host_context())
    assert history(next_model.requests[0])["entries"][0]["status"] == "cancelled"
    assert len(next_model.requests) == 1


@pytest.mark.asyncio
async def test_interrupted_old_execution_is_marked_unconfirmed_without_replaying(tmp_path):
    path = tmp_path / "workspaces/work.sqlite"
    store = WorkspaceStore(path)
    workspace = store.open_work("neko", "colony-A")
    old = store.accept(workspace, "neko", "Unconfirmed previous execution")
    store.start(workspace, "neko", old)
    store.close()
    plugin, _ = make_plugin({})
    model = Script([call("submit_response", answer="The prior execution was unconfirmed.")])
    configure(plugin, tmp_path, [model])
    try:
        result = (await plugin.rimworld_consult(question="Resume later", _ctx=host_context())).value
        assert result["workspace"]["workspace_id"] == workspace
        record = await plugin._workspace_service.storage.call("read", workspace, "neko", old)
        assert record["status"] == "failed" and record["result"]["completion_unconfirmed"]
        assert record["result"]["error_code"] == "process_interrupted"
        assert len(model.requests) == 1
    finally:
        await plugin._workspace_service.close()


@pytest.mark.asyncio
async def test_long_work_uses_bounded_views_and_retrieves_the_original_exchange(plugin, tmp_path):
    first = Script([call("submit_response", answer="Original conclusion.", detail="Original full detail.")])
    configure(plugin, tmp_path, [first])
    value = (await plugin.rimworld_consult(question="Original source question", _ctx=host_context())).value
    source = value["workspace"]["turn_id"]
    old_question = "x" * 1500
    old_detail = "y" * 10_000
    for i in range(12):
        model = Script([call("submit_response", answer=f"Exchange {i}.", detail=old_detail)])
        plugin._agent_runtime._model_factory = lambda _, current=model: current
        await plugin.rimworld_consult(question=old_question, _ctx=host_context(turn=str(i)))
    model = Script(
        [
            call("workspace_history", action="read", turn_id=source),
            call("submit_response", answer="Reopened the original work."),
        ]
    )
    plugin._agent_runtime._model_factory = lambda _: model
    await plugin.rimworld_consult(question="Reopen the initial source", _ctx=host_context())
    attached = next(
        m["content"] for m in model.requests[0]["messages"] if "Caller-attached" in (m.get("content") or "")
    )
    assert len(attached) <= 12_000 and history(model.requests[0])["omitted"]
    recovered = next(
        json.loads(m["content"])["data"] for m in model.requests[1]["messages"] if m.get("name") == "workspace_history"
    )
    assert recovered["question"] == "Original source question"
    assert "Original full detail." in recovered["result"]["answer"]


@pytest.mark.asyncio
async def test_large_checkpoint_remains_retrievable_when_the_attached_view_cannot_fit(plugin, tmp_path):
    note = "n" * 4000
    questions = [str(i) + "q" * 499 for i in range(8)]
    first = Script([call("submit_response", answer="Original exchange saved.")])
    second = Script(
        [
            call("workspace_history", action="checkpoint"),
            call("submit_response", answer="The original working note remains available."),
        ]
    )
    configure(plugin, tmp_path, [first, second])
    plugin._workspace_service = WorkspaceService(tmp_path / "workspaces/work.sqlite", plugin._agent_runtime)
    workspace = await plugin._workspace_service.storage.call("open_work", "neko", "colony-A")
    await plugin._workspace_service.storage.call("set_focus", workspace, "neko", "f" * 4000)
    original = (await plugin.rimworld_consult(question="Preserve the investigation", _ctx=host_context())).value
    # Existing caller/legacy notes remain readable; the model no longer writes them.
    await plugin._workspace_service.storage.call(
        "save_checkpoint", workspace, "neko", original["workspace"]["turn_id"],
        {"work_note": note, "open_questions": questions, "anchors": []},
    )
    await plugin.rimworld_consult(question="Continue from the note", _ctx=host_context())
    view = history(second.requests[0])
    assert view["checkpoint"]["view"] == "index_only" and view["checkpoint"]["full_note_retained"]
    assert view["focus"] == "f" * 4000
    recovered = next(
        json.loads(m["content"])["data"]["checkpoint"]
        for m in second.requests[1]["messages"]
        if m.get("name") == "workspace_history"
    )
    assert recovered["data"]["work_note"] == note and recovered["data"]["open_questions"] == questions


def test_starting_new_work_is_atomic_when_route_storage_fails(tmp_path):
    store = WorkspaceStore(tmp_path / "work.sqlite")
    try:
        old = store.open_work("neko", "colony")
        store.set_work_state(old, "neko", "waiting")
        store.db.execute(
            "CREATE TRIGGER reject_route BEFORE INSERT ON active_work BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.open_work("neko", "colony", new=True)
        assert store.open_work("neko", "colony") == old
        assert store.work_state(old, "neko") == "waiting"
        assert store.db.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [False, True])
async def test_colony_switch_during_a_read_cannot_enter_the_old_work_as_current_facts(plugin, tmp_path, semantic):
    async def native(args):
        plugin._bridge.session = SessionDescriptor("colony-B", "load-2", 2, 1)
        value = native_observation(args).to_dict()
        value["response"]["colony_id"] = "colony-B"
        return type(native_observation(args))(value)

    async def tools():
        if semantic:
            return make_game_tools(public_source(), native)
        return ToolSetup((ToolSpec("read_native", "Fixture game read", {}, native),))

    model = Script([call("count", object={"kind": "things"}, metric="units") if semantic else call("read_native")])
    configure(plugin, tmp_path, [model], tools)
    result = (await plugin.rimworld_consult(question="Read the current colony", _ctx=host_context())).value
    assert result["status"] == "degraded" and result["observations"] == []
    assert "world changed" in result["summary"].lower()
    assert count_handoff_tokens(result["summary"])[0] <= 200
    assert len(model.requests) == 1
    record = await plugin._workspace_service.storage.call(
        "read", result["workspace"]["workspace_id"], "neko", result["workspace"]["turn_id"]
    )
    assert record["evidence"] == []
