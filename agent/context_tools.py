"""Small model-facing contracts; native/request validation stays unchanged."""
from __future__ import annotations

import copy

from .object_tools import PHYSICAL
from .prompt import finalization_sections, system_sections
from .schema_compaction import compact_tools
from .semantic_surface import KINDS, POPULATIONS, supported_aspects

QUERY_GUIDE = (
    'Read only facts needed for this question. Specify look.aspects. state means physical/device state; '
    'Pawn health, skills, work, gear, mood and profile require their own aspects in the same call. '
    'focus selects named skills/work/capacities in that call. '
    'Resolve names directly; put IDs in id/ids. actor performs interaction/travel; user is the recipient. '
    'count records counts objects; units sums stack counts. Containment does not establish ownership or usability. '
    'evaluate_activity resolves actor/subject names and destination selectors in that same call; '
    'do not look up IDs, count destinations or survey the region first merely to prepare a path request. '
    'Use kind=state for contextual object predicates/occupancy/reservations without paths. '
    'Use it for all requested actors/destinations together; request additional approach, '
    'actor facts and destination state there only when relevant. Request environment in the original path call '
    'when hazards or safety affect the question; omitted environment means uninspected in the model view. survey_space reads regions. '
    'Path cost is not ETA, native Danger is not overall safety, and occupancy/reservation is not complete job eligibility. '
    'Preserve scope, unknowns and every explicitly requested comparison.'
)


def query_guide(*, native_state, activity):
    if native_state and activity:
        return QUERY_GUIDE
    text = ('Read only facts needed for this question. Specify the listed look.aspects. '
            'focus requests named skills/work/capacities and their aspects in that same call. '
            'Use names directly; stable IDs go in id/ids. count records counts objects; units sums stack counts. '
            'Containment does not establish ownership or usability. Preserve scope, unknowns and each requested comparison. ')
    if native_state:
        text += ('state means physical/device state, excluding Pawn health/skills/work/gear. Request those aspects explicitly. '
                 'actor performs interaction/travel; user is the recipient. Use only spatial tools actually listed.')
    else:
        text += 'This connection has legacy object aspects, not native state/stats or activity queries. Do not request unlisted options.'
    return text


def task_prompt(preferred_cycles, *, finalizing=False, references_supported=False):
    sections = finalization_sections if finalizing else system_sections
    parts = dict(sections(preferred_cycles, references_supported=references_supported))
    if not finalizing:
        parts['query_policy'] = (
            'Investigate the explicit question, not a larger hypothetical mission. '
            'Each read must supply a missing requested finding, distinguish the requested candidates, '
            'or resolve a contradiction in those findings. Do not repeat successful reads. '
            'Do not investigate unasked prerequisites merely to eliminate every possible uncertainty. '
            'State those prerequisites as conditions instead. Batch independent requested facts in the first call.\n')
    parts['completion_policy'] = (
        'Cover every explicitly requested subject, destination and comparison. Bind values to their '
        'actor, recipient, origin, destination, metric and sample. Do not substitute another stage or actor. '
        'Measured path cost is not elapsed time; a skill/trait is not a computed final stat. '
        'Keep unknowns and incomplete coverage explicit. Where unasked prerequisites remain unobserved, '
        'make the recommendation conditional instead of expanding the investigation. '
        'Do not infer causes, eligibility or future success from unrelated observations.\n')
    if not references_supported:
        parts['submission'] = (
            'Finish through submit_response. answer is a standalone conclusion with material conditions, '
            'aiming for <=100 tokens (hard maximum 180). detail contains only the remaining findings '
            'explicitly requested by the caller, including complete requested tables. '
            'Do not add adjacent investigations, speculative mechanisms or a catalogue of unasked unknowns. '
            'answer+detail <=12000 characters. Length-only repair sends answer only; saved detail remains.\n')
    return ''.join(parts.values())


def configure_read_spec(tool):
    tool = copy.deepcopy(tool)
    name = tool['name']
    if name not in ('look', 'inspect_route', 'survey_space'):
        return tool
    props = tool['parameters']['properties']
    options = ['body_parts', 'diagnostics'] if name == 'look' else ['environment', 'objects', 'geometry', 'diagnostics']
    props['include'] = {'type': 'array', 'uniqueItems': True, 'items': {'enum': options},
        'description': 'Optional content in this same call. look: full body_parts or diagnostics. Routes: request environment for hazards/safety, objects for inventories, geometry for full segments. Path metrics and doors remain; unrequested environment is not evidence of safety.'}
    if name == 'look':
        if 'state' in props.get('aspects', {}).get('items', {}).get('enum', []):
            tool['description'] = ('Read selected object facts in one call. state returns physical/device facets (condition, beds, reservations, doors, production); '
                'it does not include Pawn health/skills/work/gear. Request those aspects explicitly when needed. '
                'actor performs interaction; user is the recipient. stats requests exact game StatDefs. '
                'Use names directly or stable IDs in id/ids. Scope and unavailable fields are explicit.')
            props['aspects']['description'] = 'Select relevant aspects. state is physical/device state, excluding Pawn health/skills/work/gear. These require their own aspects in the same call.'
        tool['parameters']['required'] = list(dict.fromkeys([*tool['parameters'].get('required', []), 'aspects']))
        props['focus'] = {'type': 'object', 'additionalProperties': False,
            'properties': {key: {'type': 'array', 'minItems': 1, 'maxItems': 32, 'items': {'type': 'string'}}
                           for key in ('skills', 'work', 'capacities')},
            'description': 'Request only these exact definition names or labels; their aspects are included automatically. Omitted keys keep full lists. Unmatched names remain explicit. Use for specific comparisons, not whole-person overviews.'}
    elif name == 'inspect_route':
        props['include_objects'] = {'type': 'boolean', 'default': False,
            'description': 'Include incidental corridor object inventories in this call.'}
        tool['description'] += ' Request include=[environment] for path/corridor hazards and coverage; default path measurements only. include_objects returns incidental object inventories.'
    return tool


def model_tools(tools):
    result = copy.deepcopy(tools)
    for tool in result:
        function = tool.get('function', {})
        name = function.get('name')
        schema = function.get('parameters', {})
        props = schema.get('properties', {})
        if name == 'submit_response' and props.get('answer', {}).get('type') == 'string':
            function['description'] = 'Submit the requested answer. Aim for <=100 tokens in answer (maximum 180); put remaining requested findings in detail. No unasked background or speculative additions. Length-only repair sends answer alone and retains saved detail.'
            props['answer']['description'] = 'Standalone conclusion and material conditions; aim <=100 tokens, hard maximum 180.'
            if 'detail' in props:
                props['detail']['description'] = 'Remaining explicitly requested findings/tables only. Full answer+detail <=12000 characters. Omitted retains saved detail; null clears it.'
        if name == 'look' and 'aspects' in props:
            # The old public allOf repeats the entire discriminator path for
            # every kind, then repeats this same rule in descriptions. Publish
            # the applicability once; ObjectReader/Surface still reject invalid
            # combinations before issuing a native query.
            groups = {}
            native_state = 'state' in props['aspects'].get('items', {}).get('enum', [])
            for kind in KINDS:
                aspects = tuple(supported_aspects(kind)) + (('state',) if native_state and kind in PHYSICAL else ())
                groups.setdefault(aspects, []).append(kind)
            rules = '; '.join('/'.join(kinds) + ': ' + ','.join(aspects) for aspects, kinds in groups.items())
            schema.pop('allOf', None)
            props['aspects']['description'] = 'Allowed by kind: ' + rules + (
                '; cell: identity,state. Omitted: state for physical objects/cells, identity otherwise. '
                'state is physical/device state, excluding Pawn health/skills/gear/work. Request those aspects explicitly. Do not combine state aspects with distribution. '
                'actor/user requires a state aspect. stats/context require physical objects; cells accept no stats.' if native_state else '')
        elif name == 'count' and 'metric' in props:
            schema.pop('allOf', None)
            props['metric']['description'] = 'records counts objects; units sums stack_count and is valid only for things. Population kinds: ' + ','.join(POPULATIONS) + '.'
            obj = props.get('object', {})
            if 'kind' in obj.get('properties', {}):
                obj['properties']['kind']['enum'] = list(POPULATIONS)
    return compact_tools(result)
