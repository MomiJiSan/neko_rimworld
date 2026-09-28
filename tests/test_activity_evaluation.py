import copy

import pytest

from plugin.plugins.neko_rimworld.agent.activity_contracts import ActivityError, compile_plan, normalize_activity
from plugin.plugins.neko_rimworld.agent.activity_executor import evaluate_activity
from plugin.plugins.neko_rimworld.agent.activity_tools import _conservative_schema, _simplified_schema


class FixtureSource:
    source_kind = "fixture"

    def __init__(self):
        self.requests = []

    async def resolve(self, activity):
        destination = activity["destination"]
        if "candidates" in destination:
            resolved = copy.deepcopy(activity)
            return resolved
        resolved = copy.deepcopy(activity)
        resolved["destination"]={"candidates":[{"kind":"thing","id":"bed-a"},{"kind":"thing","id":"bed-b"}]}
        return resolved

    async def request(self, operation, arguments):
        self.requests.append((operation, copy.deepcopy(arguments)))
        if operation == "inspect_route":
            start = arguments.get("start")
            target = arguments.get("to")
            gas = 2 if start and target.get("id", "").startswith("bed") else 0
            return {
                "status": "ok",
                "observation_id": f"obs-{len(self.requests)}",
                "data": {
                    "route": {"status": "found", "steps": 7, "path_length": {"value": 7.0, "unit": "map_cell_widths"}, "native_cost": {"value": 98.0, "unit": "native_path_cost"}},
                    "path_environment": {"gases": [{"type": "ToxGas", "cells": gas, "max_density_0_255": 180}]},
                    "from": arguments["actor"],
                    "to": target,
                },
            }
        return {"status": "ok", "observation_id": f"obs-{len(self.requests)}", "data": {"meaning": "native object state"}}


def transfer_args():
    return {
        "kind": "transfer",
        "actor": {"candidates": [{"kind": "pawn", "id": "near"}, {"kind": "pawn", "id": "middle"}]},
        "subject": {"kind": "pawn", "id": "patient"},
        "destination": [{"kind": "thing", "id": "bed-a"}, {"kind": "thing", "id": "bed-b"}],
        "inspect": ["approach_route", "transfer_route", "destination_state"],
        "compare": ["steps", "native_cost", "path_hazards"],
    }


def test_plan_preserves_actor_subject_origin_destination_for_each_stage():
    spec = normalize_activity(transfer_args())
    plan = compile_plan(spec, spec["destination"]["candidates"])
    assert len(plan["candidates"]) == 4
    transfer = [stage for row in plan["candidates"] for stage in row["stages"] if stage["kind"] == "transfer"]
    assert len(transfer) == 4
    assert {stage["actor"]["id"] for stage in transfer} == {"near", "middle"}
    assert all(stage["origin"]["id"] == "patient" for stage in transfer)
    assert all(stage["hypothetical_start"] is True for stage in transfer)
    assert all(request["operation"] in {"inspect_route", "look"} for request in plan["requests"])


def test_invalid_duplicate_and_oversized_combinations_fail_before_reads():
    args = transfer_args()
    args["actor"]["candidates"].append({"kind": "pawn", "id": "near"})
    with pytest.raises(ActivityError) as error:
        normalize_activity(args)
    assert error.value.code == "duplicate_activity_reference"

    args = transfer_args()
    args["actor"]["candidates"] = [{"kind": "pawn", "id": str(i)} for i in range(4)]
    args["destination"] = [{"kind": "thing", "id": str(i)} for i in range(8)]
    with pytest.raises(ActivityError) as error:
        normalize_activity(args)
    assert error.value.code == "activity_combination_limit"


@pytest.mark.asyncio
async def test_transfer_evaluation_keeps_transfer_hazards_separate_from_approach():
    source = FixtureSource()
    result = await evaluate_activity(transfer_args(), source)
    assert result["status"] == "ok"
    assert result["coverage"]["requests_completed"] == result["plan"]["request_count"]
    assert result["coverage"]["complete"] is True
    assert result["decision_ready"] is False
    transfer_rows = [stage for candidate in result["candidates"] for stage in candidate["stages"] if stage["kind"] == "transfer"]
    assert transfer_rows and all(stage["result"]["path_hazards"][0]["cells"] == 2 for stage in transfer_rows)
    approach_rows = [stage for candidate in result["candidates"] for stage in candidate["stages"] if stage["kind"] == "approach"]
    assert approach_rows and all(stage["result"]["path_hazards"][0]["cells"] == 0 for stage in approach_rows)
    assert all(row["stage_id"].endswith(":transfer") for row in transfer_rows)


def test_agent_surface_variants_are_small_and_reject_unknown_fields():
    conservative = _conservative_schema()
    simplified = _simplified_schema()
    assert set(conservative["properties"]) == {"kind", "actor", "subject", "start", "stats", "destination", "inspect", "compare", "detail"}
    assert set(simplified["properties"]) == {"kind", "actors", "subject", "stats", "start", "destinations", "preferences"}
    assert conservative["additionalProperties"] is False
    assert simplified["additionalProperties"] is False


def test_compiler_uses_real_route_detail_enum():
    spec = normalize_activity(transfer_args())
    requests = compile_plan(spec, spec['destination']['candidates'])['requests']
    assert all(r['arguments']['detail'] in ('summary', 'details') for r in requests if r['operation']=='inspect_route')


@pytest.mark.parametrize('change', ['unknown', 'subject', 'detail'])
def test_invalid_contract_never_reaches_game(change):
    args = transfer_args()
    if change == 'unknown': args['invented'] = True
    if change == 'subject': args.pop('subject')
    if change == 'detail': args['detail'] = 'route'
    with pytest.raises(ActivityError): normalize_activity(args)


@pytest.mark.asyncio
async def test_partial_unknown_gas_is_never_ranked_as_zero():
    class Unknown(FixtureSource):
        async def request(self, operation, arguments):
            value = await super().request(operation, arguments)
            if operation == 'inspect_route':
                value['status']='partial'
                value['data']['path_environment']['gases']=[{'type':'ToxGas','cells':None}]
            return value
    result=await evaluate_activity(transfer_args(), Unknown())
    assert not result['coverage']['complete']
    assert result['comparison']['path_hazards'].get('entries', []) == []


def test_advertised_schema_accepts_documented_candidate_wrapper():
    import jsonschema
    jsonschema.validate(transfer_args(), _conservative_schema())


def test_schema_rejects_invalid_stage_context_and_selector_shape():
    import jsonschema
    move = {"kind": "move", "actor": {"candidates": [{"kind": "pawn", "id": "near"}]},
            "subject": {"kind": "pawn", "id": "patient"}, "destination": [{"kind": "thing", "id": "bed-a"}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(move, _conservative_schema())
    selector = {"kind": "move", "actors": [{"kind": "pawn", "id": "near"}],
                "destinations": [{"kind": "thing", "id": "bed-a"}],
                "preferences": ["higher_observed_stat"]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(selector, _simplified_schema())


@pytest.mark.asyncio
async def test_native_object_state_observation_is_projected_to_stage_readiness():
    class NativeLook(FixtureSource):
        async def request(self, operation, arguments):
            if operation == "look":
                return {"status": "ok", "data": {"facts": [{"object": {"id": arguments["object"]["id"]},
                    "state_sample": {"observation_id": "state-1"}, "state_coverage": {"registered_facets_read_successfully": True},
                    "facts": {aspect: {"status": "observed"} for aspect in arguments['aspects']}}], "state_observations": [{
                        "observation_id": "state-1", "status": "ok", "sample": {"load_epoch": "load-1", "started_tick": 1, "finished_tick": 1},
                        "context": {"actor_id": None, "user_id": None}}]}}
            return await super().request(operation, arguments)
    args = {"kind": "move", "actor": {"candidates": [{"kind": "pawn", "id": "near"}]},
            "destination": [{"kind": "thing", "id": "bed-a"}], "inspect": ["actor_state"], "compare": []}
    result = await evaluate_activity(args, NativeLook())
    assert result["decision_ready"] is True
    assert result["evidence"][0]["observed"] == ["actor_state"]


@pytest.mark.asyncio
async def test_simplified_higher_stat_preference_produces_descending_comparison():
    class StatSource(FixtureSource):
        async def request(self, operation, arguments):
            if operation == 'look':
                actor_id = arguments['object']['id']
                value = 16 if actor_id == 'near' else 8
                return {'status': 'ok', 'observation_id': f'state-{actor_id}', 'data': {
                    'facts': [{'object': {'id': actor_id}, 'state_sample': {'observation_id': f'state-{actor_id}'},
                               'state_coverage': {'registered_facets_read_successfully': True},
                               'facts': {'skills': {'ok': True}, 'stats': {'values': [{'definition': 'Medicine', 'value': value, 'status': 'observed'}], 'complete': True}}}],
                    'state_observations': [{'observation_id': f'state-{actor_id}', 'status': 'ok', 'sample': {'load_epoch': 'load-1', 'started_tick': 1, 'finished_tick': 1}, 'context': {}}]}}
            return await super().request(operation, arguments)
    args = {'kind': 'move', 'actors': [{'kind': 'pawn', 'id': 'near'}, {'kind': 'pawn', 'id': 'middle'}],
            'stats': ['Medicine'], 'destinations': [{'kind': 'thing', 'id': 'bed-a'}], 'preferences': ['higher_observed_stat']}
    result = await evaluate_activity(args, StatSource(), variant='simplified')
    entries = result['comparison']['observed_stat']['groups'][0]['entries']
    assert entries[0]['actor']['id'] == 'near' and entries[0]['rank'] == 1
