import copy
import json

from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger
from plugin.plugins.neko_rimworld.agent.contracts import AssistantTurn, ToolCall
from plugin.plugins.neko_rimworld.agent.evidence_view import deduplicate_program_result, review_evidence
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession


def test_named_schema_definitions_are_factored_without_changing_valid_calls():
    import jsonschema
    from plugin.plugins.neko_rimworld.agent.schema_compaction import compact_schema, canonical
    ref = {'type':'object','additionalProperties':False,'required':['id'],
           'properties':{'id':{'type':'string','minLength':1,'maxLength':256},'map':{'type':'string','minLength':1,'maxLength':256}}}
    activity = {'type':'object','required':['actor','target'],'additionalProperties':False,
                'properties':{'actor':copy.deepcopy(ref),'target':copy.deepcopy(ref),'recipient':copy.deepcopy(ref)}}
    raw = {'type':'object','oneOf':[{'$ref':'#/$defs/activity'},
           {'type':'object','required':['requests'],'additionalProperties':False,
            'properties':{'requests':{'type':'array','minItems':1,'items':{'$ref':'#/$defs/activity'}}}}],
           '$defs':{'activity':activity}}
    saved=copy.deepcopy(raw);short=compact_schema(raw)
    assert len(canonical(short)) < len(canonical(raw)) and raw==saved
    for value in ({'actor':{'id':'a'},'target':{'id':'b'}},
                  {'requests':[{'actor':{'id':'a'},'target':{'id':'b'},'recipient':{'id':'p'}}]},
                  {'actor':{'id':''},'target':{'id':'b'}}, {'requests':[]},
                  {'actor':{'id':'a','extra':1},'target':{'id':'b'}},
                  {'requests':[{'actor':{'id':'a'},'target':{'id':'b'}}],'extra':1}):
        assert jsonschema.Draft202012Validator(raw).is_valid(value)==jsonschema.Draft202012Validator(short).is_valid(value)
    interior={'type':'object','$defs':{'activity':activity},'properties':{'id':{'$ref':'#/$defs/activity/properties/actor/properties/id'}}}
    assert compact_schema(interior)==interior


def test_unrequested_anatomy_filter_also_handles_named_capacity_views():
    from plugin.plugins.neko_rimworld.agent.context_pruning import prune
    parts=[{'index':0,'health':34,'max_health':40}, {'index':1,'health':25,'max_health':25}]
    raw={'tool':'look','data':{'facts':[{'object':{'id':'p'},'facts':{'health':{
        'conditions':[{'definition':'Cut','part':0}], 'damaged_present_parts':[parts[0]],
        'missing_parts':[], 'capacities_by_definition':{'Moving':{'level':0}},
        'body_parts':parts,'body_parts_total':64,'body_parts_complete':False}}}]}}
    saved=copy.deepcopy(raw);view=prune(raw,{'aspects':['health']})[0]['data']['facts'][0]['facts']['health']
    assert raw==saved and 'body_parts' not in view and view['condition_part_details']==[parts[0]]
    assert view['conditions']==raw['data']['facts'][0]['facts']['health']['conditions']
    assert view['capacities_by_definition']=={'Moving':{'level':0}}
    full=prune(raw,{'aspects':['health'],'include':['body_parts']})[0]['data']['facts'][0]['facts']['health']
    assert full['body_parts']==parts and full['body_parts_complete'] is False


def test_grouped_null_states_retain_reasons_roles_unknowns_and_zero():
    from plugin.plugins.neko_rimworld.agent.context_pruning import _record_view
    raw={'context':{'actor_id':'Near','user_id':'Patient'},'status':'partial',
         'sample':{'load_epoch':'world','game_tick':8},'hit_points':None,'max_hit_points':None,
         'field_states':{'hit_points':{'status':'not_applicable','reason':'no_hp'},
                         'max_hit_points':{'status':'not_applicable','reason':'no_hp'}},
         'gases':[{'type':'ToxGas','value':180}],'zero':0,'unknown':{'status':'unavailable','reason':'deadline'},
         'occupants':[],'extension':{'status':'ok','native_type':'Keep','received_at':'Keep'},
         'semantics':['Unknown extension meaning must stay']}
    saved=copy.deepcopy(raw);short=_record_view(raw,{},[])
    assert raw==saved and short['field_states_by_status']=={'not_applicable':{'no_hp':['hit_points','max_hit_points']}}
    for k in ('context','sample','status','gases','zero','unknown','occupants','extension','semantics'):
        assert short[k]==raw[k]
    assert _record_view(raw,{'include':['diagnostics']},[])==short


def test_body_keys_reconstruct_from_native_owner_without_dropping_anatomy():
    from plugin.plugins.neko_rimworld.agent.context_pruning import _record_view
    parts=[{'key':'Thing_P/body:Human/part:0','index':0,'health':34,'max_health':40},
           {'key':'Thing_P/body:Human/part:1','index':1,'health':None,'max_health':25}]
    raw={'body_parts':parts,'body_parts_complete':False};saved=copy.deepcopy(raw)
    short=_record_view(raw,{'include':['body_parts']},[])
    assert raw==saved and short['body_parts_owner']=={'pawn_id':'Thing_P','body_definition':'Human'}
    restored=[{'key':'Thing_P/body:Human/part:'+str(r['index']),**r} for r in short['body_parts']]
    assert restored==parts and short['body_parts_complete'] is False


def test_minimal_schema_prose_keeps_all_contracts_and_literal_property_names():
    from plugin.plugins.neko_rimworld.agent.schema_compaction import schema_contract

    schema = {"type": "object", "description": "Long annotation", "title": "Title", "examples": [{}],
              "properties": {"description": {"type": "string", "description": "A real argument"},
                             "actor": {"$ref": "#/$defs/person"}, "user": {"$ref": "#/$defs/person"}},
              "$defs": {"person": {"type": "object", "properties": {"id": {"type": "string"}},
                                   "required": ["id"], "additionalProperties": False}},
              "required": ["actor"], "additionalProperties": False,
              "if": {"required": ["user"]}, "then": {"required": ["description"]}}
    expected = copy.deepcopy(schema)
    for key in ("description", "title", "examples"):
        expected.pop(key)
    expected["properties"]["description"].pop("description")
    assert schema_contract(schema) == expected
    assert schema["properties"]["description"]["description"] == "A real argument"


def test_aggressive_record_view_keeps_risk_values_unknowns_roles_and_extensions():
    from plugin.plugins.neko_rimworld.agent.context_pruning import prune

    position = {"map": "Map_0", "x": 135, "z": 133}
    raw = {"tool": "inspect_route", "data": {"map": "Map_0", "sample": {"load_epoch": "load", "game_tick": 8},
        "context": {"actor_id": "Doctor", "user_id": "Patient"},
        "route": {"status": "found", "start": {"map": "Map_0", "x": 1, "z": 2},
                  "native_cost": {"value": 414, "unit": "native_path_cost"}},
        "path_environment": {"local_hazards_total": 0, "attack_influences": [{"path_indices_count": 27, "status": "partial",
            "source": {"id": "Enemy", "kind": "pawn", "name": "Enemy", "runtime_type": "Verse.Pawn",
                       "map": "Map_0", "position": position, "held_position": position, "position_kind": "own",
                       "faction": {"name": "Ancients", "is_player": False}}}],
            "coverage": {"native_attack_checks_complete": False, "not_modeled": ["future enemy movement"]}},
        "active_classification": {"status": "unavailable", "reason": "unsupported_behavior_patch", "value": None},
        "extension": {"runtime_type": "Custom", "map": "Other", "meaning": "Preserve unknown extension"}}}
    saved = copy.deepcopy(raw)
    projected, _, _ = prune(raw, {"include": ["environment"]})
    data = projected["data"]
    assert raw == saved and data["map"] == "Map_0"
    assert data["sample"] == raw["data"]["sample"] and data["context"] == raw["data"]["context"]
    assert data["route"]["native_cost"] == {"value": 414, "unit": "native_path_cost"}
    assert data["path_environment"]["local_hazards_total"] == 0
    assert data["path_environment"]["attack_influences"][0]["path_indices_count"] == 27
    assert data["path_environment"]["coverage"] == raw["data"]["path_environment"]["coverage"]
    assert data["active_classification"] == raw["data"]["active_classification"]
    assert data["extension"] == raw["data"]["extension"]
    assert data["route"]["start_xz"] == [1, 2] and "start_map" not in data["route"]


def test_named_values_preserve_nulls_duplicate_definitions_and_coordinates():
    from plugin.plugins.neko_rimworld.agent.context_pruning import prune

    raw = {"tool": "look", "data": {"facts": [{"object": {"id": "P", "map": "A", "position": {"x": 2, "z": 3, "map": "B"}},
        "facts": {"skills": {"skills": [{"definition": "Medicine", "level": 0, "passion": None}], "skills_complete": False},
                  "health": {"capacities": [{"definition": "Moving", "level": None}, {"definition": "Moving", "level": 0}]}}}]}}
    out, _, _ = prune(raw, {"aspects": ["identity", "skills", "health"]})
    row = out["data"]["facts"][0]
    assert row["object"]["position_xz"] == [2, 3] and row["object"]["position_map"] == "B"
    assert row["facts"]["skills"]["skills_by_definition"] == {"Medicine": {"level": 0, "passion": None}}
    assert row["facts"]["skills"]["skills_complete"] is False
    assert row["facts"]["health"]["capacities"] == raw["data"]["facts"][0]["facts"]["health"]["capacities"]


def test_description_dedup_preserves_parameters_and_distinct_roles_reference_scopes():
    from plugin.plugins.neko_rimworld.agent.schema_compaction import deduplicate_descriptions

    selector = {"type": "integer", "minimum": 1, "description": "Choose the requested limit."}
    tools = [{"function": {"name": name, "description": "Tool purpose stays local", "parameters": {
        "type": "object", "properties": {
            "limit": copy.deepcopy(selector),
            "actor": {"type": "string", "description": "The bound person."},
            "user": {"type": "string", "description": "The bound person."},
            "ref": {"$ref": "#/$defs/selector", "description": "Bound selector."},
        }, "$defs": {"selector": {"type": kind}}, "required": ["limit"], "additionalProperties": False,
    }}} for name, kind in (("first", "string"), ("second", "integer"))]
    saved = copy.deepcopy(tools)
    out = deduplicate_descriptions(tools)
    assert tools == saved
    first, second = [tool["function"]["parameters"]["properties"] for tool in out]
    assert first["limit"]["description"] == selector["description"] and "description" not in second["limit"]
    assert first["actor"]["description"] == first["user"]["description"] == "The bound person."
    assert "description" not in second["actor"] and "description" not in second["user"]
    assert first["ref"]["description"] == second["ref"]["description"] == "Bound selector."

    def validation(value):
        if isinstance(value, list):
            return [validation(item) for item in value]
        if isinstance(value, dict):
            return {k: validation(v) for k, v in value.items() if k != "description"}
        return value

    assert validation(out) == validation(saved)
    assert all(tool["function"]["description"] == "Tool purpose stays local" for tool in out)


def test_advisory_does_not_invalidate_existing_conversation_prefix():
    session = AgentRunSession.create(ledger=AgentAuditLedger("q"), system_prompt="stable policy", question="q")
    session.append_assistant(
        AssistantTurn(
            "r", "test", "tool_calls", "", (ToolCall("r", "look", "{}"),), reasoning_content="private continuation"
        )
    )
    session.messages.append({"role": "tool", "tool_call_id": "r", "content": "large evidence"})
    saved = copy.deepcopy(session.messages)
    first = session.request_messages("cycle=1")
    second = session.request_messages("cycle=2")
    assert first[:-1] == second[:-1] == saved
    assert first[-1] == {"role": "system", "content": "cycle=1"}
    assert session.messages == saved


def test_dedup_keeps_source_scope_and_derived_labels_without_mutation():
    value = {"ok": True, "data": {"n": 0, "scope": "partial", "sample": 4}}
    original = {
        "full_results": [{"index": 0, "value": value}],
        "derived_output": [{"chosen": value, "computed": 5}],
        "printed": "unaltered",
    }
    saved = copy.deepcopy(original)
    reduced = deduplicate_program_result(original)
    assert reduced["derived_output"] == [{"chosen": {"same_as_full_result_index": 0}, "computed": 5}]
    assert reduced["full_results"] == original["full_results"]
    assert original == saved
    assert deduplicate_program_result({"derived_output": [value]}) == {"derived_output": [value]}


def test_review_gets_visible_failures_and_derivations_not_private_reasoning():
    messages = [
        {
            "role": "assistant",
            "reasoning_content": "PRIVATE",
            "tool_calls": [
                {"id": "r", "function": {"name": "look", "arguments": "{}"}},
                {"id": "s", "function": {"name": "submit_response", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "r", "content": '{"ok":false,"error":{"code":"unavailable"}}'},
        {"role": "tool", "tool_call_id": "s", "content": '{"full_text":"not evidence"}'},
    ]
    view = review_evidence(messages)
    assert len(view) == 1 and view[0]["result"]["ok"] is False
    assert "PRIVATE" not in json.dumps(view)


def test_review_recovered_record_retains_original_route_binding():
    value = {"ok": True, "data": {"route": {"cost": 7}}}
    args = {"actor": {"name": "A"}, "start": {"name": "B"}, "to": {"name": "C"}}
    messages = []
    for i, data in enumerate(
        [
            {"reads": [{"index": 0, "tool": "inspect_route", "arguments": args}], "full_results": []},
            {"reads": [], "full_results": [{"index": 0, "value": value}], "execution_error": None},
        ]
    ):
        messages += [
            {
                "role": "assistant",
                "tool_calls": [{"id": str(i), "function": {"name": "execute_python", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": str(i), "content": json.dumps({"data": data})},
        ]
    result = review_evidence(messages)
    restored = next(r for r in result if "native_result_index" in r)
    assert restored["call"] == {"name": "inspect_route", "arguments": args}
    assert restored["result"] == value
    assert result[-1]["result"]["data"]["execution_error"] is None
