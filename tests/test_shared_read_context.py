import copy
import json

import jsonschema
import pytest

from plugin.plugins.neko_rimworld.agent.context_pruning import ContextPruner, _record_view, prune
from plugin.plugins.neko_rimworld.agent.schema_compaction import remove_redundant_constraints


def environment_read():
    shared = {'map_context': {'weather':'Clear','active_conditions':[]},
              'rooms':[{'id':0,'outdoors':True,'temperature_c':23.0}],
              'path_cell_coverage':{'complete':False,'missing_index_ranges':[[1,2]]},
              'path_gas_bands':[{'first_index':1,'last_index':2,'environment':{'gases_0_255':{'ToxGas':180}}}]}
    return {'tool':'inspect_route','data':{
        'sample':{'load_epoch':'world','started_tick':8,'finished_tick':9},
        'context':{'actor_id':'Near','user_id':'Patient'},
        'environment':{**copy.deepcopy(shared),'scope':'corridor','observed_cells':48,
                       'gases':[{'type':'ToxGas','cells':2}], 'coverage':{'complete':False}},
        'path_environment':{**copy.deepcopy(shared),'scope':'path','observed_cells':8,
                            'gases':[], 'coverage':{'complete':True}},
        'extension':{'environment':copy.deepcopy(shared)}}}


def test_same_read_environment_shares_only_exact_fields_without_merging_scopes_or_hazards():
    raw=environment_read();saved=copy.deepcopy(raw)
    result=_record_view(raw,{},[])['data']
    shared=result['environment_shared']
    assert shared['path_cell_coverage']==raw['data']['environment']['path_cell_coverage']
    for name in ('environment','path_environment'):
        reconstructed={**shared,**result[name]}
        assert reconstructed==raw['data'][name]
    assert result['environment']['scope']=='corridor' and result['path_environment']['scope']=='path'
    assert result['environment']['gases']!=result['path_environment']['gases']
    assert result['extension']==raw['data']['extension'] and raw==saved


@pytest.mark.parametrize('change',['different','missing','null','existing_shared'])
def test_environment_uncertainty_and_different_sample_content_are_not_hoisted(change):
    raw=environment_read()
    if change=='different':raw['data']['path_environment']['path_cell_coverage']['complete']=True
    elif change=='missing':raw['data']['path_environment'].pop('path_cell_coverage')
    elif change=='null':raw['data']['path_environment']['path_cell_coverage']=None
    else:raw['data']['environment_shared']={'extension':'independent'}
    result=_record_view(raw,{},[])['data']
    if change=='existing_shared':assert result['environment_shared']=={'extension':'independent'}
    else:assert 'path_cell_coverage' not in result['environment_shared']
    if change=='missing':assert 'path_cell_coverage' not in result['path_environment']
    else:assert result['path_environment']['path_cell_coverage']==raw['data']['path_environment']['path_cell_coverage']


def test_activity_world_inheritance_preserves_every_distinct_read_actor_stage_and_sample():
    rows=[{'kind':'transfer','actor':{'id':actor},'destination':{'id':'bed'},'observation':{
        'colony_id':'colony','load_epoch':'world','connection_epoch':'connection',
        'observation_id':'read-'+actor,'sample':{'started_tick':tick,'finished_tick':tick+1},
        'context':{'actor_id':actor,'user_id':'Patient'},'native_cost':cost}}
        for actor,tick,cost in [('Near',8,440),('Middle',9,412)]]
    raw={'tool':'evaluate_activity','data':{'observations':rows}};saved=copy.deepcopy(raw)
    result=_record_view(raw,{},[])['data']
    for original,row in zip(rows,result['observations']):
        assert row['kind']==original['kind'] and row['actor']==original['actor']
        rebuilt={**{key:result[key] for key in ('colony_id','load_epoch','connection_epoch')},**row['observation']}
        assert rebuilt==original['observation']
    assert raw==saved
    raw['data']['observations'][1]['observation']['load_epoch']='other-world'
    mixed=_record_view(raw,{},[])['data']
    assert 'load_epoch' not in mixed
    assert [row['observation']['load_epoch'] for row in mixed['observations']]==['world','other-world']


def test_nested_equal_sample_and_context_inherit_only_exact_ancestor_values():
    sample={'game_tick':8,'atomic':False};context={'actor_id':'Near','user_id':None}
    raw={'sample':sample,'context':context,'facts':[{'sample':copy.deepcopy(sample),'context':copy.deepcopy(context),
          'value':0},{'sample':{'game_tick':9,'atomic':False},'context':{'actor_id':'Middle','user_id':None},'value':None}]}
    result=_record_view(raw,{},[])
    assert 'sample' not in result['facts'][0] and 'context' not in result['facts'][0]
    assert result['facts'][0]['value']==0
    assert result['facts'][1]==raw['facts'][1]


def test_native_activity_read_options_use_the_same_shared_projection_as_captured_results():
    raw={'tool':'evaluate_activity','data':{'observations':[
        {'kind':'transfer','read_options':{'include':['environment']},'observation':environment_read()['data']}]}}
    saved=copy.deepcopy(raw)
    visible=json.loads(ContextPruner().compile(raw).content)['data']['observations'][0]['observation']
    assert 'environment_shared' in visible
    assert visible['environment']['scope']=='corridor' and visible['path_environment']['scope']=='path'
    assert raw==saved


def test_schema_reduction_keeps_accepted_values_and_recursive_references():
    raw={'type':'object','required':['kind'],'additionalProperties':False,
         'properties':{'kind':{'type':'string','enum':['pawn','thing']},'value':{'$ref':'#/$defs/node'}},
         '$defs':{'node':{'type':'object','properties':{'child':{'$ref':'#/$defs/node'}}},
                  'unused':{'type':'string','minLength':100}}}
    saved=copy.deepcopy(raw);short=remove_redundant_constraints(raw)
    assert set(short['$defs'])=={'node'} and 'type' not in short['properties']['kind']
    for value in ({'kind':'pawn'}, {'kind':'thing','value':{'child':{}}}, {'kind':1}, {'kind':True},
                  {'kind':'unknown'}, {}, {'kind':'pawn','unexpected':1}):
        assert jsonschema.Draft202012Validator(raw).is_valid(value)==jsonschema.Draft202012Validator(short).is_valid(value)
    assert raw==saved


@pytest.mark.parametrize('marker',[{'$id':'https://example.invalid/schema'}, {'$anchor':'node'},
                                  {'$dynamicRef':'#node'}, {'$ref':'#/$defs/node/properties/value'}])
def test_scoped_and_interior_schema_definitions_are_retained(marker):
    raw={**marker,'$defs':{'node':{'type':'object','properties':{'value':{'type':'string'}}},'unused':{'type':'integer'}}}
    assert remove_redundant_constraints(raw)==raw


def test_mismatched_enum_type_is_not_weakened():
    raw={'type':'integer','enum':[True,1]}
    assert remove_redundant_constraints(raw)==raw


def test_identity_copies_stay_within_one_read_and_conflicting_versions_remain_full():
    identity={'id':'Pawn1','kind':'pawn','name':'Near','position_xz':[1,2],'map':'Map0'}
    raw={'route':{'actor':copy.deepcopy(identity)},'environment':{'local_hazards':[{'source':copy.deepcopy(identity)}]},
         'extension':{'actor':copy.deepcopy(identity)}}
    saved=copy.deepcopy(raw)
    result=_record_view(raw,{},[])
    assert result['route']['actor']==identity
    assert result['environment']['local_hazards'][0]['source']=={'id':'Pawn1'}
    assert result['extension']==saved['extension'] and raw==saved
    raw['environment']['local_hazards'][0]['source']['position_xz']=[3,4]
    conflict=_record_view(raw,{},[])
    assert conflict['route']['actor']==identity and conflict['environment']['local_hazards'][0]['source']['position_xz']==[3,4]
    matrix={'tool':'evaluate_activity','data':{'observations':[
        {'observation':saved},{'observation':raw}]}}
    separate=_record_view(matrix,{},[])['data']['observations']
    assert all(row['observation']['route']['actor']==identity for row in separate)


def test_delivery_alias_is_removed_only_when_all_values_match_the_actual_sample():
    raw={'sample':{'started_tick':8,'finished_tick':8,'same_tick':True,'snapshot_sequence':3,'atomic':False},
         'delivery_sample':{'game_tick':8,'snapshot_sequence':3}}
    result=_record_view(raw,{},[])
    assert 'delivery_sample' not in result and 'same_tick' not in result['sample']
    assert result['sample']['atomic'] is False and result['sample']['finished_tick']==8
    raw['delivery_sample']['game_tick']=9
    assert _record_view(raw,{},[])['delivery_sample']==raw['delivery_sample']
    raw['sample']['same_tick']=False
    assert _record_view(raw,{},[])['sample']['same_tick'] is False


def test_grouped_unknown_nulls_keep_exact_fields_and_failure_reasons():
    raw={'cost':None,'eligible':None,'density':0,
         'field_states':{'cost':{'status':'unavailable','reason':'native_timeout'},
                         'eligible':{'status':'unavailable','reason':'native_timeout'},
                         'density':{'status':'observed','reason':'native_value'}},
         'coverage':{'complete':False},'context':{'actor_id':'Near','user_id':'Patient'}}
    saved=copy.deepcopy(raw);result=_record_view(raw,{},[])
    assert result['field_states_by_status']=={'unavailable':{'native_timeout':['cost','eligible']}}
    assert result['density']==0 and result['coverage']==raw['coverage'] and result['context']==raw['context']
    assert 'cost' not in result and 'eligible' not in result and raw==saved
    raw['field_states_by_status']={'not_applicable':{'no_hp':['hit_points']}}
    merged=_record_view(raw,{},[])
    assert merged['field_states_by_status']=={'not_applicable':{'no_hp':['hit_points']},
                                             'unavailable':{'native_timeout':['cost','eligible']}}
