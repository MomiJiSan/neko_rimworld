import asyncio
import copy
import json

import pytest

from plugin.plugins.neko_rimworld.agent.activity import make_activity_tool
from plugin.plugins.neko_rimworld.agent.object_tools import ObjectReader
from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError, ToolRegistry
from plugin.plugins.neko_rimworld.agent.contracts import ToolCall


class NativeInteraction:
    """Real ObjectReader with a deterministic native transport; no model/game."""
    def __init__(self):
        self.calls = []
        self.objects = {name: {'id': name, 'kind': 'pawn' if name in ('a', 'b', 'c', 'patient') else 'thing',
                               'name': name, 'map_id': 'Map_0'} for name in ('a', 'b', 'c', 'patient', 'bed1', 'bed2', 'bed3')}
        self.selector_count = 3
        self.selector_partial = False
        self.route_error = None
        self.large = False

    async def look(self, args):
        return await ObjectReader(self).look(args)

    async def _look_legacy(self, args):
        obj = args['object']
        if obj.get('capabilities'):
            rows = [{'id': f'bed{i+1}', 'map_id': 'Map_0'} for i in range(self.selector_count)]
        else:
            ids = obj.get('ids', [obj.get('id', obj.get('name'))])
            rows = [self.objects[i] for i in ids if i in self.objects]
        return dict(status='ok', facts=[dict(object=copy.deepcopy(row), facts={'identity': {'object': row}}) for row in rows],
                    coverage={'listing': 'partial' if self.selector_partial else 'complete'}, not_read=[])

    async def execute_native(self, args, tool_name):
        self.calls.append((tool_name, copy.deepcopy(args)))
        target = args['targets'][0]
        name = target.get('id', target.get('name'))
        obj = self.objects[name]
        values = [{'definition': stat, 'status': 'unknown_definition'} if stat == 'Missing' else
                  {'definition': stat, 'status': 'observed', 'value': {'a': 4, 'b': 6, 'c': 6}.get(name, 1)}
                  for stat in args.get('stats', [])]
        return dict(status='ok', observation_id=f's{len(self.calls)}', data={
            'sample': {'load_epoch': 'load', 'started_tick': 1, 'finished_tick': 1},
            'context': {'actor_id': args.get('actor', {}).get('id'), 'user_id': args.get('user', {}).get('id')},
            'objects': [{'target': {**obj, 'map': 'Map_0', 'definition': 'Human' if obj['kind'] == 'pawn' else 'Bed'},
                         'sample_tick': 1, 'coverage': {'registered_facets_read_successfully': True},
                         'facets': {'pawn': {'status': 'observed', 'health_summary_fraction': 0.9,
                                            'skills': [{'definition': 'Medicine', 'level': 8}]},
                                    'work': {'status': 'observed', 'work_types': []},
                                    'bed': {'medical_designation': True}, 'stats': {'values': values}}}]})

    async def _world_read(self, args, operation):
        self.calls.append((operation, copy.deepcopy(args)))
        if self.route_error:
            raise self.route_error
        steps = int(args['to']['id'][-1]) if args['to']['id'].startswith('bed') else 7
        return dict(status='ok', observation_id=f'r{len(self.calls)}',
                    sample={'load_epoch': 'load', 'started_tick': 1, 'finished_tick': 1},
                    route={'status': 'found', 'steps': steps,
                           'path_length': {'value': steps, 'unit': 'map_cell_widths'},
                           'native_cost': {'value': steps * 10, 'unit': 'native_path_cost'}},
                    path_environment={'gases': [{'type': 'ToxGas', 'cells': 2 if 'start' in args else 0}],
                                      'coverage': {'visibility_complete': False},
                                      'rooms': ['x' * 150000] if self.large else []})


def arguments(kind='transfer'):
    return dict(kind=kind, actors=[{'kind': 'pawn', 'name': x} for x in ('a', 'b', 'c')],
                destinations={'selector': {'kind': 'things', 'capabilities': ['sleeping_place']}},
                stats=['MoveSpeed'], **({'subject': {'kind': 'pawn', 'name': 'patient'}} if kind == 'transfer' else {}))


@pytest.mark.asyncio
async def test_real_object_reader_matrix_reuses_reads_and_preserves_context_stats_hazards():
    native = NativeInteraction()
    result = await make_activity_tool(native).handler(arguments())
    assert result['status'] == 'ok' and len(result['candidates']) == 9
    assert len(result['reads']) == 24  # 3 actors, 3 approaches, 9 transfers, 9 bed contexts
    assert len(json.dumps(result, ensure_ascii=False)) < 120_000
    for row in result['candidates']:
        stages = {s['kind']: result['reads'][s['read']] for s in row['stages']}
        assert stages['approach']['path_environment']['gases'][0]['cells'] == 0
        assert stages['transfer']['path_environment']['gases'][0]['cells'] == 2
        assert stages['transfer']['path_environment']['coverage']['visibility_complete'] is False
        context = stages['destination_state']['state_observations'][0]['context']
        assert context == {'actor_id': row['actor']['id'], 'user_id': 'patient'}
        assert stages['actor_state']['facts'][0]['facts']['stats']['values'][0]['definition'] == 'MoveSpeed'
    stats = next(c for c in result['comparisons'] if 'stat' in c['scope'])
    assert [(r['actor']['id'], r['rank']) for r in stats['entries']] == [('b', 1), ('c', 1), ('a', 3)]
    transfers = [c for c in result['comparisons'] if c['scope'].get('stage') == 'transfer' and c['metric'] == 'steps']
    assert len(transfers) == 3 and all([r['rank'] for r in c['entries']] == [1, 2, 3] for c in transfers)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['missing_subject', 'selector_partial', 'selector_large', 'unknown_field'])
async def test_invalid_or_incomplete_selection_never_executes_paths(change):
    native = NativeInteraction(); args = arguments()
    if change == 'missing_subject': args.pop('subject')
    elif change == 'selector_partial': native.selector_partial = True
    elif change == 'selector_large': native.selector_count = 9
    else: args['preferences'] = ['shorter_route']
    with pytest.raises(ToolExecutionError):
        await make_activity_tool(native).handler(args)
    assert not any(op == 'inspect_route' for op, _ in native.calls)


@pytest.mark.asyncio
async def test_move_uses_explicit_start_and_preserves_unknown_stats():
    native = NativeInteraction(); args = arguments('move')
    args.update(start={'kind': 'pawn', 'id': 'patient'}, stats=['Missing'])
    result = await make_activity_tool(native).handler(args)
    assert all(s['kind'] != 'approach' for c in result['candidates'] for s in c['stages'])
    assert all(a['start']['id'] == 'patient' for op, a in native.calls if op == 'inspect_route')
    assert result['status'] == 'ok'
    assert all(v.get('status') == 'unknown_definition' for v in result['reads']['r1']['facts'][0]['facts']['stats']['values'])


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ToolExecutionError('world_changed', 'reload', recoverable=False), asyncio.CancelledError(), RuntimeError('programming error')])
async def test_fatal_errors_cancel_instead_of_creating_partial_success(error):
    native = NativeInteraction(); native.route_error = error
    with pytest.raises(type(error)):
        await make_activity_tool(native).handler(arguments())
    assert len([op for op, _ in native.calls if op == 'inspect_route']) == 1


@pytest.mark.asyncio
async def test_recoverable_errors_keep_other_reads_and_oversize_result_reaches_registry():
    native = NativeInteraction(); native.route_error = ToolExecutionError('route_unavailable', 'try narrower')
    result = await make_activity_tool(native).handler(arguments())
    assert result['status'] == 'partial'
    assert any(r.get('facts') for r in result['reads'].values())
    native = NativeInteraction(); native.large = True
    registry = ToolRegistry(); registry.register(make_activity_tool(native))
    prepared = registry.prepare(ToolCall(call_id='test', name='evaluate_activity', raw_arguments=json.dumps(arguments())))
    output = await registry.execute(prepared)
    assert output.ok and output.value['data']['status'] == 'ok'
    assert len(output.value['data']['candidates']) == 9
    assert len(output.value['data']['reads']) == 24
