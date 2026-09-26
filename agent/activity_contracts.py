"""Bounded activity vocabulary; no game reads or task-specific recommendations."""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping

KINDS = ('move', 'transfer')
INSPECTS = ('actor_state', 'approach_route', 'transfer_route', 'destination_state', 'move_route')
METRICS = ('steps', 'path_length', 'native_cost', 'path_hazards')
PREFERENCES = ('shorter_route', 'lower_observed_hazard', 'higher_observed_stat')
DEFAULT_INSPECTS = {'move': ('actor_state', 'move_route', 'destination_state'),
                    'transfer': INSPECTS[:4]}
MAX_ACTORS, MAX_DESTINATIONS, MAX_COMBINATIONS = 4, 8, 16
MAX_READS = 64


class ActivityError(ValueError):
    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code, self.details = code, dict(details or {})


def fail(message, code='invalid_activity_arguments', **details):
    raise ActivityError(code, message, details=details)


def keys(value, allowed, path):
    if not isinstance(value, Mapping):
        fail(f'{path} must be an object.')
    if set(value) - set(allowed):
        fail(f'{path}: unsupported keys {sorted(set(value)-set(allowed))}.')


def text(value, path):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        fail(f'{path} requires nonempty text, at most 256 characters.')
    return value


def strings(value, allowed, path, maximum=8, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        fail(f'{path} requires {minimum}..{maximum} values.')
    if any(not isinstance(v, str) or not v.strip() or len(v)>256 or allowed is not None and v not in allowed for v in value):
        fail(f'{path} contains an unsupported value.')
    if len(set(value)) != len(value):
        fail(f'{path} contains duplicates.')
    return list(value)


def normalize_ref(value, path, *, kinds=('pawn', 'thing', 'cell')):
    keys(value, ('kind','id','name','map','x','z'), path)
    if value.get('kind') not in kinds:
        fail(f'{path}.kind must be one of {kinds}.')
    if value['kind']=='cell':
        if 'id' in value or 'name' in value or any(type(value.get(k)) is not int or not -(2**31)<=value[k]<2**31 for k in ('x','z')):
            fail(f'{path}: cell requires 32-bit x/z and no id/name.')
    else:
        if ('id' in value)==('name' in value) or 'x' in value or 'z' in value:
            fail(f'{path} requires exactly one id or name.')
        text(value.get('id',value.get('name')),path)
    if 'map' in value: text(value['map'],path+'.map')
    return copy.deepcopy(dict(value))


def canonical_ref(ref):
    return json.dumps(ref,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def references(value, path, maximum, kinds):
    if not isinstance(value,list) or not 1<=len(value)<=maximum:
        fail(f'{path} requires 1..{maximum} references.')
    refs=[normalize_ref(v,path,kinds=kinds) for v in value]
    if len({canonical_ref(r) for r in refs})!=len(refs):
        fail(f'{path} contains duplicates.','duplicate_activity_reference')
    return refs


def normalize_activity(arguments, variant='conservative'):
    if variant not in ('conservative','simplified','hybrid'): fail('Unknown activity variant.')
    simple=variant in ('simplified','hybrid')
    common=('kind','subject','stats','start')
    keys(arguments, common + (('actors','destinations','preferences') if simple else ('actor','destination','inspect','compare','detail')), 'activity')
    kind=arguments.get('kind')
    if kind not in KINDS: fail(f'kind must be one of {KINDS}.')
    if simple:
        actors=references(arguments.get('actors'),'actors',MAX_ACTORS,('pawn',))
        dest=arguments.get('destinations')
        preferences=strings(arguments.get('preferences',[]),PREFERENCES,'preferences',3)
        compare=[]
        for p in preferences or ['shorter_route']:
            compare.extend({'shorter_route':['steps','path_length'],'lower_observed_hazard':['path_hazards'],'higher_observed_stat':[]}[p])
        inspect=list(DEFAULT_INSPECTS[kind])
    else:
        actor=arguments.get('actor'); keys(actor,('candidates',),'actor')
        actors=references(actor.get('candidates'),'actor.candidates',MAX_ACTORS,('pawn',))
        dest=arguments.get('destination')
        inspect=strings(arguments.get('inspect',list(DEFAULT_INSPECTS[kind])),DEFAULT_INSPECTS[kind],'inspect',4,1)
        compare=strings(arguments.get('compare',['steps','path_length']),METRICS,'compare',4)
        if arguments.get('detail','summary') not in ('summary','details'): fail('detail must be summary or details.')
        preferences=[]
    if kind=='move' and 'subject' in arguments: fail('move has no subject.')
    if kind=='transfer' and 'start' in arguments: fail('transfer derives its origin from subject; start is not accepted.')
    subject=normalize_ref(arguments.get('subject'),'subject',kinds=('pawn','thing')) if kind=='transfer' else None
    start=normalize_ref(arguments['start'],'start') if 'start' in arguments else None
    stats=strings(arguments.get('stats',[]),None,'stats',8)
    if 'higher_observed_stat' in preferences and not stats: fail('higher_observed_stat requires explicit stats.')
    if stats and 'actor_state' not in inspect: fail('stats requires actor_state inspection.')
    if isinstance(dest,list):
        destination={'candidates':references(dest,'destinations',MAX_DESTINATIONS,('pawn','thing','cell'))}
    else:
        keys(dest,('candidates','selector'),'destination')
        if ('candidates' in dest)==('selector' in dest): fail('Use destination candidates or selector.')
        if 'candidates' in dest:
            destination={'candidates':references(dest['candidates'],'destinations',MAX_DESTINATIONS,('pawn','thing','cell'))}
        else:
            selector=dest['selector'];keys(selector,('kind','capabilities','definition','map'),'selector')
            if selector.get('kind')!='things': fail('selector.kind must be things.')
            if not set(selector)&{'capabilities','definition'}: fail('selector needs capabilities or definition.')
            if 'capabilities' in selector: strings(selector['capabilities'],None,'capabilities',16,1)
            for key in ('map','definition'):
                if key in selector: text(selector[key],key)
            destination={'selector':copy.deepcopy(selector)}
    if len(actors)*len(destination.get('candidates',[]))>MAX_COMBINATIONS:
        fail('Too many actor/destination pairs.','activity_combination_limit',limit=MAX_COMBINATIONS)
    route_inspects={'move_route','approach_route','transfer_route'}
    if compare and not route_inspects.intersection(inspect): fail('Route comparison requires a route inspection.')
    return dict(kind=kind,actors=actors,subject=subject,start=start,destination=destination,
                inspect=sorted(inspect),compare=sorted(set(compare)),stats=stats)


def compile_plan(activity, destinations):
    """Pure expansion; production passes resolved stable references from its Adapter."""
    if not destinations: fail('No destinations.','activity_no_destinations')
    destinations=references(destinations,'resolved destinations',MAX_DESTINATIONS,('pawn','thing','cell'))
    if len(activity['actors'])*len(destinations)>MAX_COMBINATIONS:
        fail('Too many actor/destination pairs.','activity_combination_limit',limit=MAX_COMBINATIONS)
    requests={};candidates=[]
    def stage(row, kind, origin, target, operation, args):
        binding=dict(candidate_id=row['candidate_id'],stage_id=f"{row['candidate_id']}:{kind}",kind=kind,
                     actor=copy.deepcopy(row['actor']),subject=copy.deepcopy(row['subject']),
                     origin=copy.deepcopy(origin),destination=copy.deepcopy(target),hypothetical_start='start' in args)
        row['stages'].append(binding)
        key=hashlib.sha256(canonical_ref({'operation':operation,'arguments':args}).encode()).hexdigest()
        if key not in requests: requests[key]=dict(request_id=key,operation=operation,arguments=copy.deepcopy(args),bindings=[])
        requests[key]['bindings'].append(dict(candidate_id=row['candidate_id'],stage_id=binding['stage_id']))
    for i,actor in enumerate(activity['actors']):
        for j,dest in enumerate(destinations):
            subject=activity['subject'];row=dict(candidate_id=f'c{i}-{j}',actor=actor,subject=subject,destination=dest,stages=[])
            wants=set(activity['inspect'])
            if 'actor_state' in wants:
                args=dict(object=actor,aspects=['health','skills','work'],detail=0)
                if activity['stats']: args['stats']=activity['stats']
                stage(row,'actor_state',None,None,'look',args)
            if 'move_route' in wants:
                args=dict(actor=actor,to=dest,detail='summary')
                if activity['start']: args['start']=activity['start']
                stage(row,'move',activity['start'] or {'kind':'actor_current_position'},dest,'inspect_route',args)
            if 'approach_route' in wants:
                stage(row,'approach',{'kind':'actor_current_position'},subject,'inspect_route',dict(actor=actor,to=subject,detail='summary'))
            if 'transfer_route' in wants:
                stage(row,'transfer',subject,dest,'inspect_route',dict(actor=actor,start=subject,to=dest,detail='summary'))
            if 'destination_state' in wants:
                args=dict(object=dest,aspects=['state'],actor=actor,detail=0)
                # Native user is a Pawn recipient, never an item or cell.
                if subject and subject['kind']=='pawn': args['user']=subject
                elif activity['kind']=='move': args['user']=actor
                stage(row,'destination_state',None,dest,'look',args)
            candidates.append(copy.deepcopy(row))
    if len(requests)>MAX_READS: fail('Activity exceeds the read limit.','activity_read_limit')
    return dict(kind=activity['kind'],activity=copy.deepcopy(activity),candidates=candidates,requests=list(requests.values()),
                limits=dict(max_combinations=MAX_COMBINATIONS,candidate_count=len(candidates),request_count=len(requests)))
