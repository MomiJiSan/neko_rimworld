"""T1 negotiated native lookup narrows candidates without changing ambiguity."""
import copy

from plugin.plugins.neko_rimworld.agent.semantic_surface import lookup_spec, apply_lookup


class RecordingSurface:
    def __init__(self, supported):
        self.argument_schemas={'thing_defs':{'properties':{'exact_names':{}}if supported else {}}}
    def compile(self, plan):return copy.deepcopy(plan)


def test_exact_definition_lookup_uses_negotiated_index_and_retains_label_union():
    obj={'kind':'things','definition':'Bed'}
    indexed=lookup_spec(RecordingSurface(True),obj)
    legacy=lookup_spec(RecordingSurface(False),obj)
    block=indexed['request']['blocks'][0]
    assert block['args']=={'exact_names':['Bed']}
    assert 'args'not in legacy['request']['blocks'][0]
    assert block['where']==legacy['request']['blocks'][0]['where']=={
        'any':[{'def_name':{'eq':'Bed','case_sensitive':False}},
               {'label':{'eq':'Bed','case_sensitive':False}}]}
    assert apply_lookup(obj,indexed,{'status':'ambiguous','ids':['Bed','OtherLabelBed']})is None
    assert apply_lookup(obj,indexed,{'status':'incomplete','ids':['Bed']})is None
    assert apply_lookup(obj,indexed,{'status':'resolved','ids':['Bed']})['definition']=='Bed'


def test_capability_selection_schema_includes_native_meanings():
    from plugin.plugins.neko_rimworld.agent.semantic_surface import _object_schema
    directory={'roots':[{'from':'things','type':'Thing','args':[{'name':'capabilities'}]}],
               'field_index':{'Thing':{'capabilities':{}}},
               'world_observation':{'capability_meanings':{'production':'IBillGiver, including Pawn surgery bills'},
                   'tools':[{'name':'survey_space','parameters':{'properties':{'select':{'properties':{
                       'capabilities':{'items':{'enum':['production','building']}}}}}}}]}}
    schema=_object_schema(directory)
    assert schema['properties']['capabilities']['items']['enum']==['production','building']
    assert 'Pawn surgery bills' in schema['properties']['capabilities']['description']
