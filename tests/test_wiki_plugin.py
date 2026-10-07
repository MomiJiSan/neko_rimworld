"""Composition for offline, public-query and native semantic paths."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld import NekoRimworldPlugin
from plugin.plugins.neko_rimworld.agent.tools import ToolSetup
from plugin.plugins.neko_rimworld.knowledge import KnowledgeLibrary
from plugin.plugins.neko_rimworld.query_tools import make_game_tools
from plugin.plugins.neko_rimworld.reports import ReportStore

from .test_semantic_tools import _noop_query, public_source, setup_for
from .test_wiki_tools import write_snapshot


@pytest.fixture
def library(tmp_path):
    value = KnowledgeLibrary(tmp_path, tmp_path / "index")
    yield value
    value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["offline", "public", "native"])
async def test_provider_composes_wiki_without_losing_original_setup(tmp_path, mode, library):
    original = {
        "offline": ToolSetup((), "offline"),
        "public": make_game_tools(public_source(), _noop_query),
        "native": setup_for(),
    }[mode]

    async def load():
        return original

    plugin = object.__new__(NekoRimworldPlugin)
    plugin._reports = ReportStore()
    plugin._catalog_tools = SimpleNamespace(load=load)
    plugin._knowledge_directory = tmp_path
    plugin._knowledge = library
    missing = await plugin._load_agent_tools()
    assert missing.tools == original.tools and missing.context == original.context
    assert missing.capabilities == ()
    write_snapshot(tmp_path)
    assert plugin._knowledge.refresh()
    setup = await plugin._load_agent_tools()
    assert setup.tools[:-1] == original.tools
    assert setup.tools[-1].name == "wiki"
    assert setup.directory == original.directory
    assert setup.references_supported == original.references_supported
    assert setup.context.startswith(original.context)
    assert "wiki(query)" in setup.context
    assert setup.context_blocks[-1][0] == "wiki"
    first = await setup.tools[-1].handler({"query": "Steel"})
    assert first["status"] == "found"
    write_snapshot(tmp_path, entries=[])
    assert plugin._knowledge.refresh()
    assert (await setup.tools[-1].handler({"query": "Steel"})) == first
    next_run = await plugin._load_agent_tools()
    assert (await next_run.tools[-1].handler({"query": "Steel"}))["status"] == "not_found"


@pytest.mark.asyncio
async def test_invalid_knowledge_does_not_advertise_wiki(tmp_path, library):
    async def load():
        return ToolSetup((), "offline")

    plugin = object.__new__(NekoRimworldPlugin)
    plugin._reports = ReportStore()
    plugin._catalog_tools = SimpleNamespace(load=load)
    plugin._knowledge_directory = tmp_path
    plugin._knowledge = library
    (tmp_path / "current.json").write_text("broken", encoding="utf-8")
    setup = await plugin._load_agent_tools()
    assert setup.tools == () and "wiki" not in setup.context


@pytest.mark.parametrize("prepared", [False, True])
def test_provider_returns_while_background_refresh_is_blocked(tmp_path, monkeypatch, library, prepared):
    original = ToolSetup((), "offline")
    write_snapshot(tmp_path)
    if prepared:
        assert library.refresh()

    async def load():
        return original

    plugin = object.__new__(NekoRimworldPlugin)
    plugin._reports = ReportStore()
    plugin._catalog_tools = SimpleNamespace(load=load)
    plugin._knowledge = library
    entered = Queue()
    release = threading.Event()
    refresh = library.refresh

    def blocked_refresh():
        entered.put(threading.current_thread())
        assert release.wait(timeout=5), "test must release the importer"
        return refresh()

    monkeypatch.setattr(library, "refresh", blocked_refresh)
    worker = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            # Run the provider on its own event loop so even a synchronous wait
            # in its implementation fails with a bounded timeout.
            consultation = executor.submit(lambda: asyncio.run(plugin._load_agent_tools()))
            worker = entered.get(timeout=2)
            setup = consultation.result(timeout=2)
            assert worker.daemon
            assert not release.is_set()
            if prepared:
                result = asyncio.run(setup.tools[-1].handler({"query": "Steel"}))
                assert result["status"] == "found"
            else:
                assert setup.tools == original.tools and setup.context == original.context
                assert setup.capabilities == ()
        finally:
            library.close()
            release.set()
            if worker is not None and worker.daemon:
                worker.join(timeout=2)
                assert not worker.is_alive()
