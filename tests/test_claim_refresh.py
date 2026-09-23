"""Minimal native receipts from rescue baseline/01-triage (tick 8).

Only the patient capacity collection and transport/scope identity are retained.
"""
import copy
import json
from pathlib import Path

import pytest

from plugin.plugins.neko_rimworld.agent.claims import build_read_index, check_claims
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, SubmissionPatchError

TEXT = 'Rescue_Patient 的全部11项能力均已读取。'


def receipts():
    return json.loads((Path(__file__).parent / 'fixtures/rescue_capacity_refresh.json').read_text())


def result(observation):
    return observation['response']['payload']['data']['results'][0]


def capacities(observation):
    return result(observation)['rows'][0]['health']['capacities']


def test_real_partial_then_complete_refreshes_without_mutating_observations():
    readings = receipts()
    original = copy.deepcopy(readings)
    assert check_claims(TEXT, observations=readings[:1])
    assert check_claims(TEXT, observations=readings) == []
    assert readings == original
    index = build_read_index(readings)
    old = next(c for c in index['collections'] if c['total'] == 11 and c['returned'] == 5)
    assert old['complete'] is False and old['superseded'] is True


@pytest.mark.parametrize('difference', [
    'connection_epoch', 'colony_id', 'load_epoch', 'game_tick', 'snapshot_sequence',
    'missing_sample', 'subject', 'collection_path', 'relation', 'where', 'args',
    'total', 'scan_complete', 'fields_complete', 'aggregate_only', 'later_partial',
])
def test_different_or_insufficient_receipt_cannot_refresh(difference):
    readings = receipts()
    newer = readings[1]
    relation = capacities(newer)
    if difference in ('connection_epoch', 'colony_id', 'load_epoch', 'game_tick', 'snapshot_sequence'):
        newer['response'][difference] = 'different'
    elif difference == 'missing_sample':
        newer['response'].pop('game_tick')
    elif difference == 'subject':
        result(newer)['rows'][0]['id'] = 'Thing_Other'
    elif difference == 'collection_path':
        result(newer)['rows'][0]['health']['other_capacities'] = result(newer)['rows'][0]['health'].pop('capacities')
    elif difference == 'relation':
        relation['scope']['relation'] = 'Health.other'
    elif difference == 'where':
        relation['scope']['where'] = {'level': {'gt': 0}}
    elif difference == 'args':
        relation['scope']['args'] = {'visible_only': True}
    elif difference == 'total':
        relation['matched_count'] = 12
    elif difference in ('scan_complete', 'fields_complete'):
        relation[difference] = False
    elif difference == 'aggregate_only':
        relation.pop('rows')
        relation['aggregates'] = {'count': {'complete': True, 'value': 11}}
    elif difference == 'later_partial':
        readings.reverse()
    assert 'incomplete_quantifier_claim' in [c['code'] for c in check_claims(TEXT, observations=readings)]


def test_complete_refresh_does_not_authorize_unrelated_health_claim():
    assert 'health_completeness_claim' in [c['code'] for c in check_claims(
        'Rescue_Patient 完全健康。', observations=receipts())]


def test_submission_can_recover_after_real_partial_rejection():
    readings = receipts()
    draft = SubmissionDraft()
    with pytest.raises(SubmissionPatchError) as caught:
        draft.apply({"answer": [{"text": TEXT}]}, observations=readings[:1])
    assert caught.value.code == "incomplete_quantifier_claim"
    assert draft.claims_blocked is True
    submitted = draft.apply({"answer": [{"text": TEXT}]}, observations=readings)
    assert submitted["agent_text"] == TEXT
    assert draft.claims_blocked is False


def test_complete_receipt_in_request_does_not_refresh():
    readings = receipts()
    readings[1] = {"request": readings[1]}
    assert check_claims(TEXT, observations=readings)


@pytest.mark.parametrize("old_sequence,new_sequence,accepted", [
    (1, 2, True), (1, 1, True), (2, 1, False),
    (None, 2, False), (1, None, False), (1, -1, False),
])
def test_native_sequence_progression_within_same_tick(old_sequence, new_sequence, accepted):
    readings = receipts()
    for observation, sequence in zip(readings, [old_sequence, new_sequence]):
        if sequence is None:
            observation["response"].pop("snapshot_sequence")
        else:
            observation["response"]["snapshot_sequence"] = sequence
    assert (check_claims(TEXT, observations=readings) == []) is accepted


def test_newer_sequence_at_different_tick_does_not_refresh():
    readings = receipts()
    readings[1]["response"]["snapshot_sequence"] = 2
    readings[1]["response"]["game_tick"] += 1
    assert check_claims(TEXT, observations=readings)
