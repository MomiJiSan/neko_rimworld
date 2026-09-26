"""Evaluate bounded activities, retaining evidence roles and uncertainty."""
from __future__ import annotations

import asyncio
import copy
import math
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
    if value.get('error', {}).get('code') == 'world_changed': return 'world_changed'
    return value.get('status') if value.get('status') in ('ok','world_changed') else 'partial'


def _pick(value, keys):
    return {key:copy.deepcopy(value[key]) for key in keys if key in value}


def _route_summary(value):
    data=value.get('data',{});route=data.get('route',{});env=data.get('path_environment')
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


def _object_summary(value):
    data=copy.deepcopy(value.get('data',{}))
    for item in data.get('facts',[]):
        health=item.get('facts',{}).get('health')
        if isinstance(health,dict) and 'body_parts' in health:
            health.pop('body_parts')
            health['body_parts_not_shown']=True
    return dict(status=_status(value),data=data,observation_id=value.get('observation_id'),
                observations=copy.deepcopy(value.get('observations',[])),issues=copy.deepcopy(value.get('issues',[])),
                error=copy.deepcopy(value.get('error')),sample=copy.deepcopy(value.get('sample')))


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


def _environment_complete(env):
    coverage=env.get('coverage',{})
    return (env.get('path_cell_coverage',{}).get('complete') is True
            and coverage.get('cell_scan_complete') is True and coverage.get('visibility_complete') is True
            and coverage.get('changes_detected') is False)



def _evidence_for(stage, result, required_metrics):
    status=result.get('status')
    required=[];observed=[];missing=[]
    if stage['kind']=='actor_state':
        required=['actor_state']; observed=['actor_state'] if status=='ok' else []; missing=[] if status=='ok' else ['actor_state']
    elif stage['kind']=='destination_state':
        required=['destination_state']; observed=['destination_state'] if status=='ok' else []; missing=[] if status=='ok' else ['destination_state']
    else:
        required=['route']; observed=['route'] if status=='ok' and result.get('route',{}).get('status')=='found' else []
        if not observed: missing.append('route')
        if 'path_hazards' in required_metrics:
            required.append('path_environment')
            if _environment_complete(result.get('path_environment',{})) and isinstance(result.get('path_hazards'),list) and all(_finite(x.get('cells')) for x in result['path_hazards'] if isinstance(x,dict)):
                observed.append('path_environment')
            else: missing.append('path_environment.coverage')
        for metric in required_metrics:
            if metric=='path_hazards': continue
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
                if stage['kind'] not in ('move','approach','transfer'): continue
                result=stage['result'];route=result.get('route',{})
                # The same approach observation can support several destinations.
                key=(result.get('evidence_id'),stage['kind'])
                if key in seen: continue
                seen.add(key)
                base=dict(candidate_id=candidate['candidate_id'],stage_id=stage['stage_id'],actor=stage['actor'],
                          subject=stage['subject'],origin=stage['origin'],destination=stage['destination'],
                          evidence_id=result.get('evidence_id'),observation_id=result.get('observation_id'))
                sample=result.get('sample') or {}
                complete=(result.get('status')=='ok' and route.get('status')=='found'
                          and not result.get('changed_during_read') and sample.get('started_tick')==sample.get('finished_tick')
                          and sample.get('started_tick') is not None)
                scope=dict(stage=stage['kind'],subject=stage['subject'],destination=stage['destination'],
                           tick=sample.get('started_tick'),load_epoch=sample.get('load_epoch'))
                if metric=='native_cost':
                    # Preserve the existing contract: actor-dependent search cost
                    # orders destinations for one actor, never different actors.
                    scope['actor']=stage['actor'];scope.pop('destination')
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
            group['entries'].sort(key=lambda e:e['value'])
            last=None;rank=0
            for i,entry in enumerate(group['entries']):
                if last is None or entry['value']!=last: rank=i+1
                entry['rank']=rank;last=entry['value']
        output[metric]=dict(groups=list(groups.values()),unranked=unranked,
            status='partial' if unranked else 'observed' if groups else 'not_computed',
            meaning='Ranks concern returned paths within each explicit group, not ETA or optimality. Hazards are unranked factor vectors; absence of recorded gases is not overall safety.')
    return output


async def evaluate_activity(arguments, source: ActivityDataSource, *, variant='conservative'):
    activity=normalize_activity(arguments,variant)
    resolved=await source.resolve(copy.deepcopy(activity))
    plan=compile_plan(resolved, resolved['destination']['candidates'])
    results={};objects={};status='ok';bytes_read=0;started=time.monotonic()
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
        summary=_route_summary(value) if request['operation']=='inspect_route' else _object_summary(value)
        summary.update(evidence_id=summary.get('observation_id') or request['request_id'],request_id=request['request_id'],source_kind=source.source_kind)
        if summary['status']!='ok': status='partial'
        if request['operation']=='look':
            objects[request['request_id']]=summary
            summary=_pick(summary,('status','evidence_id','observation_id','observations','sample','source_kind','error'))
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
    return dict(status=status,read_status=status,evidence_status=evidence_status,decision_ready=decision_ready,
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
