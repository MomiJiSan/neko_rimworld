import copy
import json
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.activity import make_activity_tool
from plugin.plugins.neko_rimworld.agent.context_pruning import ContextPruner
from plugin.plugins.neko_rimworld.agent.object_tools import ObjectReader, STATE_FACETS
from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction
from plugin.plugins.neko_rimworld.tests.test_native_on_demand import state_directory


def test_survey_geometry_is_optional_but_environment_and_distance_origin_stay_explicit():
    from plugin.plugins.neko_rimworld.agent.context_pruning import survey_view
    raw={'region':{'geometry':{'resolved_center':{'kind':'cell','map':'Map_0','x':140,'z':124}},
        'area_zone_intersections':[{'id':'roof-designation','intersecting_cells':52}]},
        'environment':{'roof_cells':{'RoofConstructed':143},'gases':[{'type':'ToxGas','cells':2}]},
        'objects':[{'spatial_relation':{'geometric_distance':10.295630140987,'distance_unit':'map_cell_widths'}}]}
    saved=copy.deepcopy(raw)
    selected=survey_view(raw,{'include':['environment','objects']})
    assert 'area_zone_intersections' not in selected['region']
    assert selected['environment']==raw['environment']
    assert selected['objects'][0]['spatial_relation']=={'distance_to_region_center':10.295630140987,'distance_unit':'map_cell_widths'}
    assert survey_view(raw,{'include':['environment','objects','geometry']})['region']==raw['region']
    assert raw==saved


def test_unavailable_identity_map_is_not_sent_as_a_native_map_selector():
    identity={'id':'pawn','map_id':{'state':'unavailable','reason':'deadline_exceeded'}}
    saved=copy.deepcopy(identity)
    assert ObjectReader._target(identity,{'kind':'colonists'})=={'id':'pawn'}
    assert ObjectReader._target(identity,{'kind':'colonists','map':'Map_0'})=={'id':'pawn','map':'Map_0'}
    assert identity==saved


def test_environment_retains_gas_positions_without_full_geometry_and_never_promotes_coverage():
    clean={'first_index':0,'last_index':0,'environment':{'gases_0_255':{'ToxGas':0}}}
    gas={'first_index':1,'last_index':2,'first_cell':{'x':134,'z':124},'last_cell':{'x':135,'z':124},'environment':{'gases_0_255':{'ToxGas':180}}}
    unknown={'first_index':3,'last_index':4,'environment':{'gases_0_255':None}}
    raw={'tool':'inspect_route','data':{'path_environment':{'scope':'observed_path_cells','gases':[{'type':'ToxGas','cells':2}],
        'coverage':{'visibility_complete':False},'path_environment_bands':[clean,gas,unknown],'path_environment_bands_complete':False}}}
    saved=copy.deepcopy(raw)
    visible=json.loads(ContextPruner().compile(raw,arguments={'include':['environment']}).content)['data']['path_environment']
    assert visible['path_gas_bands']==[gas,unknown]
    assert visible['path_gas_bands_listing_complete'] is False and visible['coverage']['visibility_complete'] is False
    assert 'path_environment_bands' not in visible and raw==saved


@pytest.mark.asyncio
@pytest.mark.parametrize('native_selection', [True, False])
async def test_selected_state_forwards_only_negotiated_facets_without_extra_read(monkeypatch, native_selection):
    monkeypatch.setenv('NEKO_CONTEXT_PRUNING', '1')
    native = NativeInteraction()
    directory = state_directory('world_observation.v2.3')
    if native_selection:
        directory['world_observation']['tools'][0]['parameters']['properties']['facets'] = {'items': {'enum':list(STATE_FACETS)}}
    native.setup = SimpleNamespace(directory=directory)
    args = {'object':{'kind':'thing','id':'bed1'}, 'focus':{'state':['bed','reservations']},
            'aspects':['identity'], 'actor':{'kind':'pawn','id':'a'}, 'user':{'kind':'pawn','id':'patient'}}
    result = await ObjectReader(native).look(args)
    assert len(native.calls) == 1
    request = native.calls[0][1]
    assert ('facets' in request) is native_selection
    if native_selection:
        assert request['facets'] == ['bed','reservations']
    assert request['sections'] == ['state']  # Native target identity accompanies each state read.
    saved = copy.deepcopy(result)
    view = json.loads(ContextPruner().compile({'tool':'look','data':result},arguments=args).content)['data']
    state = view['facts'][0]['facts']['state']
    assert set(state['facets']) == {'bed','reservations'}
    assert state['facets']['bed']['medical_designation'] is True
    assert state['facets']['reservations']['status'] == 'not_observed'
    assert 'pawn' not in state['facets'] and result == saved
    assert view['state_observations'][0]['context'] == {'actor_id':'a','user_id':'patient'}
    assert view['state_facet_selection']['native_selected'] is native_selection


@pytest.mark.asyncio
async def test_matrix_preserves_full_three_by_three_capacity(monkeypatch):
    monkeypatch.setenv('NEKO_CONTEXT_PRUNING','1')
    native = NativeInteraction()
    actors = [{'kind':'pawn','id':x,'map':'Map_0'} for x in ['a','b','c']]
    patient = {'kind':'pawn','id':'patient','map':'Map_0'}
    beds = [{'kind':'thing','id':'bed'+str(i),'map':'Map_0'} for i in [1,2,3]]
    args = {'kind':'transfer','actors':actors,'subject':patient,'destinations':beds,'include':['approach','destination_state']}
    result = await make_activity_tool(native).handler(args)
    assert len(native.calls)==21 and len(result['observations'])==21
    assert sum(row['kind']=='approach' for row in result['observations'])==3
    assert sum(row['kind']=='transfer' for row in result['observations'])==9
    assert sum(row['kind']=='destination_state' for row in result['observations'])==9


def test_model_schema_preserves_matrix_and_rejects_withdrawn_batch(monkeypatch):
    import jsonschema
    from plugin.plugins.neko_rimworld.agent.context_tools import model_tools
    monkeypatch.setenv('NEKO_CONTEXT_PRUNING','1')
    tool=make_activity_tool(NativeInteraction())
    schema=model_tools([tool.schema()])[0]['function']['parameters']
    assert schema['type']=='object'  # Provider function arguments require an object root.
    pawn={'kind':'pawn','id':'a'}
    bed={'kind':'thing','id':'bed1'}
    jsonschema.validate({'kind':'state','actors':[pawn],'destinations':[bed],'focus':{'state':['bed']}},schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'requests':[{'kind':'state','actors':[pawn],'destinations':[bed]}]},schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'requests':[{'kind':'state','actors':[{'kind':'thing','id':'bed1'}],'destinations':[bed]}]},schema)
