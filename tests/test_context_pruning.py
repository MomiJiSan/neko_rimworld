import copy
import json

import pytest

from plugin.plugins.neko_rimworld.agent.context_pruning import ContextPruner, prune
from plugin.plugins.neko_rimworld.agent.evidence_compiler import make_compiler, requested
from plugin.plugins.neko_rimworld.agent.contracts import ToolCall
from plugin.plugins.neko_rimworld.agent.tools import ToolRegistry, ToolSpec


def route():
    return {'ok': True, 'tool': 'inspect_route', 'data': {
        'sample': {'load_epoch': 'load', 'started_tick': 4, 'finished_tick': 5},
        'context': {'actor_id': 'A', 'user_id': 'P'},
        'route': {'status': 'found', 'start': {'x': 1, 'z': 2}, 'end': {'x': 3, 'z': 4},
                  'steps': 9, 'native_cost': {'value': 126, 'unit': 'native_path_cost'},
                  'segments': [{'door': None, 'terrain': 'Soil'}, {'door': {'id': 'D', 'passable': False}}]},
        'path_environment': {'gases': [{'type': 'ToxGas', 'cells': 2}],
                             'coverage': {'complete': False}, 'path_environment_bands': [{'temperature': 50}]},
        'environment': {'gases': [], 'coverage': {'complete': True}},
        'objects': [{'target': {'id': 'shirt'}, 'facets': {'condition': {'hit_points': 100}}}],
        'objects_summary': {'records': 1}, 'page': {'listing_complete': True},
        'extension': {'read_ms': 9, 'meaning': 'game data', 'unknown_mod_fields': None}}}


def test_pruning_removes_incidental_inventory_without_losing_route_or_hazards():
    raw = route()
    saved = copy.deepcopy(raw)
    result, _, _ = prune(raw, {'include': ['environment']})
    data = result['data']
    assert raw == saved
    assert 'objects' not in data and data['objects_included'] is False
    assert data['route']['steps'] == 9
    assert data['route']['native_cost']['value'] == 126
    assert data['route']['door_segments'][0]['door']['passable'] is False
    assert data['path_environment']['gases'] == raw['data']['path_environment']['gases']
    assert data['path_environment']['coverage'] == {'complete': False}
    assert data['sample'] == raw['data']['sample'] and data['context'] == raw['data']['context']
    assert data['extension'] == raw['data']['extension']
    assert data['environment']['gases'] == []  # Corridor and path never merged.


def test_same_call_opt_in_keeps_inventory_geometry_and_diagnostics():
    raw = route()
    raw['data']['read_ms'] = 1
    result, _, _ = prune(raw, {'include_objects': True, 'include': ['environment', 'geometry', 'diagnostics']})
    assert result == {**raw, 'data': {**raw['data'], 'objects_included': True, 'environment_included': True}}


def test_duplicate_identity_and_profile_copies_are_deleted_only_when_equal():
    identity = {'id': 'P', 'name': 'Pawn', 'faction': None}
    value = {'tool': 'look', 'data': {'facts': [{'object': identity,
        'facts': {'identity': {'object': copy.deepcopy(identity)},
                  'profile': {'profile': {'age': 22, 'traits': ['x']}, 'age': 22, 'traits': ['x']}}}]}}
    out, _, _ = prune(value, {'aspects': ['identity', 'profile']})
    facts = out['data']['facts'][0]['facts']
    assert 'identity' not in facts and facts['profile'] == {'profile': {'age': 22, 'traits': ['x']}}
    value['data']['facts'][0]['facts']['identity']['object']['faction'] = 'different'
    out, _, _ = prune(value, {'aspects': ['identity']})
    assert out['data']['facts'][0]['facts']['identity']['object']['faction'] == 'different'


def test_health_keeps_all_exception_values_unknowns_and_explicit_anatomy_option():
    health = {'conditions': [{'part': 2, 'tended': False}], 'damaged_present_parts': [],
              'missing_parts': [{'index': 3}], 'capacities': [{'definition': 'Moving', 'level': 0}],
              'pain_fraction': None, 'body_parts': [{'index': 1, 'health': 10}, {'index': 2, 'health': 4}],
              'body_parts_complete': True, 'body_parts_total': 3,
              'field_states': {'pain_fraction': {'status': 'unavailable'}}}
    raw = {'tool': 'look', 'data': {'facts': [{'object': {'id': 'A'}, 'facts': {'health': health}}]}}
    out, _, _ = prune(raw, {'aspects': ['health']})
    result = out['data']['facts'][0]['facts']['health']
    assert result['condition_part_details'] == [{'index': 2, 'health': 4}]
    assert result['body_parts_included'] is False and 'body_parts_complete' not in result
    for key in ('conditions', 'missing_parts', 'capacities', 'pain_fraction', 'field_states'):
        assert result[key] == health[key]
    out, _, _ = prune(raw, {'aspects': ['health'], 'include': ['body_parts']})
    assert out['data']['facts'][0]['facts']['health'] == health


def test_focused_skills_do_not_turn_missing_names_into_zero_or_complete_population():
    raw = {'tool': 'look', 'data': {'facts': [{'object': {'id': 'P'}, 'facts': {'skills': {
        'skills': [{'definition': 'Medicine', 'level': 0}, {'definition': 'Cooking', 'level': 12}],
        'skills_complete': True, 'skills_total': 2}}}]}}
    out, _, _ = prune(raw, {'aspects': ['skills'], 'focus': {'skills': ['Medicine', 'ModAdded']}})
    result = out['data']['facts'][0]['facts']['skills']
    assert result['skills'] == [{'definition': 'Medicine', 'level': 0}]
    assert result['skills_selection']['unmatched'] == ['ModAdded']
    assert result['skills_complete'] is True and result['skills_total'] == 1
    assert result['skills_selection']['requested'] == ['Medicine', 'ModAdded']
    assert len(raw['data']['facts'][0]['facts']['skills']['skills']) == 2


def test_notes_are_kept_once_without_references_and_failed_compiles_do_not_consume_them():
    compiler = ContextPruner()
    raw = {'tool': 'look', 'data': {'semantics': ['one rule', 'one rule'], 'value': None}}
    with pytest.raises(ValueError, match='evidence_view_limit'):
        compiler.compile(raw, max_view_chars=1)
    first = json.loads(compiler.compile(raw).content)
    second = json.loads(compiler.compile(raw).content)
    assert first['data']['semantics'] == ['one rule']
    assert 'semantics' not in second['data'] and second['data']['value'] is None
    assert '_neko_evidence_format' not in first


def test_failed_activity_result_remains_an_error_without_cycles():
    raw = {'ok': False, 'tool': 'evaluate_activity', 'error': {'code': 'timeout'}}
    assert json.loads(ContextPruner().compile(raw).content) == raw


@pytest.mark.asyncio
async def test_actual_registry_passes_inclusion_arguments_and_keeps_raw_result(monkeypatch):
    monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', '3')
    assert requested()
    registry = ToolRegistry(evidence_compiler=make_compiler())
    async def read(args): return route()['data']
    registry.register(ToolSpec('inspect_route', 'path', {'type': 'object'}, read))
    raw = await registry.execute(registry.prepare(ToolCall('r', 'inspect_route', '{"include_objects":true,"include":["geometry"]}')))
    assert 'objects' in raw.value['data'] and 'objects' in json.loads(raw.as_message()['content'])['data']
    assert raw.model_view_metrics['format'] == 'json'


@pytest.mark.asyncio
async def test_activity_default_reads_only_requested_paths_and_optional_reads_stay_one_call(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.activity import evaluate_activity, SemanticActivityDataSource
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction
    monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', '3')
    arguments = {'kind': 'transfer', 'actors': [{'kind': 'pawn', 'id': x, 'map': 'Map_0'} for x in ('a', 'b', 'c')],
                 'subject': {'kind': 'pawn', 'id': 'patient', 'map': 'Map_0'},
                 'destinations': [{'kind': 'thing', 'id': f'bed{i}', 'map': 'Map_0'} for i in (1, 2, 3)]}
    native = NativeInteraction()
    result = await evaluate_activity(arguments, SemanticActivityDataSource(native))
    assert len(native.calls) == 9 and all(op == 'inspect_route' for op, _ in native.calls)
    assert len(result['observations']) == 9
    assert all(row['kind'] == 'transfer' and row['origin']['id'] == 'patient' for row in result['observations'])
    assert all(row['observation']['environment_included'] is False and 'path_environment' not in row['observation'] for row in result['observations'])
    native = NativeInteraction()
    result = await evaluate_activity({**arguments, 'include': ['approach', 'destination_state', 'environment'],
                                     'actor_aspects': ['health', 'skills', 'work']}, SemanticActivityDataSource(native))
    assert len(native.calls) == 24
    assert len(result['observations']) == 24
    assert {r['kind'] for r in result['observations']} == {'actor_state', 'approach', 'transfer', 'destination_state'}
    assert all('read' not in row for row in result['observations'])
    assert any(row['observation'].get('path_environment', {}).get('gases') for row in result['observations'])


def test_budget_charges_every_resend_and_never_charges_a_refused_request():
    from plugin.plugins.neko_rimworld.agent.input_budget import InputBudget, public_input_tokens
    messages = [{'role': 'user', 'content': 'question'}, {'role': 'assistant', 'content': '',
                 'tool_calls': [{'function': {'name': 'look', 'arguments': '{}'}}]}]
    tools = [{'type': 'function', 'function': {'name': 'look'}}]
    cost = public_input_tokens(messages, tools)
    budget = InputBudget(2 * cost)
    assert budget.admit(messages, tools)[0]
    assert budget.admit(messages, tools)[0]
    admitted, record = budget.admit(messages, tools)
    assert not admitted and budget.used == cost * 2
    assert record['cumulative_public_input_tokens'] == cost * 2
    assert record['private_continuation_and_provider_framing_counted'] is False


def test_selector_nesting_repair_preserves_explicit_values_and_rejects_conflicts(monkeypatch):
    monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', '3')
    registry = ToolRegistry()
    registry.register(ToolSpec('count', 'count', {'properties': {'object': {}, 'metric': {}}, 'additionalProperties': False}, None))
    args = {'object': {'kind': 'things'}, 'metric': 'records', 'include_held': False, 'include_fogged': True}
    call = registry.prepare(ToolCall('c', 'count', json.dumps(args)))
    assert call.arguments == {'object': {'kind': 'things', 'include_held': False, 'include_fogged': True}, 'metric': 'records'}
    args['object']['include_held'] = True
    rejected = registry.prepare(ToolCall('d', 'count', json.dumps(args)))
    assert rejected.ok is False


def test_script_outputs_are_not_reinterpreted_as_native_metadata():
    raw = {'tool': 'execute_python', 'data': {'read_ms': 12, 'body_parts': [], 'meaning': 'literal output'}}
    assert json.loads(ContextPruner().compile(raw).content) == raw


def test_review_never_projects_author_visible_opt_in_details_again():
    raw = route()
    assert json.loads(make_compiler('3', project_display=False).compile(raw).content) == raw


@pytest.mark.asyncio
async def test_state_no_longer_smuggles_all_pawn_facts_and_selected_health_still_arrives(monkeypatch):
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction
    from plugin.plugins.neko_rimworld.agent.object_tools import ObjectReader
    monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', '3')
    native = NativeInteraction()
    result = await ObjectReader(native).look({'object': {'kind': 'pawn', 'id': 'a'}, 'aspects': ['state', 'health']})
    facts = result['facts'][0]['facts']
    assert 'pawn' not in facts['state']['facets'] and 'work' not in facts['state']['facets']
    assert facts['health']['health_summary_fraction'] == 0.9
    assert 'skills' not in facts['health']
    assert len(native.calls) == 1


def test_length_only_repair_does_not_replay_world_or_detail_and_never_hides_claim_errors(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.session import AgentRunSession
    from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger
    monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', '3')
    session = AgentRunSession.create(ledger=AgentAuditLedger('question'), system_prompt='policy', question='question')
    session.messages.append({'role': 'tool', 'content': 'LARGE WORLD DATA'})
    session.submission.answer = [{'text': 'A conditional answer; B remains unknown.'}]
    session.submission.detail = 'SAVED FULL REPORT'
    session.submission.errors = {'delivery': {'code': 'handoff_over_budget'}}
    before = copy.deepcopy(session.messages)
    repair = session.length_repair_messages()
    assert 'LARGE WORLD DATA' not in json.dumps(repair) and 'SAVED FULL REPORT' not in json.dumps(repair)
    assert repair[2]['content'] == session.submission.answer[0]['text']
    assert session.messages == before and session.submission.detail == 'SAVED FULL REPORT'
    session.submission.errors = {'delivery': {'code': 'unsupported_claim'}}
    assert session.length_repair_messages() is None


@pytest.mark.asyncio
async def test_runtime_length_repair_uses_one_small_request_and_preserves_full_report(monkeypatch):
    from plugin.plugins.neko_rimworld.tests.test_agent import FakeLogger, settings, turn
    from plugin.plugins.neko_rimworld.agent import AgentRuntime, AgentAuditRegistry
    monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', '3')
    full = 'SAVED DETAIL ' * 400
    long_answer = 'This is a conditional conclusion with uncertainty. ' * 40
    class Model:
        def __init__(self): self.calls = 0
        async def complete(self, messages, tools, *, timeout):
            self.calls += 1
            if self.calls == 1:
                return turn('', ToolCall('first', 'submit_response', json.dumps({'answer': long_answer, 'detail': full})))
            assert self.calls == 2
            assert len(tools) == 1 and tools[0]['function']['name'] == 'submit_response'
            assert 'SAVED DETAIL' not in json.dumps(messages)
            assert set(tools[0]['function']['parameters']['properties']) == {'answer'}
            return turn('', ToolCall('repair', 'submit_response', '{"answer":"Conditional conclusion; uncertainty remains."}'))
    model = Model()
    runtime = AgentRuntime(observation_tools=[], audit_registry=AgentAuditRegistry(), logger=FakeLogger(),
                           model_factory=lambda _: model)
    result = await runtime.run('Answer with detail.', settings())
    assert result['status'] == 'ok' and model.calls == 2
    assert full.strip() in result['answer']


@pytest.mark.asyncio
async def test_redundant_move_leg_and_explicit_recipient_do_not_need_repair_calls(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.activity import evaluate_activity, SemanticActivityDataSource
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction
    monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', '3')
    native = NativeInteraction()
    result = await evaluate_activity({'kind': 'move', 'actors': [{'kind': 'pawn', 'id': 'a', 'map': 'Map_0'}],
        'destinations': [{'kind': 'thing', 'id': 'bed1', 'map': 'Map_0'}],
        'user': {'kind': 'pawn', 'id': 'patient', 'map': 'Map_0'}, 'include': ['approach', 'destination_state']},
        SemanticActivityDataSource(native))
    assert len(native.calls) == 2
    assert [x['kind'] for x in result['observations']] == ['move', 'destination_state']
    assert next(args for op, args in native.calls if op == 'inspect_route')['user']['id'] == 'patient'
    assert next(args for op, args in native.calls if op == 'inspect_state')['user']['id'] == 'patient'


@pytest.mark.asyncio
async def test_contextual_state_matrix_does_not_read_unrequested_paths_or_actor_profiles(monkeypatch):
    from plugin.plugins.neko_rimworld.agent.activity import evaluate_activity, SemanticActivityDataSource
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction
    monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', '3')
    native = NativeInteraction()
    result = await evaluate_activity({'kind': 'state',
        'actors': [{'kind': 'pawn', 'id': x, 'map': 'Map_0'} for x in ('a', 'b', 'c')],
        'destinations': [{'kind': 'thing', 'id': f'bed{i}', 'map': 'Map_0'} for i in (1, 2, 3)],
        'user': {'kind': 'pawn', 'id': 'patient', 'map': 'Map_0'}}, SemanticActivityDataSource(native))
    assert len(native.calls) == 9 and all(op == 'inspect_state' for op, _ in native.calls)
    assert len(result['observations']) == 9 and all(r['kind'] == 'destination_state' for r in result['observations'])
    assert all(args['user']['id'] == 'patient' for _, args in native.calls)


def test_legacy_contract_does_not_advertise_unavailable_native_state_or_activity():
    from plugin.plugins.neko_rimworld.agent.semantic_surface import tool_schemas
    from plugin.plugins.neko_rimworld.agent.context_tools import configure_read_spec, query_guide
    spec = configure_read_spec(tool_schemas()[0])
    assert 'state' not in spec['parameters']['properties']['aspects']['items']['enum']
    assert 'state returns' not in spec['description']
    assert 'evaluate_activity' not in query_guide(native_state=False, activity=False)


@pytest.mark.asyncio
async def test_named_focus_includes_its_aspect_in_the_same_native_read(monkeypatch):
    from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction
    from plugin.plugins.neko_rimworld.agent.object_tools import ObjectReader
    monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', '3')
    native = NativeInteraction()
    result = await ObjectReader(native).look({'object': {'kind': 'pawn', 'id': 'a'},
        'aspects': ['health'], 'focus': {'skills': ['Medicine']}})
    assert len(native.calls) == 1 and native.calls[0][1]['sections'] == ['health', 'skills']
    assert result['facts'][0]['facts']['skills']['skills'][0]['definition'] == 'Medicine'
