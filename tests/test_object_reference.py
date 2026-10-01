import copy
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.activity import SemanticActivityDataSource
from plugin.plugins.neko_rimworld.agent.object_tools import observed_id_reference, ObjectReader
from plugin.plugins.neko_rimworld.agent.semantic_surface import normalize_object, route_object
from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError
from plugin.plugins.neko_rimworld.agent.query_surface import SurfaceError
from .test_object_tools import Reader


def receipts(*objects, status='ok'):
    return [{'response': {'payload': {'status': status, 'data': {'objects': [
        {'target': obj} for obj in objects]}}}}]


BED = {'id': 'Thing_Bed7', 'name': 'Bed', 'kind': 'thing', 'map': 'Map_0'}


@pytest.mark.parametrize('kind', ['thing_def', 'thing_defs'])
@pytest.mark.parametrize('value', [True, False])
def test_definition_scope_flags_do_not_change_the_read(kind, value):
    base = {'kind': kind, 'definition': 'Steel'}
    normalized = normalize_object({**base, 'include_held': value, 'include_fogged': value})
    assert normalized == normalize_object(base)
    assert route_object(normalized) == route_object(normalize_object(base))
    with pytest.raises(SurfaceError):
        normalize_object({**base, 'include_held': 'false'})


def test_only_observed_unique_ids_are_normalized_without_mutation():
    ref = {'kind': 'thing', 'name': 'Thing_Bed7', 'map': 'current'}
    readings = receipts(BED)
    before = copy.deepcopy((ref, readings))
    assert observed_id_reference(ref, readings) == {'kind': 'thing', 'id': 'Thing_Bed7', 'map': 'current'}
    assert (ref, readings) == before
    assert observed_id_reference(ref, []) == ref
    assert observed_id_reference(ref, receipts(BED, status='error')) == ref
    assert observed_id_reference({**ref, 'map': 'Map_1'}, readings) == {**ref, 'map': 'Map_1'}
    assert observed_id_reference({**ref, 'ids': ['other']}, readings) == {**ref, 'ids': ['other']}
    collision = {**BED, 'id': 'Thing_Actual9', 'name': 'Thing_Bed7'}
    assert observed_id_reference(ref, receipts(BED, collision)) == ref
    assert observed_id_reference(ref, receipts(BED, {**BED, 'map': 'Map_1'})) == ref
    assert observed_id_reference(ref, [{'request': {'response': readings[0]['response']}}]) == ref


@pytest.mark.asyncio
@pytest.mark.parametrize('selector', [{'name': 'Thing_Bed7'}, {'names': ['Thing_Bed7']}])
async def test_known_id_in_name_uses_one_current_native_read(selector):
    adapter = Reader([], {'status': 'ok', 'observation_id': 'current-bed', 'data': {'objects': [{
        'target': {**BED, 'definition': 'Bed'}, 'facets': {},
        'coverage': {'registered_facets_read_successfully': True}}]}})
    adapter.session = SimpleNamespace(observations=receipts(BED))
    args = {'object': {'kind': 'thing', **selector}, 'aspects': ['state']}
    result = await ObjectReader(adapter).look(args)
    assert result['status'] == 'ok'
    assert len(adapter.calls) == 1
    assert adapter.calls[0][1]['targets'][0]['id'] == 'Thing_Bed7'
    assert args['object'] == {'kind': 'thing', **selector}


@pytest.mark.asyncio
async def test_activity_reuses_known_id_without_identity_call():
    interaction = SimpleNamespace(session=SimpleNamespace(observations=receipts(BED)))
    resolved = await SemanticActivityDataSource(interaction)._resolve({'kind': 'thing', 'name': 'Thing_Bed7'}, 'destination')
    assert resolved == {'kind': 'thing', 'id': 'Thing_Bed7'}


@pytest.mark.asyncio
async def test_activity_accepts_explicit_id_shape_in_name_without_identity_call():
    interaction = SimpleNamespace(session=SimpleNamespace(observations=[]))
    resolved = await SemanticActivityDataSource(interaction)._resolve(
        {'kind': 'thing', 'name': 'Thing_Bed7'}, 'destination'
    )
    assert resolved == {'kind': 'thing', 'id': 'Thing_Bed7'}


@pytest.mark.asyncio
async def test_missing_reference_does_not_report_ambiguity():
    class Missing:
        async def execute_native(self, request, tool_name):
            return {'status': 'partial', 'data': {'objects': [{'status': 'unavailable', 'reason': 'object_unavailable'}]}}
    with pytest.raises(ToolExecutionError) as caught:
        await SemanticActivityDataSource(Missing())._resolve({'kind': 'thing', 'name': 'Missing'}, 'destination')
    assert caught.value.code == 'activity_reference_unresolved'
    assert 'ambiguous' not in str(caught.value)


@pytest.mark.asyncio
async def test_native_per_target_ambiguity_keeps_candidates_and_does_not_pick():
    resolution = {'status': 'ambiguous', 'candidates': [BED, {**BED, 'id': 'Thing_Bed8'}]}
    class Ambiguous:
        async def execute_native(self, request, tool_name):
            return {'status': 'partial', 'data': {'objects': [{'status': 'unavailable',
                'reason': 'ambiguous_reference', 'resolution': resolution}]}}
    with pytest.raises(ToolExecutionError) as caught:
        await SemanticActivityDataSource(Ambiguous())._resolve({'kind': 'thing', 'name': 'Bed'}, 'destination')
    assert 'ambiguous' in str(caught.value)
    assert caught.value.details['resolution'] == resolution
