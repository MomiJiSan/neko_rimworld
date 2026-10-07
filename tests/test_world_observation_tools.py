import asyncio
import copy

import pytest

from plugin.plugins.neko_rimworld.agent.semantic_tools import semantic_interaction_factory
from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError
from plugin.plugins.neko_rimworld.observation import Observation
from plugin.plugins.neko_rimworld.protocol import QUERY_RESULT, Payload, SessionDescriptor, make_envelope
from plugin.plugins.neko_rimworld.query_tools import make_game_tools

from .test_semantic_tools import Session, _update, public_source

NAMES = ("survey_space", "inspect_route", "inspect_state")


def catalog():
    source = public_source()
    source["world_observation"] = {
        "version": "world_observation.v2.2",
        "tools": [
            {"name": name, "operation": name, "description": "Read current state", "parameters": {"type": "object", "properties": {}}}
            for name in NAMES
        ],
    }
    return source


def test_capability_registration_dispatch_and_raw_evidence():
    source = catalog()
    before = copy.deepcopy(source)
    requests = []

    async def backend(request):
        requests.append(copy.deepcopy(request))
        payload = {
            "status": "partial",
            "objects": [
                {
                    "target": {"id": "x"},
                    "facets": {
                        "bed": {"status": "unavailable"},
                        "condition": {"hit_points": None},
                        "pawn": {
                            "health_summary_fraction": 1.0,
                            "conditions": [{"definition": "Cataract"}],
                            "status": "observed",
                        },
                        "stats": {"status": "unavailable"},
                    },
                }
            ],
            "sample": {"started_tick": 10, "finished_tick": 11},
            "coverage": {"complete": False},
        }
        env = make_envelope(
            QUERY_RESULT,
            "1" * 32,
            len(requests),
            payload=Payload(
                status="partial", data=payload, issues=[{"code": "unavailable", "message": "facet missing", "path": []}]
            ),
            session=SessionDescriptor("2" * 32, "3" * 32, 11, len(requests)),
            request_id="4" * 32,
        )
        return Observation.from_response(request["operation"], request["arguments"], env)

    session = Session()
    adapter = semantic_interaction_factory(make_game_tools(source, backend), session, _update)
    specs = {t.name: t for t in adapter.tools()}
    assert set(NAMES) <= set(specs)
    for name in NAMES:
        result = asyncio.run(specs[name].handler({"targets": [{"id": "x"}], **({"include": ["environment", "objects", "geometry"]} if name != "inspect_state" else {})}))
        assert result["coverage"]["complete"] is False
        facets = result["objects"][0]["facets"]
        assert list(facets)[:2] == ["pawn", "stats"]
        raw = session.observations[-1]["response"]["payload"]["data"]["objects"][0]["facets"]
        assert facets == raw and list(raw) == ["bed", "condition", "pawn", "stats"]
        facets["pawn"]["conditions"].clear()
        assert len(raw["pawn"]["conditions"]) == 1
    assert [r["operation"] for r in requests] == list(NAMES)
    assert len(session.observations) == 3 and source == before
    assert "inspect_route" in adapter.prompt(4)
    adapter.finalizing = True
    assert [t.name for t in adapter.tools()] == ["submit_response"]


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong_operation"])
def test_malformed_native_registration_rejected(change):
    source = catalog()
    ts = source["world_observation"]["tools"]
    if change == "missing":
        ts.pop()
    elif change == "duplicate":
        ts[-1] = copy.deepcopy(ts[0])
    else:
        ts[0]["operation"] = "reserve"
    with pytest.raises(ToolExecutionError):
        make_game_tools(source, lambda *a: None)
