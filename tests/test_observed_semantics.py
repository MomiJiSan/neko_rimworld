import asyncio
import copy
import pytest
from plugin.plugins.neko_rimworld.agent.semantic_tools import _with_field_semantics, semantic_interaction_factory
from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError
from plugin.plugins.neko_rimworld.query_tools import make_game_tools
from .test_semantic_tools import public_source,Session,_update


def test_measure_units_and_trait_codes_stay_with_values_without_mutating_evidence():
    data={'facts':[{'object':{'id':'p'},'facts':{'health':{'summary':{'health_percent':.92,'bleed_rate':.36,'pain':None}},'profile':{'traits':{'rows':[{'degree':2,'description':'Move speed +0.4 c/s','suppressed':False}]}}}}]}
    before=copy.deepcopy(data); result=_with_field_semantics(data)
    summary=result['facts'][0]['facts']['health']['summary']
    assert summary['bleed_rate']==.36 and summary['pain'] is None
    assert summary['field_semantics']['bleed_rate']['unit']=='blood_loss_fraction_per_game_day'
    assert 'not blood' in summary['field_semantics']['health_percent']['meaning']
    traits=result['facts'][0]['facts']['profile']['traits']
    assert traits['rows'][0]['degree']==2 and 'variant code' in traits['field_semantics']['degree']
    assert data==before
    unknown={'facts':[{'facts':{'health':{'summary':{'state':'unavailable','reason':'not captured'}}}}]}
    assert _with_field_semantics(unknown)==unknown


def test_pawn_target_alias_preserves_target_position_and_touch_endpoint():
    from plugin.plugins.neko_rimworld.observation import Observation
    from plugin.plugins.neko_rimworld.protocol import QUERY_RESULT,Payload,SessionDescriptor,make_envelope
    source=public_source(spatial=True);cap=source['spatial_query'];cap['route_summary']='native_path_summary.v1'
    cap['parameters']['properties']['detail']={'type':'string','enum':['summary','route']}
    requests=[]; target_kind=['pawn']
    original={'answer':'reachable','from':{'kind':'pawn','id':'actor'},'target':{'kind':'pawn','id':'patient','position':{'map':'Map_0','x':140,'z':124}},'route':{'status':'found','end_mode':'Touch','end':{'map':'Map_0','x':141,'z':123},'path_length':{'value':9.24,'unit':'map_cell_widths'}}}
    async def backend(request):
        requests.append(request)
        data=copy.deepcopy(original);data['target']['kind']=target_kind[0]
        env=make_envelope(QUERY_RESULT,'1'*32,len(requests),payload=Payload(status='ok',data=data,issues=[]),session=SessionDescriptor('2'*32,'3'*32,8,1),request_id='4'*32)
        return Observation.from_response('spatial_query',request['arguments'],env)
    session=Session();adapter=semantic_interaction_factory(make_game_tools(source,backend),session,_update)
    args={'from':{'kind':'pawn','id':'actor'},'to':{'kind':'pawn','id':'patient'},'detail':'route'}
    result=asyncio.run(adapter.inspect_space(args))
    assert args['to']['kind']=='pawn' and requests[0]['arguments']['to']['kind']=='thing'
    assert result['to']['position']['x']==140 and result['route']['end']['x']==141
    assert 'not the target' in result['route']['endpoint_meaning']
    assert 'endpoint_meaning' not in session.observations[0]['response']['payload']['data']['route']
    target_kind[0]='thing'
    with pytest.raises(ToolExecutionError,match='not a Pawn'):
        asyncio.run(adapter.inspect_space(args))
    target_kind[0]='pawn'
    batch=asyncio.run(adapter.inspect_space({**args,'to':[args['to']]}))
    assert batch['results'][0]['requested_target']['kind']=='pawn'
