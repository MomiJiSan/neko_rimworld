"""Validate the actual public schema against supported handler vocabulary."""
import copy
import itertools

import jsonschema
import pytest

from plugin.plugins.neko_rimworld.agent.semantic_surface import tool_schemas, KINDS, ASPECTS, validate_by, validate_metric
from plugin.plugins.neko_rimworld.agent.query_surface import SurfaceError
from plugin.plugins.neko_rimworld.agent.object_tools import extend_look_schema, ObjectReader
from .test_object_tools import Reader as BaseReader


class Reader(BaseReader):
    async def execute_native(self, args, tool_name):
        result = await super().execute_native(args, tool_name)
        for row in result["data"]["objects"]:
            row["target"].update(definition="Human", map="Map_0")
        return result


def look_schema():
    ref = {'properties': {'id': {'type': 'string'}, 'name': {'type': 'string'}, 'map': {'type': 'string'}}}
    contract = {'parameters': {'properties': {'actor': ref, 'user': ref, 'stats': {'type': 'array'}}}}
    return extend_look_schema(tool_schemas()[0], contract)['parameters']


@pytest.mark.asyncio
async def test_explicit_state_and_legacy_aspects_agree_with_reader():
    validator = jsonschema.Draft202012Validator(look_schema())
    for kind, aspect in itertools.product(KINDS, (*ASPECTS, 'state')):
        obj = {'kind': kind}
        if kind in ('pawn', 'thing', 'map'): obj['id'] = 'a'
        if kind == 'thing_def': obj['definition'] = 'Bed'
        args = {'object': obj, 'aspects': [aspect]}
        schema_ok = validator.is_valid(args)
        # The adapter is a fake native data source; vocabulary validation runs in product code.
        reader = Reader([{'id': 'a'}])
        try:
            await ObjectReader(reader).look(copy.deepcopy(args))
            runtime_ok = True
        except SurfaceError:
            runtime_ok = False
        # Nonphysical legacy adapter validates aspects in production, not in this fake.
        if kind not in ('pawn', 'pawns', 'colonists', 'thing', 'things'):
            from plugin.plugins.neko_rimworld.agent.semantic_surface import validate_aspects
            try: validate_aspects(kind, [aspect]); runtime_ok = True
            except SurfaceError: runtime_ok = False
        assert schema_ok == runtime_ok, (kind, aspect)
    assert validator.is_valid({'object': {'kind': 'thing', 'id': 'a'}})
    assert not validator.is_valid({'object': {'kind': 'things'}, 'aspects': ['state', 'distribution']})


def test_count_combinations_match_real_validators():
    validator = jsonschema.Draft202012Validator(tool_schemas()[1]['parameters'])
    for kind, metric, by in itertools.product(KINDS, ('records', 'units'), (None, 'map', 'definition', 'category', 'holder')):
        args = {'object': {'kind': kind}, 'metric': metric}
        if by is not None: args['by'] = by
        try: validate_metric(kind, metric); validate_by(kind, by); valid = True
        except SurfaceError: valid = False
        assert validator.is_valid(args) == valid, args


@pytest.mark.asyncio
@pytest.mark.parametrize('args', [
    {'object': {'kind': 'thing', 'id': 'a'}, 'aspects': ['identity'], 'actor': {'name': 'p'}},
    {'object': {'kind': 'map', 'id': 'a'}, 'stats': ['MoveSpeed']},
    {'object': {'kind': 'thing_defs'}, 'stats': ['MoveSpeed']},
    {'object': {'kind': 'cell', 'x': 1, 'z': 1}, 'offset': 1},
])
async def test_cross_parameter_rejections_are_public(args):
    assert not jsonschema.Draft202012Validator(look_schema()).is_valid(args)
    with pytest.raises(SurfaceError):
        await ObjectReader(Reader([])).look(args)
