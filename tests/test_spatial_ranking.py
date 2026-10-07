import asyncio
import copy

import pytest

from plugin.plugins.neko_rimworld.agent.semantic_tools import semantic_interaction_factory
from plugin.plugins.neko_rimworld.agent.spatial_ranking import rank_paths
from plugin.plugins.neko_rimworld.query_tools import make_game_tools

from .test_semantic_tools import Session, _update, public_source


def row(i, value, actor="p"):
    return {
        "input_index": i,
        "result": {
            "from": {"id": actor},
            "route": {"status": "found", "path_length": {"value": value, "unit": "map_cell_widths"}},
        },
    }


def test_rank_zero_first_ties_and_unknown_without_mutation():
    rows = [row(0, 1.4), row(1, 14.5), row(2, 0), row(3, 1.4), {"input_index": 4, "result": {"answer": "unsupported"}}]
    original = copy.deepcopy(rows)
    result = rank_paths(rows, "path_length")
    assert [r["input_index"] for r in result["results"]] == [2, 0, 3, 1, 4]
    assert [r["rank"] for r in result["results"]] == [1, 2, 2, 4, None]
    assert result["ordering"]["complete"] is False and rows == original
    different = rank_paths([row(0, 1, "p"), row(1, 2, "q")], "path_length")
    assert different["ordering"]["status"] == "unavailable"
    assert all(r["rank"] is None for r in different["results"])


def test_ranking_does_not_mix_units():
    rows = [row(0, 1), row(1, 2)]
    rows[1]["result"]["route"]["path_length"]["unit"] = "seconds"
    result = rank_paths(rows, "path_length")
    assert result["ordering"]["ranked"] == 1
    assert result["results"][1]["rank"] is None


def test_multi_target_handler_validates_first_and_returns_program_ranks():
    from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError
    from plugin.plugins.neko_rimworld.observation import Observation
    from plugin.plugins.neko_rimworld.protocol import QUERY_RESULT, Payload, SessionDescriptor, make_envelope

    source = public_source(spatial=True)
    source["spatial_query"]["route_summary"] = "native_path_summary.v1"
    source["spatial_query"]["parameters"]["properties"]["detail"] = {"type": "string", "enum": ["summary", "route"]}
    requests = []

    async def backend(request):
        requests.append(request)
        value = {"a": 1.4, "b": 14.5, "c": 0}[request["arguments"]["to"]["id"]]
        data = {
            "answer": "reachable",
            "from": {"id": "p"},
            "route": {"status": "found", "grid_distance": {"value": value, "unit": "map_cell_widths"}},
        }
        env = make_envelope(
            QUERY_RESULT,
            "1" * 32,
            len(requests),
            payload=Payload(status="ok", data=data, issues=[]),
            session=SessionDescriptor("2" * 32, "3" * 32, 1, 1),
            request_id="4" * 32,
        )
        return Observation.from_response("spatial_query", request["arguments"], env)

    session = Session()
    adapter = semantic_interaction_factory(make_game_tools(source, backend), session, _update)
    tool = next(t for t in adapter.tools() if t.name == "inspect_space")
    args = {
        "from": {"kind": "pawn", "id": "p"},
        "to": [{"kind": "thing", "id": i} for i in ["a", "b", "c"]],
        "order_by": "path_length",
    }
    result = asyncio.run(tool.handler(args))
    assert [r["input_index"] for r in result["results"]] == [2, 0, 1]
    assert len(requests) == len(session.observations) == 3
    assert all(r["arguments"]["detail"] == "route" for r in requests)
    requests.clear()
    args["to"][-1] = {"kind": "cell", "x": True, "z": 0}
    with pytest.raises(ToolExecutionError):
        asyncio.run(tool.handler(args))
    assert requests == []


def test_world_change_aborts_batch_before_third_read_or_ranking():
    from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError
    from plugin.plugins.neko_rimworld.observation import Observation
    from plugin.plugins.neko_rimworld.protocol import QUERY_RESULT, Payload, SessionDescriptor, make_envelope

    source = public_source(spatial=True)
    cap = source["spatial_query"]
    cap["route_summary"] = "native_path_summary.v1"
    cap["parameters"]["properties"]["detail"] = {"type": "string", "enum": ["summary", "route"]}
    requests = []

    async def backend(request):
        requests.append(request)
        epoch = "3" * 32 if len(requests) == 1 else "5" * 32
        data = {
            "answer": "reachable",
            "from": {"id": "p"},
            "route": {"status": "found", "grid_distance": {"value": 1, "unit": "map_cell_widths"}},
        }
        env = make_envelope(
            QUERY_RESULT,
            "1" * 32,
            len(requests),
            payload=Payload(status="ok", data=data, issues=[]),
            session=SessionDescriptor("2" * 32, epoch, 1, 1),
            request_id="4" * 32,
        )
        return Observation.from_response("spatial_query", request["arguments"], env)

    session = Session()
    adapter = semantic_interaction_factory(make_game_tools(source, backend), session, _update)
    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(
            adapter.inspect_space(
                {
                    "from": {"kind": "pawn", "id": "p"},
                    "to": [{"kind": "thing", "id": i} for i in ["a", "b", "c"]],
                    "order_by": "path_length",
                }
            )
        )
    assert error.value.code == "world_changed" and error.value.recoverable is False
    assert len(requests) == 2 and len(session.observations) == 1
