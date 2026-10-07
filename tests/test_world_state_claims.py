import copy

import pytest

from plugin.plugins.neko_rimworld.agent.claims import check_claims

from .test_claim_refresh import TEXT, capacities, receipts, result


def native_state():
    full = receipts()[1]
    row = result(full)["rows"][0]
    data = {
        "objects": [
            {
                "target": {"id": row["id"], "name": "Rescue_Patient"},
                "sample_tick": full["response"]["game_tick"],
                "facets": {
                    "pawn": {
                        "status": "observed",
                        "capacities": [
                            {"definition": v["def_name"], "level": v["level"]} for v in capacities(full)["rows"]
                        ],
                        "conditions": [],
                    }
                },
            }
        ]
    }
    full["response"]["payload"]["data"] = data
    return full


def test_native_complete_then_consistent_partial_is_valid_evidence():
    readings = [native_state(), receipts()[0]]
    readings[1]["response"]["snapshot_sequence"] = 2
    before = copy.deepcopy(readings)
    assert check_claims(TEXT, observations=readings) == []
    assert readings == before
    assert any(
        e["code"] == "health_completeness_claim"
        for e in check_claims("Rescue_Patient 完全健康。", observations=readings)
    )


@pytest.mark.parametrize("change", ["tick", "subject", "values", "scope", "facet_failure"])
def test_native_complete_does_not_license_different_or_changed_partial(change):
    readings = [native_state(), receipts()[0]]
    readings[1]["response"]["snapshot_sequence"] = 2
    if change == "tick":
        readings[1]["response"]["game_tick"] += 1
    elif change == "subject":
        result(readings[1])["rows"][0]["id"] = "Thing_Other"
    elif change == "values":
        capacities(readings[1])["rows"][0]["level"] = -123
    elif change == "scope":
        capacities(readings[1])["scope"]["where"] = {"level": {"gt": 0}}
    else:
        readings[0]["response"]["payload"]["data"]["objects"][0]["facets"]["pawn"]["status"] = "unavailable"
    assert any(e["code"] == "incomplete_quantifier_claim" for e in check_claims(TEXT, observations=readings))


@pytest.mark.parametrize("field", ["load_epoch", "connection_epoch"])
def test_state_proof_never_crosses_world_or_connection(field):
    readings = [native_state(), receipts()[0]]
    readings[1]["response"][field] = "other-session"
    readings[1]["response"]["snapshot_sequence"] = 2
    assert check_claims(TEXT, observations=readings)


def test_intermediate_changed_capacity_is_not_hidden_by_later_matching_partial():
    readings = [native_state(), receipts()[0], receipts()[0]]
    readings[1]["response"]["snapshot_sequence"] = 2
    readings[2]["response"]["snapshot_sequence"] = 3
    capacities(readings[1])["rows"][0]["level"] = -123
    assert check_claims(TEXT, observations=readings)


def test_bounded_native_capacity_rows_do_not_license_full_comparison():
    reading = native_state()
    pawn = reading["response"]["payload"]["data"]["objects"][0]["facets"]["pawn"]
    pawn["capacities_total"] = len(pawn["capacities"])
    pawn["capacities"] = pawn["capacities"][:1]
    pawn["capacities_complete"] = False
    index = __import__("plugin.plugins.neko_rimworld.agent.claims", fromlist=["build_read_index"]).build_read_index(
        [reading]
    )
    native_collections = [row for row in index["collections"] if row.get("native_state_complete")]
    assert native_collections == []
    assert any(row["complete"] is False for row in index["collections"])
