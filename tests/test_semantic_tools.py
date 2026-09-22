"""The semantic surface is the shipped interface, and it knows when to step aside.

What matters here is the wiring: which connections get the closed-enum vocabulary,
that the Mod's directory really leaves the prompt, and that the one combination
the adapter cannot serve keeps working. The compile and render semantics are
covered by the surface's own tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime, ToolCall
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft
from plugin.plugins.neko_rimworld.agent.semantic_tools import (
    ENVIRONMENT_SWITCH,
    SemanticInteraction,
    semantic_interaction_factory,
    semantic_surface_available,
    semantic_surface_requested,
)
from plugin.plugins.neko_rimworld.agent.tools import ToolSetup
from plugin.plugins.neko_rimworld.query_tools import make_game_tools

from .test_agent import FakeLogger, settings, turn

FIXTURES = Path(__file__).parent / "fixtures"
CATALOGUE_MARKER = "canonical fields"


def catalog():
    return json.loads((FIXTURES / "query_catalog.json").read_text(encoding="utf-8-sig"))


def public_source(*, directory=True):
    """A public_query catalog, with or without the directory the Surface needs."""
    source = catalog()
    parameters = {
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "items": {
                    "allOf": [
                        {
                            "oneOf": [
                                {"properties": {"from": {"const": "things"}, "args": {"type": "object"}}},
                                {"properties": {"from": {"const": "pawn"}, "args": {"type": "object"}}},
                            ]
                        }
                    ]
                },
            }
        },
    }
    public = {"version": "public_query.v2", "parameters": parameters,
              "rules": CATALOGUE_MARKER, "roots": []}
    if directory:
        public.update(
            roots=[{"from": "things", "type": "Thing", "description": "Map things."},
                   {"from": "pawn", "type": "Pawn", "description": "One pawn."}],
            field_index={
                "Thing": {"id": "String", "spawned": "Bool", "stack_count": "Int",
                          "definition": "ThingDef", "parent_holder": "Thing"},
                "Pawn": {"id": "String", "name": "String"},
            },
            common_fields=[],
            field_notes={},
        )
    source["public_query"] = public
    return source


async def _noop_query(arguments):
    raise AssertionError("no native read expected")


def setup_for(**kwargs) -> ToolSetup:
    return make_game_tools(public_source(**kwargs), _noop_query)


class Session:
    """Only the attributes the adapter reads while building tools or a prompt."""

    def __init__(self):
        self.submission = SubmissionDraft()
        self.observations = []


def _update(*_args, **_kwargs):
    return None


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_SWITCH, raising=False)


def test_semantic_surface_is_the_shipped_default():
    assert semantic_surface_requested() is True
    assert semantic_surface_available(setup_for()) is True


def test_deployment_can_return_to_the_mod_interface_without_a_code_change(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_SWITCH, "0")
    assert semantic_surface_requested() is False
    assert semantic_surface_available(setup_for()) is False
    assert semantic_interaction_factory(setup_for(), None, None) is None


def test_the_mod_directory_leaves_the_prompt_but_the_enums_stay():
    """The reason this interface exists: the whole field catalogue stops being sent."""
    setup = setup_for()
    assert CATALOGUE_MARKER in setup.context, "the query interface needs the catalogue"

    adapter = semantic_interaction_factory(setup, Session(), _update)
    assert isinstance(adapter, SemanticInteraction)
    prompt = adapter.prompt(4)

    assert CATALOGUE_MARKER not in prompt
    assert '"field_index"' not in prompt
    assert "look(object, aspects, detail?)" in prompt
    assert "count(object, metric, by?, detail?)" in prompt


def test_semantic_surface_replaces_the_query_vocabulary():
    adapter = semantic_interaction_factory(setup_for(), Session(), _update)
    assert [tool.name for tool in adapter.tools()] == ["look", "count", "submit_response"]

    adapter.finalizing = True
    assert [tool.name for tool in adapter.tools()] == ["submit_response"]


def test_a_mod_without_the_directory_keeps_its_own_interface():
    setup = setup_for(directory=False)
    assert semantic_surface_available(setup) is False
    assert semantic_interaction_factory(setup, None, None) is None


def test_a_setup_without_a_query_tool_keeps_its_own_interface():
    """The adapter may only read through the Mod's query handler."""
    setup = ToolSetup(tools=(), directory=public_source()["public_query"], context="")
    assert semantic_surface_available(setup) is False


def test_a_directory_that_cannot_compile_is_refused_before_the_prompt_promises_it():
    source = public_source()
    del source["public_query"]["field_index"]
    setup = make_game_tools(source, _noop_query)
    assert setup.directory is not None and "field_index" not in setup.directory
    assert semantic_surface_available(setup) is False


@pytest.mark.asyncio
async def test_an_attached_report_keeps_the_mod_interface():
    """A report registers its own evidence tool and prompt text.

    An interaction adapter rebuilds both every cycle, so the attached-report
    consultation stays on the Mod's own interface. This used to be a hard error;
    it is now a per-run decision, and the report must still deliver.
    """
    engaged = []

    def factory(setup, session, update):
        engaged.append(setup)
        return semantic_interaction_factory(setup, session, update)

    model = _Model([
        turn("", ToolCall("finish", "submit_response", json.dumps(
            {"detail": "历史报告的完整说明。", "answer": [{"text": "历史报告的结论。"}]},
            ensure_ascii=False))),
    ])

    async def tools_provider():
        return setup_for()

    runtime = AgentRuntime(observation_tools=[], tools_provider=tools_provider,
                           audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model, interaction_factory=factory)

    result = await runtime.run("这一问。", settings(), reference_report=report())

    assert engaged == [], "the adapter must not be built for an attached report"
    assert result["status"] == "ok"
    assert "历史报告的结论。" in result["agent_text"]


class _Model:
    """Returns the scripted turns in order.

    `test_agent.ScriptedModel` asserts the first tool is the legacy `query_game`,
    which is not true on the public_query path this surface serves.
    """

    def __init__(self, turns):
        self._turns = list(turns)

    async def complete(self, messages, tools, *, timeout):  # noqa: ARG002
        return self._turns.pop(0)


@pytest.mark.asyncio
async def test_wiki_is_the_only_extra_passthrough_and_never_an_observation(tmp_path):
    from dataclasses import replace

    from plugin.plugins.neko_rimworld.agent.tools import ToolRegistry, ToolSpec
    from plugin.plugins.neko_rimworld.agent.wiki_tools import WikiStore, make_wiki_tool

    from .test_wiki_tools import write_snapshot

    write_snapshot(tmp_path)
    wiki = make_wiki_tool(WikiStore(tmp_path))
    forbidden = ToolSpec("unapproved", "not exposed", {}, _noop_query)
    setup = setup_for()
    setup = replace(setup, tools=(*setup.tools, wiki, forbidden))
    session = Session()
    adapter = semantic_interaction_factory(setup, session, _update)
    assert [tool.name for tool in adapter.tools()] == ["look", "count", "wiki", "submit_response"]
    assert "definition knowledge" in adapter.prompt(4)
    registry = ToolRegistry()
    for tool in adapter.tools():
        registry.register(tool)
    result = await registry.execute(registry.prepare(ToolCall("wiki1", "wiki", '{"query":"Steel"}')))
    assert result.observation is None
    assert session.observations == []
    adapter.finalizing = True
    assert [tool.name for tool in adapter.tools()] == ["submit_response"]


def test_missing_wiki_does_not_advertise_knowledge():
    adapter = semantic_interaction_factory(setup_for(), Session(), _update)
    assert "wiki(query)" not in adapter.prompt(4)


def report():
    return {
        "report_id": "report-1",
        "state": "final",
        "created_at": "2026-09-01T00:00:00Z",
        "question": "早先那一问。",
        "full_text": "早先的完整报告。",
        "observations": [],
    }
