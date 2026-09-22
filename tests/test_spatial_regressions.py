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
