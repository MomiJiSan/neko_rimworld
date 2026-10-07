"""The retired implementation-dump option cannot regain a model route."""

import copy
import json
from types import SimpleNamespace

import jsonschema
import pytest

from plugin.plugins.neko_rimworld.agent.context_tools import configure_read_spec, model_tools
from plugin.plugins.neko_rimworld.agent.context_pruning import ContextPruner
from plugin.plugins.neko_rimworld.agent.object_tools import ObjectReader
from plugin.plugins.neko_rimworld.agent.semantic_surface import SurfaceError
from plugin.plugins.neko_rimworld.agent.semantic_tools import SemanticInteraction
from plugin.plugins.neko_rimworld.tests.test_activity import NativeInteraction
from plugin.plugins.neko_rimworld.tests.test_native_on_demand import route_recorder
from plugin.plugins.neko_rimworld.tests.test_context_pruning import route


@pytest.mark.parametrize('name,allowed', [
    ('look', ['body_parts']),
    ('inspect_route', ['environment', 'objects', 'geometry']),
    ('survey_space', ['environment', 'objects', 'geometry']),
])
def test_published_include_rejects_diagnostics_and_preserves_real_selection(name, allowed):
    spec = configure_read_spec({'name': name, 'description': 'Read',
                               'parameters': {'type': 'object', 'properties': {}}})
    published = model_tools([{'type': 'function', 'function': spec}])[0]['function']
    validator = jsonschema.Draft202012Validator(published['parameters'])
    base = {'aspects': ['health']} if name == 'look' else {}
    assert not validator.is_valid({**base, 'include': ['diagnostics']})
    for option in allowed:
        assert validator.is_valid({**base, 'include': [option]})
    assert 'diagnostics' not in json.dumps(published)


@pytest.mark.asyncio
async def test_native_look_rejects_dump_before_any_read(monkeypatch):
    monkeypatch.setenv('NEKO_CONTEXT_PRUNING', '1')
    native = NativeInteraction()
    with pytest.raises(SurfaceError, match='include accepts body_parts'):
        await ObjectReader(native).look({'object': {'kind': 'pawn', 'id': 'a'},
                                        'aspects': ['health'], 'include': ['diagnostics']})
    assert native.calls == []


@pytest.mark.asyncio
async def test_legacy_look_rejects_dump_before_any_read():
    calls = []

    async def read(args):
        calls.append(args)
        raise AssertionError('rejected arguments must not read')

    legacy = SimpleNamespace(object_reader=None, _look_legacy=read)
    with pytest.raises(SurfaceError, match='include accepts body_parts'):
        await SemanticInteraction.look(legacy, {'object': {'kind': 'pawn', 'id': 'a'},
                                              'aspects': ['health'], 'include': ['diagnostics']})
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['inspect_route', 'survey_space'])
@pytest.mark.parametrize('version', ['world_observation.v2.2', 'world_observation.v2.3'])
async def test_spatial_dump_rejected_before_supported_native_dispatch(monkeypatch, operation, version):
    monkeypatch.setenv('NEKO_CONTEXT_PRUNING', '1')
    native, calls = route_recorder(version)
    with pytest.raises(SurfaceError, match='spatial include accepts'):
        await SemanticInteraction._world_read(native, {'include': ['diagnostics']}, operation)
    assert calls == []


def test_forged_historical_option_cannot_restore_projection_bypass_or_mutate_audit():
    raw = route()
    raw['data'].update(read_ms=17, cooperative_budget_overrun=False,
                       unsupported_components=['Verse.CompA', 'Verse.CompB'])
    raw['data']['objects'][0]['target'].update(position={'x': 1, 'z': 2},
                                             runtime_type='Verse.Thing', holder_chain=['tracker'])
    saved = copy.deepcopy(raw)
    options = {'include': ['environment', 'objects']}
    normal = ContextPruner().compile(raw, arguments=options)
    forged = ContextPruner().compile(raw, arguments={'include': [*options['include'], 'diagnostics']})
    assert forged.content == normal.content
    assert forged.source_sha256 == normal.source_sha256 and raw == saved
    visible = json.loads(forged.content)['data']
    assert 'read_ms' not in visible and 'cooperative_budget_overrun' not in visible
    assert visible['unsupported_components_count'] == 2
    assert 'runtime_type' not in visible['objects'][0]['target']
    assert 'holder_chain' not in visible['objects'][0]['target']
    assert visible['path_environment']['gases'] == saved['data']['path_environment']['gases']
    assert visible['sample'] == saved['data']['sample']
    assert visible['context'] == saved['data']['context']
    assert visible['extension'] == saved['data']['extension']
