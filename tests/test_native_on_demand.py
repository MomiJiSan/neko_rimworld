"""world_observation.v2.3: route display selection becomes native generation.

Pins the forwarding boundary end to end on the plugin side: which requests
gain the native `objects` flag, which connections must stay byte-identical,
and that the older contract keeps the audited dispatch behavior (objects
on/off produced the same native request under v2.2).
"""

import copy
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.object_tools import state_contract
from plugin.plugins.neko_rimworld.agent.semantic_tools import SemanticInteraction
from plugin.plugins.neko_rimworld.agent.tools import WORLD_OBSERVATION_VERSIONS


def route_recorder(version):
    """Unbound _world_read harness mirroring the feasibility audit dispatch control."""
    calls = []

    async def execute_native(args, *, tool_name="query"):
        calls.append((tool_name, copy.deepcopy(args)))
        return {
            "status": "ok",
            "observation_id": "obs-1",
            "issues": [],
            "data": {"route": {"status": "found", "steps": 7}},
        }

    stub = SimpleNamespace(
        original={"inspect_route": object(), "survey_space": object()},
        setup=SimpleNamespace(directory={"world_observation": {"version": version}}),
        execute_native=execute_native,
    )
    return stub, calls


ROUTE = {"actor": {"kind": "pawn", "id": "Pawn_1"}, "to": {"kind": "thing", "id": "Thing_Bed1"}}


@pytest.mark.asyncio
async def test_v23_route_without_include_requests_no_native_objects(monkeypatch):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    stub, calls = route_recorder("world_observation.v2.3")
    await SemanticInteraction._world_read(stub, {**ROUTE, "include": ["environment"]}, "inspect_route")
    tool_name, args = calls[0]
    assert tool_name == "inspect_route"
    assert args["objects"] is False
    assert "include" not in args and "include_objects" not in args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested",
    [
        {"include_objects": True},
        {"include": ["objects"]},
        {"include": ["environment", "objects"], "include_objects": False},
        {"cursor": "page:2"},
    ],
)
async def test_v23_route_keeps_native_objects_when_the_view_keeps_them(monkeypatch, requested):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    stub, calls = route_recorder("world_observation.v2.3")
    await SemanticInteraction._world_read(stub, {**ROUTE, **requested}, "inspect_route")
    assert calls[0][1]["objects"] is True


@pytest.mark.asyncio
async def test_v23_survey_explicit_environment_selection_avoids_incidental_objects(monkeypatch):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    stub, calls = route_recorder("world_observation.v2.3")
    await SemanticInteraction._world_read(
        stub, {"region": {"map": "current"}, "include": ["environment"]}, "survey_space"
    )
    assert calls[0][1]["objects"] is False and "include" not in calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["world_observation.v2.2", "world_observation.v2.3"])
async def test_survey_default_and_older_dispatch_remain_compatible(monkeypatch, version):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    stub, calls = route_recorder(version)
    await SemanticInteraction._world_read(stub, {"region": {"map": "current"}}, "survey_space")
    assert calls[0][1] == {"region": {"map": "current"}}
    if version == "world_observation.v2.2":
        await SemanticInteraction._world_read(stub, {"region": {"map": "current"}, "include": ["environment"]}, "survey_space")
        assert calls[1][1] == calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"objects": True}, {"include": ["objects"]}, {"cursor": "next-page"}])
async def test_survey_object_opt_in_and_paging_remain_available(monkeypatch, extra):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    stub, calls = route_recorder("world_observation.v2.3")
    await SemanticInteraction._world_read(stub, {"region": {"map": "current"}, "include": ["environment"], **extra}, "survey_space")
    assert calls[0][1]["objects"] is True


@pytest.mark.asyncio
async def test_v22_dispatch_control_stays_identical_with_objects_on_and_off(monkeypatch):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    stub, calls = route_recorder("world_observation.v2.2")
    for objects in (False, True):
        await SemanticInteraction._world_read(
            stub, {**ROUTE, "include": ["environment"], "include_objects": objects}, "inspect_route"
        )
    assert calls[0][1] == calls[1][1] == ROUTE




def state_directory(version):
    return {
        "world_observation": {
            "version": version,
            "tools": [
                {
                    "name": "inspect_state",
                    "operation": "inspect_state",
                    "description": "state",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            key: {}
                            for key in (
                                "targets",
                                "stats",
                                "actor",
                                "user",
                                "include_fogged",
                                "detail_limit",
                                "exact_lookup",
                            )
                        }
                        | {"sections": {"items": {"enum": ["identity", "state", "health", "skills", "gear", "work"]}}},
                    },
                }
            ],
        }
    }


@pytest.mark.parametrize("version", sorted(WORLD_OBSERVATION_VERSIONS))
def test_state_contract_accepts_every_supported_version(version):
    assert state_contract(state_directory(version))["name"] == "inspect_state"


def test_unknown_contract_versions_are_refused():
    assert state_contract(state_directory("world_observation.v9.9")) is None
    assert state_contract({}) is None


@pytest.mark.asyncio
async def test_activity_objects_selection_reaches_the_route_request(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.activity import SemanticActivityDataSource, evaluate_activity
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction

    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    arguments = {
        "kind": "transfer",
        "actors": [{"kind": "pawn", "id": "a", "map": "Map_0"}],
        "subject": {"kind": "pawn", "id": "patient", "map": "Map_0"},
        "destinations": [{"kind": "thing", "id": "bed1", "map": "Map_0"}],
    }
    native = NativeInteraction()
    await evaluate_activity(arguments, SemanticActivityDataSource(native))
    routes = [args for op, args in native.calls if op == "inspect_route"]
    assert routes and all("include_objects" not in args for args in routes)

    native = NativeInteraction()
    await evaluate_activity({**arguments, "include": ["objects"]}, SemanticActivityDataSource(native))
    routes = [args for op, args in native.calls if op == "inspect_route"]
    assert routes and all(args.get("include_objects") is True for args in routes)
