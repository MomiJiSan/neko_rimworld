"""Hosted work reads survive restarts and never dispatch model/game calls."""

import asyncio
import hashlib
import sqlite3

import pytest

from ..workspace.store import WorkspaceStore
from ..workspace.views import WorkspaceViews
from .test_hosted_workspace import Script, call, configure, host_context
from .test_plugin import make_plugin


@pytest.fixture
async def plugin():
    value, _ = make_plugin({})
    yield value
    await value.shutdown()


def test_empty_view_does_not_create_database(tmp_path):
    path = tmp_path / "absent/work.sqlite"
    assert WorkspaceViews(path).read("list")["workspaces"] == []
    assert not path.parent.exists()


@pytest.mark.asyncio
async def test_work_history_handoff_and_audit_survive_restart_without_replaying(tmp_path, monkeypatch):
    first, _ = make_plugin({})
    model = Script([call("submit_response", answer="Original answer.", detail="Original full detail.")])
    configure(first, tmp_path, [model])
    result = (await first.rimworld_consult(question="Original question", _ctx=host_context("alpha"))).value
    await first.shutdown()
    second, _ = make_plugin({})
    configure(second, tmp_path, [])
    second._bridge.session = None
    path = tmp_path / "workspaces/work.sqlite"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    listing = (await second.workspace_view()).value
    assert listing["workspaces"][0]["owner"] == "alpha"
    assert not listing["workspaces"][0]["current_colony"]
    wid, tid = result["workspace"]["workspace_id"], result["workspace"]["turn_id"]
    work = (await second.workspace_view(action="work", workspace_id=wid)).value
    turn = (await second.workspace_view(action="turn", workspace_id=wid, turn_id=tid)).value
    audit = (await second.workspace_view(action="audit", workspace_id=wid, turn_id=tid, limit=1)).value
    assert work["turns"][0]["question"] == "Original question"
    assert turn["history_saved"] and "Original full detail." in turn["result"]["answer"]
    assert turn["handoff"]["summary"] == result["summary"]
    assert "not a receipt" in turn["handoff"]["meaning"]
    assert audit["workspace_id"] == wid and audit["turn_id"] == tid and audit["events_page"]["has_more"]
    assert second._workspace_service is None and len(model.requests) == 1

    def unreadable(_report_id):
        raise OSError("Offline report read failure")

    monkeypatch.setattr(second._reports, "read", unreadable)
    retained = (await second.workspace_view(action="turn", workspace_id=wid, turn_id=tid)).value
    assert retained["report_unavailable"] and "Original full detail." in retained["result"]["answer"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    await second.shutdown()


@pytest.mark.asyncio
async def test_reading_unconfirmed_turn_does_not_retry_or_recover_it(plugin, tmp_path, monkeypatch):
    model = Script([call("submit_response", answer="Complete answer.")])
    configure(plugin, tmp_path, [model])

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Offline failed write")

    monkeypatch.setattr(WorkspaceStore, "finish", fail)
    result = (await plugin.rimworld_consult(question="Investigate", _ctx=host_context())).value
    wid, tid = result["workspace"]["workspace_id"], result["workspace"]["turn_id"]
    view = (await plugin.workspace_view(action="turn", workspace_id=wid, turn_id=tid)).value
    assert view["result"]["answer"] == "Complete answer." and not view["history_saved"]
    assert view["status"] == "running" and view["active"] is None
    assert len(model.requests) == 1
    assert (await plugin._workspace_service.storage.call("turn", wid, "neko", tid))["status"] == "running"


@pytest.mark.asyncio
async def test_current_execution_is_bound_to_its_work_and_full_entry_lifetime(plugin, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Waiting(Script):
        async def complete(self, messages, tools, *, timeout):
            started.set()
            await release.wait()
            return await super().complete(messages, tools, timeout=timeout)

    model = Waiting([call("submit_response", answer="Completed.")])
    configure(plugin, tmp_path, [model])
    exporting, exported = asyncio.Event(), asyncio.Event()

    async def export(**kwargs):
        exporting.set()
        await exported.wait()
        return {"success": True}

    plugin.export_push = export
    task = asyncio.create_task(plugin.rimworld_consult(question="Active question", _ctx=host_context()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        listing = (await plugin.workspace_view()).value
        row = listing["workspaces"][0]
        assert row["active"]["workspace_id"] == row["workspace_id"]
        assert row["active"]["turn_id"]
        release.set()
        await asyncio.wait_for(exporting.wait(), 2)
        assert (await plugin.dashboard_ui_context())["consultations_in_progress"] == 1
        exported.set()
        result = (await task).value
        assert (await plugin.dashboard_ui_context())["consultations_in_progress"] == 0
        view = (
            await plugin.workspace_view(
                action="turn", workspace_id=row["workspace_id"], turn_id=result["workspace"]["turn_id"]
            )
        ).value
        assert view["handoff"]["report"]["export"]["status"] == "exported"
    finally:
        release.set()
        exported.set()
        await task


@pytest.mark.asyncio
async def test_exchange_cannot_be_read_under_another_workspace(plugin, tmp_path):
    configure(
        plugin, tmp_path, [Script([call("submit_response", answer="A")]), Script([call("submit_response", answer="B")])]
    )
    a = (await plugin.rimworld_consult(question="A", _ctx=host_context("alpha"))).value
    b = (await plugin.rimworld_consult(question="B", _ctx=host_context("beta"))).value
    result = await plugin.workspace_view(
        action="turn", workspace_id=a["workspace"]["workspace_id"], turn_id=b["workspace"]["turn_id"]
    )
    assert result.is_err()


def test_evidence_is_exact_and_hash_checked(tmp_path):
    path = tmp_path / "work.sqlite"
    store = WorkspaceStore(path)
    wid = store.open_work("alpha", "colony")
    tid = store.accept(wid, "alpha", "Read facts")
    store.start(wid, "alpha", tid)
    source = {"observation_id": "source", "response": {"payload": {"data": {"flag": False, "value": None}}}}
    store.finish(wid, "alpha", tid, {"status": "ok", "answer": "Facts", "observations": [source]})
    store.close()
    view = WorkspaceViews(path).read("observation", workspace_id=wid, turn_id=tid, observation_id="source")
    assert view["observation"] == source and view["historical"] and not view["current_observation"]
    # Corrupt a disposable fixture, never an archived experiment or real work.
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER sealed_observation")
        db.execute("UPDATE observations SET payload='{}'")
    with pytest.raises(ValueError, match="hash check"):
        WorkspaceViews(path).read("observation", workspace_id=wid, turn_id=tid, observation_id="source")
