"""Regression contracts from the V2.1 eight-case trace, without model calls."""
import copy
import pytest
from plugin.plugins.neko_rimworld.agent import semantic_surface as surface
from plugin.plugins.neko_rimworld.agent.semantic_tools import SemanticInteraction


def test_blocked_definition_lookup_never_certifies_instance_count():
    native = {'observation_id': 'definition-probe', 'data': {'results': [{
        'from': 'thing_defs', 'state': 'complete', 'fields': 'complete', 'listing': 'complete',
        'matched': 0, 'known_matches': 0, 'rows': [],
        'scope': {'args': {}, 'notes': ['Loaded definitions, not physical quantities.']},
    }]}, 'issues': []}
    before = copy.deepcopy(native)
    resolution = {'status': 'missing_in_scope', 'field': 'def_name'}
    output = surface.render_blocked('count', {'kind':'things','definition':'Thing_Silver2520','map':'Map_0'}, resolution, native)
    assert output['lookup']['source'] == 'thing_defs'
    assert output['lookup']['scope']['args'] == {}
    assert output['lookup']['coverage']['matched'] == 0
    assert output['coverage']['scan'] == 'not_run'
    assert output['coverage']['matched'] is None
    assert output['execution'] == 'not_run' and output['measures'] == {}
    assert 'not executed' in output['explanation']
    assert native == before


def test_pawn_map_and_single_id_route_without_dropping_scope():
    obj = surface.normalize_object({'kind':'pawn','id':'pawn1','map':'map2'})
    root,args,where = surface.route_object(obj)
    assert root == 'pawns' and args == {'map_id':'map2'}
    assert where == {'id':{'in':['pawn1']}}
    class Compiler:
        def compile(self, request): return request
    name = surface.normalize_object({'kind':'pawn','name':'Alice','map':'map2'})
    lookup = surface.lookup_spec(Compiler(),name)
    assert lookup['request']['blocks'][0]['args'] == {'map_id':'map2'}
    with pytest.raises(surface.SurfaceError):
        surface.normalize_object({'kind':'pawn','id':'a','ids':['b']})


@pytest.mark.parametrize('value',[
    {'kind':'cell','x':True,'z':1}, {'kind':'cell','x':2**40,'z':1},
    {'kind':'thing','id':''}, {'kind':'thing','id':17},
    {'kind':'thing','id':'x','name':'x'}, {'kind':'thing','name':'x','map':None},
])
def test_invalid_spatial_reference_is_rejected_before_backend(value):
    with pytest.raises(surface.SurfaceError):
        SemanticInteraction._validate_space_ref(value,'to',{'thing','cell'})


def test_look_schema_rejects_wrong_kind_aspect_before_guessing():
    from jsonschema import validate, ValidationError
    schema = surface.tool_schemas()[0]['parameters']
    validate({'object':{'kind':'pawn','id':'p','map':'m'},'aspects':['work']},schema)
    validate({'object':{'kind':'thing','id':'t'},'aspects':['identity']},schema)
    with pytest.raises(ValidationError):
        validate({'object':{'kind':'thing','id':'t'},'aspects':['work']},schema)


def test_native_pack_preserves_actual_lookup_root_and_scope():
    packed=surface.pack({'results':[{'from':'thing_defs','scan_complete':True,'fields_complete':True,'matched_count':0,'known_matches':0,'rows':[], 'page':{'offset':0,'returned':0,'has_more':False},'scope':{'args':{},'description':'Loaded definitions, not quantities.'}}]})
    result=surface.render_blocked('count',{'kind':'things','definition':'Thing_Silver2520'}, {'field':'def_name','status':'missing_in_scope'}, {'data':packed})
    assert result['lookup']['source']=='thing_defs'
    assert result['lookup']['scope']['notes']==['Loaded definitions, not quantities.']
    assert result['execution']=='not_run'


def test_route_is_one_dispatch_and_partial_keeps_reachability():
    import asyncio
    from .test_semantic_tools import public_source, Session, _update
    from plugin.plugins.neko_rimworld.query_tools import make_game_tools
    from plugin.plugins.neko_rimworld.agent.semantic_tools import semantic_interaction_factory
    from plugin.plugins.neko_rimworld.observation import Observation
    from plugin.plugins.neko_rimworld.protocol import QUERY_RESULT, Payload, SessionDescriptor, make_envelope
    source=public_source(spatial=True)
    cap=source['spatial_query']; cap['route_summary']='native_path_summary.v1'
    cap['parameters']['properties']['detail']={'type':'string','enum':['summary','route']}
    requests=[]
    async def backend(request):
        requests.append(request)
        data={'answer':'reachable','route':{'status':'unavailable','reason':'deadline_exceeded'},'checked':{'native_reachability':True}}
        response=make_envelope(QUERY_RESULT,'1'*32,1,payload=Payload(status='partial',data=data,issues=[{'code':'deadline_exceeded','path':['route'],'message':'path unavailable'}]),session=SessionDescriptor('2'*32,'3'*32,42,1),request_id='4'*32)
        return Observation.from_response(request['operation'],request['arguments'],response)
    session=Session();adapter=semantic_interaction_factory(make_game_tools(source,backend),session,_update)
    spec=next(t for t in adapter.tools() if t.name=='inspect_space')
    assert 'detail="route"' in spec.description
    args={'from':{'kind':'pawn','name':'Alice'},'to':{'kind':'thing','id':'t'},'detail':'route'}
    result=asyncio.run(spec.handler(args))
    assert requests==[{'operation':'spatial_query','arguments':args}]
    assert result['answer']=='reachable' and result['route']['status']=='unavailable'
    assert len(session.observations)==1


def test_old_spatial_mod_does_not_advertise_or_dispatch_route():
    import asyncio
    from .test_semantic_tools import setup_for, Session, _update
    from plugin.plugins.neko_rimworld.agent.semantic_tools import semantic_interaction_factory
    adapter=semantic_interaction_factory(setup_for(spatial=True),Session(),_update)
    spec=next(t for t in adapter.tools() if t.name=='inspect_space')
    assert 'detail' not in spec.parameters['properties']
    from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError
    with pytest.raises(ToolExecutionError):
        asyncio.run(spec.handler({'from':{'kind':'pawn','id':'p'},'to':{'kind':'thing','id':'t'},'detail':'route'}))


def test_map_alias_routes_to_map_identity_without_scope_conflict():
    obj=surface.normalize_object({'kind':'map','map':'Map_0'})
    assert surface.route_object(obj)==('map',{'id':'Map_0'},None)
    assert surface.route_object(surface.normalize_object({'kind':'map','id':'Map_0'}))==surface.route_object(obj)
    with pytest.raises(surface.SurfaceError):
        surface.normalize_object({'kind':'map','map':'Map_0','id':'Map_1'})


def test_route_adapter_displays_along_path_length_without_mutating_observation():
    import asyncio
    from .test_semantic_tools import setup_for, Session, _update
    from plugin.plugins.neko_rimworld.agent.semantic_tools import semantic_interaction_factory
    adapter=semantic_interaction_factory(setup_for(spatial=True),Session(),_update)
    data={'answer':'reachable','route':{'status':'found','grid_distance':{'value':238.9066,'unit':'map_cell_widths'},'steps':195}}
    saved=copy.deepcopy(data)
    async def native(*args,**kwargs): return {'data':data,'issues':[]}
    adapter.execute_native=native
    output=asyncio.run(adapter.inspect_space({'from':{'kind':'pawn','id':'p'},'to':{'kind':'thing','id':'t'}}))
    assert output['route']['path_length']['value']==238.9066
    assert 'not straight-line' in output['route']['path_length']['meaning']
    assert 'grid_distance' not in output['route']
    assert data==saved


def test_map_alias_through_real_look_handler():
    import asyncio
    from .test_semantic_tools import public_source, Session, _update
    from plugin.plugins.neko_rimworld.query_tools import make_game_tools
    from plugin.plugins.neko_rimworld.agent.semantic_tools import semantic_interaction_factory
    from plugin.plugins.neko_rimworld.observation import Observation
    from plugin.plugins.neko_rimworld.protocol import QUERY_RESULT, Payload, SessionDescriptor, make_envelope
    source=public_source();p=source['public_query']
    p['roots'].append({'from':'map','type':'Map','description':'One map'})
    p['field_index']['Map']={'id':'String','name':'String'}
    p['parameters']['properties']['requests']['items']['allOf'][0]['oneOf'].append({'properties':{'from':{'const':'map'},'args':{'type':'object','properties':{'id':{'type':'string'}}}}})
    calls=[]
    async def backend(request):
        calls.append(request)
        data={'results':[{'from':'map','scan_complete':True,'fields_complete':True,'matched_count':1,'known_matches':1,'rows':[{'id':'Map_0','name':'Home'}],'page':{'offset':0,'returned':1,'has_more':False},'scope':{'args':{'id':'Map_0'}}}]}
        env=make_envelope(QUERY_RESULT,'1'*32,1,payload=Payload(status='ok',data=data,issues=[]),session=SessionDescriptor('2'*32,'3'*32,1,1),request_id='4'*32)
        return Observation.from_response(request['operation'],request['arguments'],env)
    adapter=semantic_interaction_factory(make_game_tools(source,backend),Session(),_update)
    result=asyncio.run(adapter.look({'object':{'kind':'map','map':'Map_0'},'aspects':['identity']}))
    assert len(calls)==1 and calls[0]['arguments']['from']=='map'
    assert calls[0]['arguments']['args']=={'id':'Map_0'}
    assert result['facts'][0]['object']['id']=='Map_0'
