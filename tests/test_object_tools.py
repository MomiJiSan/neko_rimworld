import copy
import pytest

from plugin.plugins.neko_rimworld.agent.object_tools import ObjectReader, state_contract, extend_look_schema
from plugin.plugins.neko_rimworld.agent.query_surface import SurfaceError
from plugin.plugins.neko_rimworld.agent.semantic_surface import route_object


class Reader:
    def __init__(self, rows, reply=None):
        self.rows=rows; self.calls=[]; self.reply=reply
    async def _look_legacy(self,args):
        self.calls.append(('query',copy.deepcopy(args)))
        return {'status':'ok','tool':'look','scope':{'original':copy.deepcopy(args['object'])},
                'coverage':{'matched':len(self.rows),'listing':'complete'},
                'facts':[{'object':copy.deepcopy(o),'facts':{'identity':{'object':copy.deepcopy(o)}}}for o in self.rows],
                'not_read':['aspects not requested: health, work']}
    async def execute_native(self,args,tool_name):
        self.calls.append((tool_name,copy.deepcopy(args)))
        if self.reply is not None:return copy.deepcopy(self.reply)
        return {'status':'ok','observation_id':str(len(self.calls)), 'issues':[], 'data':{
            'sample':{'started_tick':10,'finished_tick':11},'context':{'user_id':'patient'},
            'objects':[{'target':target,'facets':{
                'pawn':{'status':'observed','conditions':[{'severity':4}], 'skills':[{'definition':'Medicine','level':16,'aptitude':0}], 'skills_complete':True,'skills_total':1},
                'work':{'status':'observed','work_types':[{'priority':0}]},
                'stats':{'status':'observed','values':[{'value':4.4}]}},
                'coverage':{'registered_facets_read_successfully':True},'sample_tick':10}for target in args['targets']]}}


@pytest.mark.asyncio
async def test_known_objects_use_one_reader_and_internal_batching():
    adapter=Reader([{'id':str(i),'name':str(i),'map_id':'Map_1'}for i in range(9)])
    result=await ObjectReader(adapter).look({'object':{'kind':'pawns','ids':[str(i)for i in range(9)]},'aspects':['health','skills','work'],'stats':['MoveSpeed']})
    assert adapter.calls[0][1]['aspects']==['identity']
    states=[a for name,a in adapter.calls if name=='inspect_state'];assert list(map(lambda a:len(a['targets']),states))==[8,1]
    assert all(t['map']=='Map_1'for a in states for t in a['targets'])
    assert all(a['sections']==['health','skills','work'] and a['include_fogged']is True for a in states)
    assert len(result['facts'])==9 and len(result['state_observations'])==2
    assert result['facts'][0]['facts']['skills']['skills_total']==1
    assert 'skills_total'not in result['facts'][0]['facts']['health']
    assert result['facts'][0]['facts']['stats']['values'][0]['value']==4.4


@pytest.mark.asyncio
async def test_partial_prefix_and_reasons_survive_without_inventing_missing_rows():
    reply={'status':'partial','observation_id':'s','issues':[{'code':'deadline'}],'data':{
        'objects':[{'target':{'id':'a'},'facets':{'work':{'status':'unavailable'}},'coverage':{'registered_facets_read_successfully':False}}],
        'read_error':{'code':'deadline'},'changed_during_read':True,'sample':{'started_tick':1,'finished_tick':2}}}
    original=copy.deepcopy(reply)
    adapter=Reader([{'id':'a'},{'id':'b'}],reply)
    result=await ObjectReader(adapter).look({'object':{'kind':'pawns','ids':['a','b']},'aspects':['work']})
    assert result['status']=='partial' and result['coverage']['state_facets_read_successfully']is False
    assert result['facts'][0]['facts']['work']['status']=='unavailable'
    assert result['facts'][1]['state']['reason']=='state_read_incomplete'
    assert result['state_observations'][0]['changed_during_read']is True
    assert result['state_observations'][0]['issues']==[{'code':'deadline'}]
    assert reply==original


@pytest.mark.asyncio
async def test_cell_identity_uses_visible_native_identity_not_legacy_or_environment():
    adapter=Reader([],{'status':'ok','observation_id':'cell','data':{'objects':[{'target':{'kind':'cell','map':'Map_0','x':2,'z':3}}]}})
    result=await ObjectReader(adapter).look({'object':{'kind':'cell','x':2,'z':3},'aspects':['identity']})
    assert [n for n,a in adapter.calls]==['inspect_state']
    assert adapter.calls[0][1]['sections']==['identity'] and adapter.calls[0][1]['include_fogged']is False
    assert result['facts'][0]['facts']=={'identity':{'kind':'cell','map':'Map_0','x':2,'z':3}}


@pytest.mark.asyncio
async def test_plain_identity_does_not_enrich_state_and_bad_combinations_read_nothing():
    adapter=Reader([{'id':'a'}]);await ObjectReader(adapter).look({'object':{'kind':'thing','id':'a'},'aspects':['identity']})
    assert len(adapter.calls)==1
    for args in [
        {'object':{'kind':'things'},'aspects':['state','distribution']},
        {'object':{'kind':'pawn','id':'a'},'stats':[]},
        {'object':{'kind':'cell','x':True,'z':1}},
        {'object':{'kind':'map'},'stats':['MoveSpeed']},
    ]:
        adapter.calls.clear()
        with pytest.raises(SurfaceError):await ObjectReader(adapter).look(args)
        assert adapter.calls==[]


def test_capability_predicate_intersects_ids_before_aggregation():
    root,args,where=route_object({'kind':'things','ids':['x'],'capabilities':['medicine','sleeping_place']})
    assert root=='things'
    assert where=={'id':{'in':['x']}}
    assert args['capabilities']==['medicine','sleeping_place']


def test_output_page_limits_whole_objects_and_preserves_continuation_offset():
    original=[{'object':{'id':str(i)},'facts':{'work':{'large':'x'*22000}}}for i in range(8)]
    data={'facts':copy.deepcopy(original),'coverage':{'scan':'complete','listing':'partial','matched':40},
          'page':{'offset':10,'returned':8,'has_more':True,'next_offset':18},'not_read':[]}
    result=ObjectReader._fit_page(data)
    assert 0<len(result['facts'])<8
    assert result['facts']==original[:len(result['facts'])]
    assert result['page']['next_offset']==10+len(result['facts'])
    assert result['page']['reduced_for_size'] is True and result['coverage']['matched']==40


@pytest.mark.asyncio
async def test_legacy_quantity_can_accompany_state_without_an_extra_count_call():
    adapter=Reader([{"id":"p"}])
    result=await ObjectReader(adapter).look({"object":{"kind":"colonists"},"aspects":["health","quantity"]})
    assert adapter.calls[0][1]["aspects"]==["identity","quantity"]
    assert [name for name,args in adapter.calls]==["query","inspect_state"]
    assert "health"in result["facts"][0]["facts"]


@pytest.mark.asyncio
@pytest.mark.parametrize('obj,target,all_maps', [
    ({'kind':'pawn','name':'Alex'}, {'kind':'pawn','name':'Alex'}, True),
    ({'kind':'thing','id':'a'}, {'kind':'thing','id':'a'}, True),
    ({'kind':'thing','name':'bed'}, {'kind':'thing','name':'bed','map':'current'}, False),
    ({'kind':'pawn','id':'a','map':'Map_2'}, {'kind':'pawn','id':'a','map':'Map_2'}, False),
])
async def test_single_reference_resolves_and_reads_once_with_preserved_map_defaults(obj,target,all_maps):
    adapter=Reader([],{'status':'ok','observation_id':'s','data':{'objects':[{
        'target':{'id':'a','definition':'Human','map':'Map_2','name':'Alex','pawn_membership':{'is_colonist':True}},
        'facets':{'work':{'status':'observed'}},'coverage':{'registered_facets_read_successfully':True}}]}})
    result=await ObjectReader(adapter).look({'object':obj,'aspects':['state']})
    assert len(adapter.calls)==1 and adapter.calls[0][0]=='inspect_state'
    request=adapter.calls[0][1]
    assert request['targets']==[target] and request['exact_lookup'] is True
    assert result['facts'][0]['object']['id']=='a' and result['facts'][0]['object']['map_id']=='Map_2'
    assert result['facts'][0]['object']['is_colonist'] is True


@pytest.mark.asyncio
async def test_direct_ambiguous_reference_keeps_candidates_without_inventing_identity():
    adapter=Reader([],{'status':'partial','observation_id':'s','data':{'objects':[{
        'status':'unavailable','reason':'ambiguous_reference','resolution':{'candidates':[{'id':'a'},{'id':'b'}]}}]}})
    result=await ObjectReader(adapter).look({'object':{'kind':'pawn','name':'Alex'}})
    assert result['status']=='partial'
    assert result['facts'][0]['state']['resolution']['candidates']==[{'id':'a'},{'id':'b'}]
    assert 'id'not in result['facts'][0]['object'] and not result['facts'][0]['facts']


@pytest.mark.asyncio
async def test_canonical_location_unifies_direct_and_population_without_mutating_native():
    native={'status':'ok','observation_id':'native','data':{'objects':[{
        'target':{'id':'Thing_Human7','name':'Alex','kind':'pawn','definition':'Human','map':'Map_0',
                  'position':{'kind':'cell','map':'Map_0','x':10,'z':20},
                  'held_position':{'kind':'cell','map':'Map_0','x':10,'z':20},'spawned':True},
        'facets':{'pawn':{'status':'observed','conditions':[]}},
        'coverage':{'registered_facets_read_successfully':True}}]}}
    original=copy.deepcopy(native)
    legacy={'id':'Thing_Human7','name':'Alex','map_id':'Map_0',
            'status':{'position':{'map_id':'Map_0','x':10,'z':20}}}
    reader=Reader([legacy],native)
    direct=await ObjectReader(reader).look({'object':{'kind':'pawn','id':'Thing_Human7'},'aspects':['health']})
    grouped=await ObjectReader(reader).look({'object':{'kind':'pawns','ids':['Thing_Human7']},'aspects':['health']})
    identity=await ObjectReader(reader).look({'object':{'kind':'pawn','id':'Thing_Human7'},'aspects':['identity']})
    expected={'kind':'cell','map':'Map_0','x':10,'z':20}
    for result in [direct,grouped,identity]:
        obj=result['facts'][0]['object']
        assert obj['location']['position']==expected
        assert obj['status']['position']['map']=='Map_0'
        assert obj['status']['position']['map_id']=='Map_0'
    assert native==original
    assert grouped['facts'][0]['object']['location']['held_position']is None  # never invented


def test_canonical_location_never_substitutes_holder_position_for_own():
    raw={'id':'Thing_Item1','spawned':False,'position':{'x':-1000,'z':-1000},
         'map_held':{'id':'Map_0'},'held_position':{'map':'Map_0','x':3,'z':4}}
    result=ObjectReader._canonical_identity(raw)
    assert result['location']['position']is None
    assert result['location']['held_position']=={'kind':'cell','map':'Map_0','x':3,'z':4}
    assert result['position']==raw['position']


@pytest.mark.asyncio
async def test_id_like_unresolved_name_gets_hint_without_coercion():
    reply={'status':'partial','observation_id':'miss','data':{'objects':[{
        'status':'unavailable','reason':'object_unavailable','requested_target':{'name':'Thing_Bed7'}}]}}
    reader=Reader([],reply)
    result=await ObjectReader(reader).look({'object':{'kind':'thing','name':'Thing_Bed7'}})
    assert reader.calls[0][1]['targets']==[{'kind':'thing','name':'Thing_Bed7','map':'current'}]
    assert result['input_hints'][0]['code']=='possible_id_in_name'
    assert result['input_hints'][0]['suggested_field']=='object.id'
    assert not result['facts'][0]['facts']
    assert 'id'not in result['facts'][0]['object']


@pytest.mark.asyncio
async def test_id_like_real_display_name_keeps_its_resolved_identity():
    reply={'status':'ok','observation_id':'match','data':{'objects':[{
        'target':{'id':'Thing_Actual9','name':'Thing_Bed7','definition':'Bed','map':'Map_0'},
        'facets':{'bed':{'status':'observed'}},'coverage':{'registered_facets_read_successfully':True}}]}}
    result=await ObjectReader(Reader([],reply)).look({'object':{'kind':'thing','name':'Thing_Bed7'}})
    assert result['facts'][0]['object']['id']=='Thing_Actual9'
    assert not result.get('input_hints')


@pytest.mark.asyncio
async def test_unknown_stat_discovery_and_observed_sibling_are_preserved():
    stats={'status':'partial','complete':False,'values':[
        {'definition':'MoveSpeed','value':4.4,'status':'observed'},
        {'definition':'Moving','status':'unknown_definition','discovery':{
            'definition_kind':'StatDef','matches':[], 'truncated':False,
            'matching_definition_kind':'PawnCapacityDef','matching_definition':'Moving',
            'read_from':'health.capacities'}}]}
    reply={'status':'partial','observation_id':'stats','data':{'objects':[{
        'target':{'id':'p','name':'Alex','definition':'Human','map':'Map_0'},
        'facets':{'stats':stats},'coverage':{'registered_facets_read_successfully':False}}]}}
    result=await ObjectReader(Reader([],reply)).look({'object':{'kind':'pawn','id':'p'},'stats':['MoveSpeed','Moving']})
    assert result['facts'][0]['facts']['stats']==stats
    assert result['status']=='partial'
    assert 'stats'not in result['facts'][0]['facts']['state']['facets']  # one canonical stats location
