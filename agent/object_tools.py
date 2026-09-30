"""One object-facing reader; native state and legacy facts are internal adapters."""
from __future__ import annotations

import copy
import json
import re

from .query_surface import SurfaceError
from .semantic_surface import PAWN_KINDS, THING_KINDS, ASPECTS, KINDS, supported_aspects, normalize_object, validate_aspects, validate_detail
from .world_order import order_world_fields
from .tools import MAX_TOOL_RESULT_CHARS
from .evidence_compiler import MAX_SOURCE_CHARS, canonical, model_size, requested, pruning_requested

PHYSICAL = (*PAWN_KINDS, *THING_KINDS)
OBJECT_GUIDE = (
    'Use look for object facts and count for population quantities/grouping. '
    'look accepts the same object selectors; capabilities on things matches any listed native capability, '
    'intersected with other selectors. Identity discovery does not scan environment. '
    'look defaults to current state for physical objects/cells; request selected aspects for small reads, '
    'and stats for exact game-computed attributes. An unknown StatDef may return native discovery matches; these are names, not computed values or automatic replacements. '
    'actor interacts/travels, user receives/uses. Use health.capacities for Pawn capacities, not guessed StatDef names. '
    'Resolve names directly; stable IDs belong in id/ids, never name/names. No preliminary ID lookup is required. '
    'include_held/include_fogged retain their inclusive polarity and scope defaults. '
    'object.location.position is the canonical own-position cell; held_position is separate. A null location is not proof that the object is absent; retain its applicability and coverage. '
    'Use wiki for definition names/types when available; definition knowledge is not a current actor value. '
    'survey_space reads region and environment; inspect_route reads path and environment; '
    'inspect_space remains the lightweight reachability/path-ranking tool. '
    'Read per-observation scope, coverage and unknowns. Neither occupancy nor reservation '
    'is a complete action check; native Danger is not overall safety.'
)
STATE_ASPECTS = ('state', 'health', 'skills', 'gear', 'work')
GEAR_FIELDS = ('carried', 'equipment', 'apparel', 'inventory')

def _belongs(key, fields):
    return any(key == field or key.startswith(field + '_') for field in fields)


def state_contract(directory):
    world = directory.get('world_observation', {})
    if world.get('version') != 'world_observation.v2.2':
        return None
    for tool in world.get('tools', []):
        if tool.get('name') == 'inspect_state':
            properties = tool.get('parameters', {}).get('properties', {})
            required = {'targets','stats','actor','user','sections','include_fogged','detail_limit','exact_lookup'}
            sections = properties.get('sections',{}).get('items',{}).get('enum',[])
            if required <= properties.keys() and {'identity',*STATE_ASPECTS} <= set(sections):
                return tool
    return None


def extend_look_schema(spec, contract):
    """Keep one selector/aspects interface, exposing only negotiated capabilities."""
    spec = copy.deepcopy(spec)
    spec['description'] = (
        'Find objects and read their requested facts in one call. Physical objects support state '
        '(all current native facets), plus selected health/skills/gear/work for pawns. '
        'Work includes assignments and current jobs. Identity discovery does not inspect environment. '
        'Other aspects retain biography, mood, resources and definition facts. '
        'Use stats for exact game-computed StatDefs. actor performs interaction/travel; user is '
        'the intended recipient; omitted actor defaults to user. Context checks are observations, not action eligibility guarantees. '
        'Resolve names directly; stable IDs belong in id/ids, not name/names. Do not look up IDs first. '
        'object.location gives canonical position and held_position cells without removing legacy fields. '
        'Unknown StatDefs may provide discovery.matches and matching_definition_kind; names are not computed values. '
        'Scope, coverage and unavailable facts are explicit.'
    )
    props = spec['parameters']['properties']
    cell = {'type': 'object', 'additionalProperties': False, 'required': ['kind','x','z'],
            'properties': {'kind': {'const':'cell'}, 'map': {'type':'string','default':'current'},
                           'x': {'type':'integer','minimum':-2147483648,'maximum':2147483647},
                           'z': {'type':'integer','minimum':-2147483648,'maximum':2147483647}}}
    props['offset'] = {'type':'integer','minimum':0,'maximum':8192,'default':0,'description':'Continue object rows using returned page.next_offset. Each page is a fresh observation, not a frozen snapshot.'}
    props['object'] = {'oneOf': [props['object'], cell]}
    props['aspects']['items']['enum'] = list(ASPECTS) + ['state']
    props['detail']['description'] = 'Bounds long Pawn/work detail lists (conditions, body parts, gear, queued jobs); 0 uses reader defaults. Skills, capacities and work-type assignments remain complete when observed, including mod-added definitions. Other facets report their own bounds. Does not change selection or requested stats.'
    props['aspects']['description'] = 'Omitted: current state for physical objects/cells, identity otherwise. Select only needed aspects for smaller reads. state already contains native health, skills, gear, work and device facets; mood/biography are separate.'
    for key in ('stats','actor','user'):
        props[key] = copy.deepcopy(contract['parameters']['properties'][key])
        if key in ('actor','user'):
            props[key] = {'type':'object','additionalProperties':False,'description':'Pawn context: provide exactly one of id or name, not both. Optional map defaults to current.',
                          'properties':{**{name:copy.deepcopy(props[key]['properties'][name]) for name in ('id','name','map')}, 'kind':{'type':'string','const':'pawn'}},
                          'oneOf':[{'required':['id'],'not':{'required':['name']}},{'required':['name'],'not':{'required':['id']}}]}
    # Replace legacy per-kind constraints as well as the top-level vocabulary.
    spec['parameters']['allOf'] = [
        {'if': {'properties': {'object': {'properties': {'kind': {'const': kind}}, 'required': ['kind']}}, 'required': ['object']},
         'then': {'properties': {'aspects': {'items': {'enum':
             list(supported_aspects(kind)) + (['state'] if kind in PHYSICAL else [])}}}}}
        for kind in KINDS
    ] + [
        {'if': {'properties': {'object': {'properties': {'kind': {'const': 'cell'}}, 'required': ['kind']}}},
         'then': {'properties': {'aspects': {'items': {'enum': ['identity', 'state']}}, 'offset': {'const': 0}}, 'not': {'required': ['stats']}}},
        {'not': {'properties': {'aspects': {'allOf': [
            {'contains': {'enum': list(STATE_ASPECTS)}}, {'contains': {'const': 'distribution'}}]}}, 'required': ['aspects']}},
    ]
    spec['parameters']['allOf'].extend([
        {'if': {'properties': {'object': {'properties': {'kind': {'not': {'enum': [*PHYSICAL, 'cell']}}}, 'required': ['kind']}}},
         'then': {'not': {'anyOf': [{'required': [key]} for key in ('stats', 'actor', 'user')]}}},
        {'if': {'anyOf': [{'required': ['actor']}, {'required': ['user']}]},
         'then': {'properties': {'aspects': {'contains': {'enum': list(STATE_ASPECTS)}}}}},
    ])
    spec['parameters']['required'] = ['object']
    return spec


class ObjectReader:
    def __init__(self, interaction):
        self.interaction = interaction

    async def look(self, args):
        if not isinstance(args, dict) or not isinstance(args.get('object'), dict):
            raise SurfaceError('look requires object')
        unknown = set(args) - {'object','aspects','detail','stats','actor','user','offset', *({'include', 'focus'} if pruning_requested() else set())}
        if unknown:
            raise SurfaceError('look does not accept: ' + ', '.join(sorted(unknown)))
        if pruning_requested():
            include = args.get('include', [])
            if not isinstance(include, list) or any(v not in ('body_parts', 'diagnostics') for v in include):
                raise SurfaceError('look include accepts body_parts and diagnostics')
            # Model-only inclusion controls never enter the native request.
            focus = args.get('focus', {})
            if not isinstance(focus, dict) or set(focus) - {'skills', 'work', 'capacities'} or any(
                not isinstance(v, list) or not 1 <= len(v) <= 32 or any(not isinstance(n, str) or not n or len(n) > 256 for n in v)
                for v in focus.values()
            ):
                raise SurfaceError('focus selects skills/work/capacities by 1..32 exact definition names or labels')
            # Native requests below are built from explicit native keys. Retain
            # these controls here so page fitting uses the requested model view.
        raw_object = args['object']
        kind = raw_object.get('kind')
        cell = kind == 'cell'
        obj = copy.deepcopy(raw_object) if cell else normalize_object(raw_object)
        aspects = args.get('aspects', ['state'] if kind in PHYSICAL or cell else ['identity'])
        if not isinstance(aspects,list) or not aspects or any(not isinstance(a,str) for a in aspects):
            raise SurfaceError('aspects must be a nonempty string array')
        aspects = list(dict.fromkeys(aspects))
        if pruning_requested():
            aspects = list(dict.fromkeys([*aspects, *({'skills':'skills','work':'work','capacities':'health'}[k]
                                                     for k in args.get('focus', {}))]))
        detail = validate_detail(args.get('detail',0))
        offset = args.get('offset',0)
        if type(offset) is not int or not 0 <= offset <= 8192 or cell and offset:
            raise SurfaceError('offset must be 0..8192 for object populations; cells use zero')
        state = [a for a in aspects if a in STATE_ASPECTS]
        if state and 'distribution' in aspects:
            raise SurfaceError('Use count for quantities/grouping; look for object state')
        legacy = [a for a in aspects if a not in STATE_ASPECTS]
        if kind not in PHYSICAL and not cell:
            if state or any(k in args for k in ('stats','actor','user')):
                raise SurfaceError('state/stats/context require physical objects or cells')
            return await self.interaction._look_legacy({**args,'aspects':aspects})
        if cell:
            if set(aspects)-{'identity','state'} or 'stats' in args:
                raise SurfaceError('cells support identity/state, not Pawn/Thing stats')
            if set(obj)-{'kind','map','x','z'} or any(type(obj.get(k)) is not int for k in ('x','z')):
                raise SurfaceError('cell requires integer x/z and optional map')
        else:
            validate_aspects(kind, [a for a in aspects if a != 'state']) if any(a != 'state' for a in aspects) else None
        stats = args.get('stats')
        if 'stats' in args and (not isinstance(stats,list) or not 1 <= len(stats) <= 16 or any(not isinstance(x,str) or not x for x in stats)):
            raise SurfaceError('stats requires 1..16 exact StatDef names')
        for key in ('actor','user'):
            if key in args:
                ref = args[key]
                if (not isinstance(ref,dict) or set(ref)-{'id','name','kind','map'}
                        or ('id' in ref) == ('name' in ref) or ref.get('kind','pawn') != 'pawn'
                        or not isinstance(ref.get('id',ref.get('name')),str) or not ref.get('id',ref.get('name'))):
                    raise SurfaceError(key+' requires one Pawn id or name and optional map')
        if any(k in args for k in ('actor','user')) and not state:
            raise SurfaceError('actor/user context requires a state aspect')
        if not cell and not state and stats is None and not any(k in args for k in ('actor','user')):
            return self._fit_page(self._normalize_identities(await self.interaction._look_legacy({**args,'aspects':aspects})), args)
        direct = (kind in ('pawn','thing') and offset == 0
                  and not set(obj) - {'kind','ids','names','map','include_fogged'}
                  and len(obj.get('ids',obj.get('names',[]))) == 1
                  and not set(legacy) - {'identity'})
        if cell or direct:
            base = {'status':'ok','tool':'look','object':obj,'scope':{'object':obj,'include_fogged':False if cell else obj.get('include_fogged',True)},'coverage':{},'facts':[{'object':obj,'facts':{}}],'not_read':[]}
        else:
            # Identity selection uses the established population/scope/ambiguity contract.
            # Mood/biography still use their existing reader, but no duplicate health/work read.
            base = await self.interaction._look_legacy({'object':raw_object,'aspects':list(dict.fromkeys(['identity',*legacy])),'detail':detail,'offset':offset})
            if not base.get('facts'):
                return base
        return await self._enrich(base, obj, args, aspects, detail, direct=direct)

    async def _enrich(self, base, obj, args, aspects, detail, *, direct=False):
        cell = obj['kind'] == 'cell'
        state = [aspect for aspect in aspects if aspect in STATE_ASPECTS]
        stats = args.get('stats')
        base = copy.deepcopy(base)
        base['requested_aspects'] = aspects
        base['not_read'] = [s for s in base.get('not_read',[]) if not s.startswith('aspects not requested:')]
        base['state_observations'] = []
        base['consistency'] = 'Selection and state have separate samples below; no atomic snapshot or silent merge across loads.'
        if direct:
            base['consistency'] = 'Identity and state come from the reported native observation; this is not an atomic snapshot.'
        facts = base['facts']
        for offset in range(0,len(facts),8):
            batch = facts[offset:offset+8]
            if direct:
                target = {'kind':obj['kind']}
                target['id' if 'ids' in obj else 'name'] = obj.get('ids',obj.get('names'))[0]
                if 'map' in obj:target['map'] = obj['map']
                elif obj['kind'] == 'thing' and 'names' in obj:target['map'] = 'current'
                targets = [target]
            else:
                targets = [self._target(f['object'],obj) for f in batch]
            request = {'targets':targets,'sections':state or ['identity'],
                       'include_fogged':False if cell else obj.get('include_fogged',True),
                       'detail':'details' if detail else 'summary'}
            if direct:
                request['exact_lookup'] = True
            if detail:
                request['detail_limit'] = detail
            for key in ('stats','actor','user'):
                if key in args:request[key] = copy.deepcopy(args[key])
            native = await self.interaction.execute_native(request,tool_name='inspect_state')
            data = order_world_fields(native['data'])
            base['state_observations'].append({'observation_id':native['observation_id'],'sample':data.get('sample'), 'status':native['status'],'context':data.get('context'),'issues':native.get('issues',[]),'read_error':data.get('read_error'),'changed_during_read':data.get('changed_during_read',False)})
            if native['status'] != 'ok':base['status'] = 'partial'
            rows = data.get('objects',[])
            if len(rows) > len(batch) or native['status'] == 'ok' and len(rows) != len(batch):
                raise SurfaceError('native state response does not match the requested target count')
            if len(rows) < len(batch):
                base['status'] = 'partial'
                base['state_failure'] = {key:copy.deepcopy(data[key]) for key in ('read_error','resolution','changed_during_read') if key in data}
                rows = [*rows, *({'status':'unavailable','reason':'state_read_incomplete','requested_target':target} for target in targets[len(rows):])]
            for item,row in zip(batch,rows):
                item['state_sample'] = {'tick':row.get('sample_tick'),'observation_id':native['observation_id']}
                if row.get('status') in ('unavailable','unsupported') or 'facets' not in row and not cell:
                    item['state'] = copy.deepcopy(row)
                    if direct and obj.get('names') and re.fullmatch(r'Thing_[A-Za-z0-9_]+\d+', obj['names'][0]):
                        base.setdefault('input_hints', []).append({
                            'code':'possible_id_in_name', 'field':'object.name' if 'name' in args['object'] else 'object.names',
                            'value':obj['names'][0], 'suggested_field':'object.id',
                            'message':'This unresolved exact display name resembles a stable Thing ID. If you intended an ID, retry with object.id; names are never silently coerced.'})
                    base['status'] = 'partial'
                    continue
                if cell:
                    item['facts']['state' if 'state' in state else 'identity'] = row if 'state' in state else row['target']
                    continue
                if direct:
                    identity = row.get('target',{})
                    if not identity.get('id') or 'ids' in obj and identity['id'] != obj['ids'][0]:
                        raise SurfaceError('native state target identity mismatch')
                    item['object'] = self._identity(identity)
                    item['facts']['identity'] = {'object':copy.deepcopy(item['object'])}
                    base['coverage'].update({'matched':1,'listing':'complete','scan':'complete'})
                if row.get('target',{}).get('id') != item['object'].get('id'):
                    raise SurfaceError('native state target identity mismatch')
                item['state_coverage'] = row.get('coverage')
                facets = row['facets']
                pawn_state = facets.get('pawn')
                if isinstance(pawn_state,dict) and pawn_state.get('status')=='observed' and 'health_summary_fraction' in pawn_state:
                    pawn_state['field_semantics'] = {
                        'health_summary_fraction':'Native overall health summary, not proof of no injuries or impaired capacities.',
                        'bleed_fraction_per_game_day':'Blood loss fraction per game day, not per second or a survival forecast.',
                        'pain_fraction':'Current pain fraction, separate from injury severity and blood loss.',
                    }
                if 'state' in state:
                    visible_facets = ({k:v for k,v in facets.items() if k not in ('pawn', 'work', 'stats')}
                                      if pruning_requested() else facets)
                    item['facts']['state'] = {'facets':visible_facets,'unsupported_components':row.get('unsupported_components')}
                if 'state' not in state or pruning_requested():
                    pawn = facets.get('pawn',{})
                    for aspect in state:
                        if aspect == 'state':
                            continue
                        if aspect == 'work':value = facets.get('work',{'status':'not_applicable'})
                        elif pawn.get('status') != 'observed':value = pawn or {'status':'not_applicable'}
                        elif aspect == 'health':value = {k:v for k,v in pawn.items() if not _belongs(k, (*GEAR_FIELDS,'skills','traits'))}
                        elif aspect == 'skills':value = {k:v for k,v in pawn.items() if k == 'status' or _belongs(k, ('skills',))}
                        else:value = {k:v for k,v in pawn.items() if k == 'status' or _belongs(k, GEAR_FIELDS)}
                        item['facts'][aspect] = value
                if stats is not None:
                    item['facts']['stats'] = facets.pop('stats',{'status':'not_applicable'}) if 'state' in state else facets.get('stats',{'status':'not_applicable'})
        base['coverage']['state_facets_read_successfully'] = all(f.get('state_coverage',{}).get('registered_facets_read_successfully') is True for f in facts) if not cell else base['status']=='ok'
        if base['coverage']['state_facets_read_successfully'] is False:
            base['status'] = 'partial'
        base['not_read'].append('Omitted aspects are not evidence of absence. stats are computed only when requested; contextual predicates require the relevant actor/user.')
        return self._fit_page(self._normalize_identities(base), args)

    @staticmethod
    def _canonical_identity(value):
        """Add one stable location shape while retaining every legacy field.

        A held position never substitutes for an unavailable own position.
        The enclosing map can complete an own-position cell only when the
        returned identity explicitly says the Thing is spawned.
        """
        identity = copy.deepcopy(value)
        if not isinstance(identity, dict) or not identity.get('id'):
            return identity
        status = identity.get('status') if isinstance(identity.get('status'), dict) else {}
        raw = status.get('position') if isinstance(status.get('position'), dict) else identity.get('position')
        def cell(position, *, own=False):
            if not isinstance(position, dict) or any(type(position.get(k)) is not int for k in ('x','z')):
                return None
            map_id = position.get('map', position.get('map_id'))
            if not map_id and own and identity.get('spawned') is True:
                map_id = identity.get('map_id') or (identity.get('map_held') or {}).get('id') or identity.get('map')
            if not isinstance(map_id, str) or not map_id:
                return None
            return {'kind':'cell', 'map':map_id, 'x':position['x'], 'z':position['z']}
        own = cell(raw, own=True)
        if identity.get('spawned') is False:
            own = None
        held = cell(identity.get('held_position'))
        identity['location'] = {'position':own, 'held_position':held}
        # Keep both map spellings for clients already using either path.
        if own is not None:
            if isinstance(identity.get('position'), dict):
                identity['position'] = {**identity['position'], **own, 'map_id':own['map']}
            if isinstance(status.get('position'), dict):
                identity['status'] = {**status, 'position':{**status['position'], **own, 'map_id':own['map']}}
        return identity

    @classmethod
    def _normalize_identities(cls, result):
        result = copy.deepcopy(result)
        for row in result.get('facts', []):
            if isinstance(row.get('object'), dict):
                row['object'] = cls._canonical_identity(row['object'])
            identity = row.get('facts', {}).get('identity')
            if isinstance(identity, dict) and isinstance(identity.get('object'), dict):
                identity['object'] = cls._canonical_identity(identity['object'])
        return result

    @staticmethod
    def _fit_page(base, arguments=None):
        facts = base.get('facts',[])
        original = len(facts)
        while (len(canonical(base)) > MAX_SOURCE_CHARS if requested() else False) or model_size(base, arguments=arguments) > MAX_TOOL_RESULT_CHARS - 4096:
            if len(facts) <= 1:
                raise SurfaceError('Object facts exceed output bound; select fewer aspects or smaller detail')
            facts.pop()
        if len(facts) < original:
            base['coverage']['listing'] = 'partial'
            page = base.setdefault('page',{})
            page['returned'] = len(facts)
            page['has_more'] = True
            page['next_offset'] = page.get('offset',0) + len(facts) if base['coverage'].get('scan')=='complete' else None
            page['reduced_for_size'] = True
            base['not_read'].append('Additional object rows omitted for output size; use page.next_offset. Returned object facts are not pruned.')
        return base

    @staticmethod
    def _identity(native):
        identity = copy.deepcopy(native)
        identity['definition'] = {'def_name':native['definition'],'label':native.get('definition_label')}
        identity['map_id'] = native.get('map')
        identity['map_held'] = {'id':native['map']} if native.get('map') else None
        membership = native.get('pawn_membership') or {}
        identity.update(membership)
        if native.get('kind') == 'pawn':
            identity['is_prisoner_of_colony'] = membership.get('is_prisoner')
            identity['is_slave_of_colony'] = membership.get('is_slave')
            identity['status'] = {'position':copy.deepcopy(native.get('position'))}
        else:
            identity['label'] = native.get('name')
        return identity

    @staticmethod
    def _target(identity,obj):
        if obj['kind']=='cell':return copy.deepcopy(obj)
        target = {'id':identity['id']}
        map_id = identity.get('map_id') or (identity.get('map_held') or {}).get('id') or obj.get('map')
        if map_id:target['map'] = map_id
        return target
