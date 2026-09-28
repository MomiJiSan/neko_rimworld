"""Evaluate bounded activities, retaining evidence roles and uncertainty."""
from __future__ import annotations

import asyncio
import copy
import math
import json
import time
import uuid
from collections.abc import Mapping
from typing import Protocol

from .activity_contracts import ActivityError, canonical_ref, compile_plan, normalize_activity

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_EVALUATION_SECONDS = 45


class ActivityDataSource(Protocol):
    source_kind: str
    async def resolve(self, activity): ...
    async def request(self, operation, arguments): ...


def _status(value):
    if not isinstance(value, Mapping): return 'partial'
    if (value.get('error') or {}).get('code') == 'world_changed': return 'world_changed'
    return value.get('status') if value.get('status') in ('ok','world_changed') else 'partial'


def _pick(value, keys):
    return {key:copy.deepcopy(value[key]) for key in keys if key in value}


def _route_summary(value):
    data=value.get('data') if isinstance(value.get('data'), Mapping) else value
    route=data.get('route',{});env=data.get('path_environment')
    result=dict(status=_status(value),observation_id=value.get('observation_id'),
                sample=copy.deepcopy(value.get('sample') or data.get('sample')),issues=copy.deepcopy(value.get('issues',[])))
    if value.get('error'): result['error']=copy.deepcopy(value['error'])
    result['route']=_pick(route,('status','actor','target','start','end','end_mode','path_tick','steps','path_length','native_cost','hypothetical_start','meaning'))
    for key in ('actor','target'):
        if isinstance(result['route'].get(key),dict):
            result['route'][key]=_pick(result['route'][key],('id','name','kind','map','position','x','z','held_position'))
    result['context']=copy.deepcopy(data.get('context',{}))
    result['changed_during_read']=data.get('changed_during_read',False)
    if isinstance(env,Mapping):
        # Keep every registered factor/coverage flag; only verbose geometry and
        # identity listings stay in the raw observation, not in model history.
        result['path_environment']={k:copy.deepcopy(v) for k,v in env.items()
            if k not in ('path_environment_bands','roof_support_objects')}
        result['path_environment']['detail_not_shown']=[k for k in ('path_environment_bands','roof_support_objects') if k in env]
        result['path_hazards']=copy.deepcopy(env.get('gases'))
    else:
        result['path_environment']=dict(status='not_computed',reason='no_path' if route.get('status')=='not_found' else 'not_returned')
        result['path_hazards']=None
    result['not_shown']=['route.segments','corridor.environment','corridor.objects','region']
    result['meaning']='Path-cell evidence only. Nearby corridor objects are not path evidence; omitted facts remain uninspected in this view.'
    return result


def _compact_object_data(data):
    """Keep model-visible object facts bounded; raw payload stays in the ledger."""
    compact=copy.deepcopy(data)
    observations=[]
    for item in compact.get('state_observations', []):
        if not isinstance(item, Mapping):
            continue
        observations.append({key:copy.deepcopy(item[key]) for key in
                            ('observation_id','sample','status','context','issues','changed_during_read')
                            if key in item})
    compact['state_observations']=observations
    # Selection/page metadata is useful for scope, but raw reader diagnostics
    # and repeated identity projections are retained by the observation ledger.
    for key in ('resolution', 'consistency'):
        compact.pop(key, None)
    for item in compact.get('facts', []):
        if not isinstance(item, Mapping):
            continue
        health=item.get('facts', {}).get('health')
        if isinstance(health, dict) and 'body_parts' in health:
            health.pop('body_parts')
            health['body_parts_not_shown']=True
    return compact


def _object_summary(value, arguments=None):
    # ObjectReader returns direct facts; replay adapters may retain an envelope.
    data=copy.deepcopy(value.get('data') if isinstance(value.get('data'), Mapping) else value)
    observations=data.get('state_observations', [])
    facts=data.get('facts', [])
    arguments=arguments or {}
    target=arguments.get('object', {})
    valid=bool(facts) and bool(observations) and _status(value)=='ok'
    observation_ids={o.get('observation_id') for o in observations if isinstance(o, Mapping)}
    for row in facts:
        obj=row.get('object', {})
        sample=row.get('state_sample', {})
        valid=valid and sample.get('observation_id') in observation_ids and bool(sample.get('observation_id'))
        if target.get('kind')=='cell':
            cell=row.get('facts', {}).get('state', {}).get('target', {})
            valid=valid and all(cell.get(k)==target.get(k) for k in ('x','z','map'))
        else:
            valid=valid and obj.get('id')==target.get('id') and bool(obj.get('id'))
            valid=valid and row.get('state_coverage', {}).get('registered_facets_read_successfully') is True
        valid=valid and all(aspect in row.get('facts', {}) for aspect in arguments.get('aspects', []))
        if arguments.get('stats'):
            stats=row.get('facts', {}).get('stats', {})
            valid=valid and stats.get('complete') is True
    for observation in observations:
        sample=observation.get('sample') or {}
        valid=valid and observation.get('status')=='ok' and not observation.get('changed_during_read')
        valid=valid and bool(sample.get('load_epoch')) and sample.get('started_tick') is not None
        valid=valid and sample.get('started_tick')==sample.get('finished_tick')
        context=observation.get('context') or {}
        for role in ('actor','user'):
            if arguments.get(role):
                valid=valid and context.get(role+'_id')==arguments[role].get('id')
    ids=[o.get('observation_id') for o in observations]
    stat_values=[]
    for row in facts:
        source=row.get('facts', {}).get('stats')
        if not isinstance(source, Mapping):
            source=row.get('facts', {}).get('state', {}).get('facets', {}).get('stats')
        if isinstance(source, Mapping) and isinstance(source.get('values'), list):
            stat_values.extend(copy.deepcopy(source['values']))
    compact_data=_compact_object_data(data)
    if len(ids) != 1:
        valid=False
    return dict(status=_status(value),data=compact_data,observation_id=ids[0] if len(ids)==1 else None,
                observations=observations,stat_values=stat_values,state_verified=bool(valid),issues=copy.deepcopy(value.get('issues',[])),
                error=copy.deepcopy(value.get('error')),sample=observations[0].get('sample') if len(observations)==1 else None)


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


def _environment_complete(env):
    coverage=env.get('coverage',{})
    return (env.get('path_cell_coverage',{}).get('complete') is True
            and coverage.get('cell_scan_complete') is True and coverage.get('visibility_complete') is True
            and coverage.get('changes_detected') is False)



def _evidence_for(stage, result, required_metrics):
    status=result.get('status')
    state_payload = result.get('state_verified') is True and bool(result.get('observation_id'))
    route_payload = bool(result.get('observation_id')) and isinstance(result.get('route'), Mapping)
    required=[];observed=[];missing=[]
    if stage['kind']=='actor_state':
        required=['actor_state']; observed=['actor_state'] if status=='ok' and state_payload else []; missing=[] if status=='ok' and state_payload else ['actor_state']
    elif stage['kind']=='destination_state':
        required=['destination_state']; observed=['destination_state'] if status=='ok' and state_payload else []; missing=[] if status=='ok' and state_payload else ['destination_state']
    else:
        sample=result.get('sample') or {}
        sampled=bool(sample.get('load_epoch')) and sample.get('started_tick') is not None and sample.get('started_tick')==sample.get('finished_tick')
        required=['route']; observed=['route'] if status=='ok' and route_payload and sampled and not result.get('changed_during_read') and result.get('route',{}).get('status')=='found' else []
        if not observed: missing.append('route')
        if 'path_hazards' in required_metrics:
            required.append('path_environment')
            if _environment_complete(result.get('path_environment',{})) and isinstance(result.get('path_hazards'),list) and all(isinstance(x,dict) and _finite(x.get('cells')) for x in result['path_hazards']):
                observed.append('path_environment')
            else: missing.append('path_environment.coverage')
        for metric in required_metrics:
            if metric in ('path_hazards', 'observed_stat'): continue
            raw=result.get('route',{}).get(metric)
            unit={'steps':'grid_transitions','path_length':'map_cell_widths','native_cost':'native_path_cost'}[metric]
            if metric=='steps' and _finite(raw): observed.append('metric:'+metric)
            elif isinstance(raw,dict) and raw.get('unit')==unit and _finite(raw.get('value')): observed.append('metric:'+metric)
            else: missing.append('metric:'+metric)
    return dict(stage=stage['kind'],stage_id=stage['stage_id'],candidate_id=stage['candidate_id'],
                actor=copy.deepcopy(stage.get('actor')),subject=copy.deepcopy(stage.get('subject')),
                origin=copy.deepcopy(stage.get('origin')),destination=copy.deepcopy(stage.get('destination')),
                required=required,observed=observed,missing=missing,status='complete' if not missing and status=='ok' else 'partial',
                decision_ready=not missing and status=='ok',observation_id=result.get('observation_id'),evidence_id=result.get('evidence_id'))

def compare_candidates(rows, metrics):
    """No cross-leg ranking, guessed units, null-to-zero or combined hazard score."""
    output={}
    for metric in metrics:
        groups={};unranked=[];seen=set()
        for candidate in rows:
            for stage in candidate['stages']:
                if metric == 'observed_stat':
                    if stage['kind'] != 'actor_state':
                        continue
                    result=stage['result'];route=result.get('route',{})
                else:
                    if stage['kind'] not in ('move','approach','transfer'): continue
                    result=stage['result'];route=result.get('route',{})
                if metric == 'observed_stat':
                    values=[v for v in result.get('stat_values', []) if isinstance(v, Mapping) and v.get('status') == 'observed' and _finite(v.get('value'))]
                    if len(values) != 1:
                        unranked.append(dict(candidate_id=candidate['candidate_id'], stage_id=stage['stage_id'], actor=stage['actor'], reason='stat_unavailable_or_ambiguous'))
                        continue
                    stat=values[0]
                    scope=dict(stage=stage['kind'], actor=stage['actor'], subject=stage['subject'], stat=stat.get('definition'))
                    groups.setdefault(canonical_ref(scope), dict(scope=scope, entries=[]))['entries'].append(dict(candidate_id=candidate['candidate_id'], stage_id=stage['stage_id'], actor=stage['actor'], stat=stat.get('definition'), value=stat['value'], rank=None))
                    continue
                # The same approach observation can support several destinations.
                key=(result.get('evidence_id') or stage['stage_id'],stage['kind'])
                if key in seen: continue
                seen.add(key)
                base=dict(candidate_id=candidate['candidate_id'],stage_id=stage['stage_id'],actor=stage['actor'],
                          subject=stage['subject'],origin=stage['origin'],destination=stage['destination'],
                          evidence_id=result.get('evidence_id'),observation_id=result.get('observation_id'))
                sample=result.get('sample') or {}
                complete=(result.get('status')=='ok' and route.get('status')=='found'
                          and not result.get('changed_during_read') and sample.get('started_tick')==sample.get('finished_tick')
                          and sample.get('started_tick') is not None and bool(sample.get('load_epoch')) and bool(result.get('observation_id')))
                scope=dict(stage=stage['kind'],actor=stage['actor'],subject=stage['subject'],
                           origin=stage['origin'],destination=stage['destination'],
                           tick=sample.get('started_tick'),load_epoch=sample.get('load_epoch'))
                if metric=='native_cost':
                    # Costs compare destinations for one traveller/origin only.
                    scope.pop('destination')
                if metric=='path_hazards':
                    env=result.get('path_environment',{});values=env.get('gases')
                    complete=complete and _environment_complete(env)
                    if not complete or not isinstance(values,list) or any(not isinstance(v,dict) or not _finite(v.get('cells')) for v in values):
                        unranked.append(dict(**base,reason='incomplete_path_environment'));continue
                    # A vector is intentional: gas cells can overlap, and other
                    # hazards cannot be added to gases as a meaningful score.
                    groups.setdefault(canonical_ref(scope),dict(scope=scope,entries=[]))['entries'].append(
                        dict(**base,gases=copy.deepcopy(values),rank=None))
                    continue
                raw=route.get(metric)
                unit={'steps':'grid_transitions','path_length':'map_cell_widths','native_cost':'native_path_cost'}[metric]
                value=raw if metric=='steps' else raw.get('value') if isinstance(raw,dict) else None
                valid_unit=metric=='steps' or isinstance(raw,dict) and raw.get('unit')==unit
                if not complete or not valid_unit or not _finite(value):
                    unranked.append(dict(**base,reason='unavailable_or_incompatible_metric'));continue
                groups.setdefault(canonical_ref(scope),dict(scope=scope,entries=[]))['entries'].append(dict(**base,value=value,unit=unit))
        for group in groups.values():
            if metric=='path_hazards': continue
            group['entries'].sort(key=lambda e:e['value'], reverse=metric == 'observed_stat')
            last=None;rank=0
            for i,entry in enumerate(group['entries']):
                if last is None or entry['value']!=last: rank=i+1
                entry['rank']=rank;last=entry['value']
        output[metric]=dict(groups=list(groups.values()),unranked=unranked,
            status='partial' if unranked else 'observed' if groups else 'not_computed',
            meaning='Ranks concern returned paths or explicitly requested observed stats within each explicit group, not ETA, eligibility or optimality. Hazards are unranked factor vectors; absence of recorded gases is not overall safety.')
    return output


async def evaluate_activity(arguments, source: ActivityDataSource, *, variant='conservative'):
    activity=normalize_activity(arguments,variant)
    started=time.monotonic()
    try:
        async with asyncio.timeout(MAX_EVALUATION_SECONDS):
            resolved=await source.resolve(copy.deepcopy(activity))
    except TimeoutError as error:
        raise ActivityError('activity_deadline', 'Activity identity resolution exceeded its deadline.') from error
    plan=compile_plan(resolved, resolved['destination']['candidates'])
    results={};objects={};status='ok';bytes_read=0;epoch=None
    for request in plan['requests']:
        if bytes_read>=MAX_EVIDENCE_BYTES or time.monotonic()-started>=MAX_EVALUATION_SECONDS:
            status='partial';break
        try:
            async with asyncio.timeout(max(.01,MAX_EVALUATION_SECONDS-(time.monotonic()-started))):
                value=await source.request(request['operation'],request['arguments'])
        except (ActivityError, asyncio.CancelledError): raise
        except Exception as error:
            if getattr(error,'code',None)=='world_changed' or getattr(error,'recoverable',True) is False: raise
            if not isinstance(error,(TimeoutError,)) and not hasattr(error,'code'): raise
            value=dict(status='partial',error=dict(code=getattr(error,'code','activity_deadline'),message=str(error)))
        if not isinstance(value,Mapping): raise ActivityError('activity_invalid_result','Reader returned no object.')
        if _status(value)=='world_changed': raise ActivityError('world_changed','Loaded world changed; discard this evaluation.')
        bytes_read+=len(canonical_ref(value).encode())
        summary=_route_summary(value) if request['operation']=='inspect_route' else _object_summary(value, request['arguments'])
        samples=[summary.get('sample')] + [o.get('sample') for o in summary.get('observations', [])]
        for identity_key in ('colony_id', 'load_epoch', 'connection_epoch'):
            if value.get(identity_key) is not None:
                summary[identity_key] = value[identity_key]
        for sample in samples:
            current=(sample or {}).get('load_epoch')
            if current and epoch and current!=epoch:
                raise ActivityError('world_changed', 'Activity observations belong to different loaded worlds.')
            epoch=current or epoch
        summary.update(evidence_id=summary.get('observation_id') or request['request_id'],request_id=request['request_id'],source_kind=source.source_kind)
        if summary['status']!='ok': status='partial'
        if request['operation']=='look':
            objects[request['request_id']]=summary
            summary=_pick(summary,('status','evidence_id','observation_id','state_verified','stat_values','sample','source_kind','error','colony_id','load_epoch','connection_epoch'))
        results[request['request_id']]=summary
    binding={(b['candidate_id'],b['stage_id']):r['request_id'] for r in plan['requests'] for b in r['bindings']}
    candidates=copy.deepcopy(plan['candidates'])
    for c in candidates:
        for s in c['stages']:
            s['result']=copy.deepcopy(results.get(binding[(c['candidate_id'],s['stage_id'])],
                                                  dict(status='not_computed',reason='activity_budget')))
    evidence=[]
    for candidate in candidates:
        for stage in candidate['stages']:
            evidence.append(_evidence_for(stage,stage['result'],activity['compare']))
    decision_ready=bool(evidence) and all(item['decision_ready'] for item in evidence)
    evidence_status='complete' if decision_ready else 'partial'
    result = dict(status=status,read_status=status,evidence_status=evidence_status,decision_ready=decision_ready,
                activity=dict(evaluation_id=uuid.uuid4().hex,kind=plan['kind'],variant=variant),
                source_kind=source.source_kind,candidates=candidates,objects=objects,evidence=evidence,
                resolution=copy.deepcopy(resolved.get('resolution',{})),
                plan=dict(candidate_count=len(candidates),request_count=len(plan['requests']),limits=plan['limits']),
                comparison=compare_candidates(candidates,activity['compare']),
                coverage=dict(requests_completed=len(results),requests_total=len(plan['requests']),complete=status=='ok',
                              decision_ready=decision_ready),
                unknowns=['Action eligibility and outcome','Carrying effects on speed and path cost','Future hazards'],
                limitations=['Sequential current observations, not an atomic snapshot.','Transfer uses a hypothetical subject-position origin; no pickup or movement occurred.',
                             'Native destination checks use current actor state and position, not simulated post-transfer state.'])
    return _fit_result(result)


def _fit_result(result):
    """Preserve whole candidates within transport; omitted evidence is never complete."""
    # Reserve room for the registry's envelope. Raw native observations remain in
    # the consultation ledger. A smaller explicit evaluation can retrieve omissions.
    limit=120_000
    omitted=[]
    while len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')))>limit:
        if not result['candidates']:
            raise ActivityError('activity_result_too_large', 'A bounded activity result cannot fit the transport.')
        row=result['candidates'].pop()
        omitted.append({k:row[k] for k in ('candidate_id','actor','subject','destination')})
        kept={c['candidate_id'] for c in result['candidates']}
        result['evidence']=[e for e in result['evidence'] if e['candidate_id'] in kept]
        used={s['result'].get('evidence_id') for c in result['candidates'] for s in c['stages'] if s['result'].get('evidence_id')}
        result['objects']={k:v for k,v in result['objects'].items() if v.get('evidence_id') in used}
        result['comparison']=compare_candidates(result['candidates'], list(result['comparison']))
        result.update(status='partial', evidence_status='partial', decision_ready=False)
        result['coverage'].update(complete=False, decision_ready=False, output_complete=False,
                                  omitted_candidates=list(omitted),
                                  reason='activity_output_limit',
                                  recovery='Evaluate the omitted actor/destination pairs separately.')
    result['coverage'].setdefault('output_complete', True)
    return result
