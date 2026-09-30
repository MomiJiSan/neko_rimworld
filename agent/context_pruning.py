"""Request-scoped content pruning. Model messages remain ordinary JSON.

No dictionaries, references, patches, model summarizer, or additional reads.
Raw tool results remain in the audit. Never merge observations or infer facts.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from .model_context import canonical, digest

GUIDE = (
    "Tool results are ordinary JSON. Repeated field explanations and exact duplicate "
    "copies are omitted. Scope, samples, nulls and incomplete coverage still apply. "
    "Routes return path measurements; environment and incidental objects are opt-in "
    "in that same call. Full body-part lists, spatial "
    "geometry and implementation diagnostics are opt-in in the original call; "
    "their omission is not a complete anatomy/geometry inspection."
)


def _subset(small, large):
    return isinstance(small, dict) and isinstance(large, dict) and all(
        k in large and (v == large[k] or _subset(v, large[k])) for k, v in small.items()
    )


def _aliases(x):
    """Remove copies only when their retained counterpart has equal facts."""
    if isinstance(x, list):
        return [_aliases(v) for v in x]
    if not isinstance(x, dict):
        return x
    out = {k: _aliases(v) for k, v in x.items()}
    if isinstance(out.get('scope'), dict) and out.get('object') == out['scope'].get('object'):
        out.pop('object', None)
    if isinstance(out.get('facts'), dict) and isinstance(out.get('object'), dict):
        identity = out['facts'].get('identity')
        if isinstance(identity, dict) and set(identity) == {'object'} and _subset(identity['object'], out['object']):
            out['facts'].pop('identity')
    # Legacy profile exposes both profile.age and age, etc.
    if isinstance(out.get('profile'), dict):
        for key in ('age', 'backstories', 'traits'):
            if key in out and key in out['profile'] and out[key] == out['profile'][key]:
                out.pop(key)
    if isinstance(out.get('location'), dict):
        for key in ('position', 'held_position'):
            v = out.get(key)
            retained = out['location'].get(key)
            if isinstance(v, dict) and isinstance(retained, dict):
                normalized = {k: z for k, z in v.items() if k != 'map_id'}
                if normalized == retained and v.get('map_id', retained.get('map')) == retained.get('map'):
                    out.pop(key)
        if isinstance(out.get('status'), dict) and set(out['status']) == {'position'}:
            pos = out['status']['position']
            retained = out['location'].get('position')
            if isinstance(pos, dict) and isinstance(retained, dict) and {
                k: z for k, z in pos.items() if k != 'map_id'
            } == retained and pos.get('map_id', retained.get('map')) == retained.get('map'):
                out.pop('status')
    if 'grid_distance' in out and out.get('grid_distance') == out.get('path_length'):
        out.pop('grid_distance')
    if isinstance(out.get('pawn_membership'), dict) and _subset(out['pawn_membership'], out):
        out.pop('pawn_membership')
    return out


def _identity(value):
    if not isinstance(value, dict):
        return value
    return {k: copy.deepcopy(v) for k, v in value.items() if k in {
        'id', 'name', 'kind', 'definition', 'map', 'map_id', 'map_held',
        'location', 'position', 'held_position', 'spawned', 'destroyed', 'position_kind', 'faction',
    }}


def route_view(value, *, environment=False, objects=False):
    """Return the requested read categories, not every incidental native read."""
    omit = set()
    if not environment:
        omit.update({'environment_contexts', 'region', 'environment', 'path_environment'})
    if not objects:
        omit.update({'objects', 'objects_summary', 'object_state_scope', 'page'})
    result = {k: copy.deepcopy(v) for k, v in value.items() if k not in omit}
    result['environment_included'] = environment
    result['objects_included'] = objects
    return result


def activity_observations(data):
    """Drop the old duplicate candidate graph/rankings, retaining every read."""
    if not isinstance(data.get('reads'), dict) or not isinstance(data.get('candidates'), list):
        return data
    emitted, rows = set(), []
    for candidate in data['candidates']:
        for stage in candidate.get('stages', []):
            key = stage.get('read')
            if key in emitted or key not in data['reads']:
                continue
            emitted.add(key)
            rows.append({**{k: copy.deepcopy(v) for k, v in stage.items() if k != 'read' and v is not None},
                         'observation': data['reads'][key]})
    if emitted != set(data['reads']):
        return data  # Unknown/unbound extension must not silently disappear.
    return {**{k: v for k, v in data.items() if k not in ('reads', 'candidates', 'comparisons')}, 'observations': rows}


def focus_facts(data, focus):
    """Select explicitly requested named measurements; never guess relevance."""
    if not focus or not isinstance(data, dict):
        return
    for row in data.get('facts', []):
        facts = row.get('facts', {})
        facets = facts.get('state', {}).get('facets', {})
        collections = {
            'skills': (facts.get('skills', facets.get('pawn', {})), 'skills'),
            'work': (facts.get('work', facets.get('work', {})), 'work_types'),
            'capacities': (facts.get('health', facets.get('pawn', {})), 'capacities'),
        }
        for name, requested in focus.items():
            if name not in collections:
                continue
            parent, key = collections[name]
            values = parent.get(key) if isinstance(parent, dict) else None
            collection = values if isinstance(values, dict) and isinstance(values.get('rows'), list) else None
            if collection is not None:
                values = collection['rows']
            if not isinstance(values, list):
                if isinstance(parent, dict) and parent:
                    parent[key + '_selection'] = {'requested': list(requested), 'status': 'unavailable',
                        'reason': 'collection_not_observed_in_this_read'}
                continue  # Preserve unavailable/legacy values without invention.
            def matches(v, term):
                return isinstance(v, dict) and term in (v.get('definition'), v.get('def_name'), v.get('label'))
            selected = [v for v in values if any(matches(v, term) for term in requested)]
            source_complete = (parent.get(key + '_complete') is True if collection is None else
                               collection.get('listing') == 'complete' and collection.get('state') in ('complete', 'empty'))
            if collection is None:
                parent[key] = selected
            else:
                collection['rows'] = selected
                collection['returned'] = len(selected)
                collection['matched'] = len(selected) if source_complete else None
                collection['known_matches'] = len(selected)
                collection['listing'] = 'complete' if source_complete else 'partial'
                collection['state'] = 'complete' if source_complete else 'partial'
                collection['has_more'] = False if source_complete else None
                collection['offset'] = 0
                collection.setdefault('scope', {})['display_selection'] = list(requested)
            parent[key + '_selection'] = {'requested': list(requested),
                'unmatched': [term for term in requested if not any(matches(v, term) for v in values)],
                'scope': 'requested names only; completeness applies to this selection. Unmatched has no observed value, not zero or disabled.'}
            parent[key + '_complete'] = source_complete
            parent[key + '_total'] = len(selected)


def prune(value, arguments=None, *, seen_notes=None):
    """Filter content by the actual operation/request, not task-name keywords."""
    arguments = arguments or {}
    included = arguments.get('include', [])
    diagnostic = 'diagnostics' in included
    geometry = 'geometry' in included
    body_parts = 'body_parts' in included
    seen = set() if seen_notes is None else set(seen_notes)
    if isinstance(value, dict) and value.get('tool') not in (None, 'look', 'count', 'inspect_route', 'inspect_space', 'inspect_state', 'survey_space', 'evaluate_activity'):
        return copy.deepcopy(value), seen, []
    removed = []
    value = _aliases(copy.deepcopy(value))
    tool = value.get('tool') if isinstance(value, dict) else None
    data = value.get('data', value) if isinstance(value, dict) else value

    if tool == 'evaluate_activity' and 'data' in value and isinstance(data, dict):
        value['data'] = data = activity_observations(data)
        for row in data.get('observations', []):
            if row.get('kind') == 'actor_state':
                focus_facts(row.get('observation'), arguments.get('actor_focus', {}))

    if tool == 'count' and isinstance(data, dict) and isinstance(data.get('listing'), list):
        # Count's facts list contains a second identity-only copy of the listing.
        facts = data.get('facts', [])
        listing = {r.get('id'): r for r in data['listing'] if isinstance(r, dict) and r.get('id')}
        if facts and all(isinstance(r, dict) and not r.get('facts') and
                         _subset(r.get('object'), listing.get(r.get('object', {}).get('id')))
                         for r in facts):
            data.pop('facts')
            removed.append('duplicate_count_identities')

    if tool == 'look' and isinstance(data, dict):
        focus_facts(data, arguments.get('focus', {}))
        aspects = arguments.get('aspects', data.get('requested_aspects', []))
        if aspects and 'identity' not in aspects:
            for row in data.get('facts', []):
                if isinstance(row, dict) and isinstance(row.get('object'), dict):
                    row['object'] = _identity(row['object'])
                    row.get('facts', {}).pop('identity', None)
            removed.append('unrequested_identity_details')

    explicit_identity = tool == 'count' or 'identity' in arguments.get('aspects', [])

    if tool == 'inspect_route' and 'data' in value and isinstance(data, dict):
        value['data'] = data = route_view(data, environment='environment' in included,
            objects=bool(arguments.get('include_objects') or arguments.get('cursor') or 'objects' in included))

    def note(v):
        if not isinstance(v, str):
            return v
        if v in seen:
            return None
        seen.add(v)
        return v

    def walk(x, path=()):
        if isinstance(x, list):
            return [walk(v, path + (i,)) for i, v in enumerate(x)]
        if not isinstance(x, dict):
            return x
        x = dict(x)
        if not diagnostic and not geometry and not explicit_identity and 'id' in x and any(k in x for k in ('position', 'held_position', 'location')):
            x = _identity(x)
        if not diagnostic:
            # Internal implementation taxonomy is not the requested game state.
            # Keep count/explicit identity types, including parent_holder types.
            if 'id' in x and ('position' in x or 'held_position' in x or 'location' in x):
                x.pop('capability_meanings', None)
                if not explicit_identity:
                    x.pop('runtime_type', None)
                    x.pop('holder_chain', None)
            if 'unsupported_components' in x and isinstance(x['unsupported_components'], list):
                x['unsupported_components_count'] = len(x.pop('unsupported_components'))
        if not body_parts and all(isinstance(x.get(k), list) for k in ('conditions', 'damaged_present_parts', 'missing_parts', 'capacities')) and isinstance(x.get('body_parts'), list):
            # Keep all conditions, missing/damaged parts and capacities, plus
            # anatomy referred to by those conditions. Healthy anatomy is not
            # automatically a second full list for every health consultation.
            indices = {r.get('part') for r in x['conditions'] if isinstance(r, dict) and type(r.get('part')) is int}
            indices.update(r.get('index') for r in x['damaged_present_parts'] if isinstance(r, dict) and type(r.get('index')) is int)
            x['condition_part_details'] = [v for v in x.pop('body_parts') if not isinstance(v, dict) or v.get('index') in indices]
            x['body_parts_included'] = False
            x.pop('body_parts_complete', None)
            removed.append(list(path + ('body_parts',)))
        if not body_parts and isinstance(x.get('body_parts'), dict) and isinstance(x['body_parts'].get('rows'), list) and 'summary' in x and 'conditions' in x:
            parts = copy.deepcopy(x['body_parts'])
            original = parts['rows']
            # Historical query snapshots use collection envelopes. Retain every
            # damaged or unknown body part; do not turn a partial list complete.
            parts['rows'] = [r for r in original if not isinstance(r, dict) or
                type(r.get('health')) not in (int, float) or type(r.get('max_health')) not in (int, float) or
                r['health'] != r['max_health'] or r.get('missing') is True]
            parts['returned'] = len(parts['rows'])
            if len(parts['rows']) != len(original):
                parts['listing'] = 'partial'
            parts['display_filter'] = 'damaged, missing or unknown; full body_parts not requested'
            x['body_parts'] = parts
        if not geometry and 'gases' in x and 'coverage' in x:
            for key in ('path_environment_bands', 'roof_support_objects'):
                if key in x:
                    x.pop(key)
                    x.pop(key + '_complete', None)
                    x[key + '_included'] = False
                    removed.append(list(path + (key,)))
            for group in ('attack_influences', 'local_hazards'):
                if isinstance(x.get(group), list):
                    filtered = []
                    for row in x[group]:
                        if not isinstance(row, dict):
                            filtered.append(row)
                            continue
                        row = dict(row)
                        # Per-ray/node debug traces are not the hazard result.
                        # Keep affected counts, explicit hazard cells, status,
                        # source identity and every uncertainty/coverage flag.
                        if isinstance(row.get('source'), dict) and row.get('checked_from') == row['source'].get('position'):
                            row.pop('checked_from', None)
                        if isinstance(row.get('path_indices'), list):
                            row['path_indices_count'] = len(row.pop('path_indices'))
                        if 'source' in row:
                            row['source'] = _identity(row['source'])
                        filtered.append(row)
                    x[group] = filtered
        if not geometry and all(k in x for k in ('status', 'start', 'end', 'steps')) and isinstance(x.get('segments'), list):
            x['door_segments'] = [s for s in x.pop('segments') if not isinstance(s, dict) or s.get('door') is not None]
            x['geometry_included'] = False
            removed.append(list(path + ('segments',)))
        out = {}
        for key, child in x.items():
            # Measurement/debug metadata is not a game observation. Keep the
            # full original result and omission paths outside model context.
            if not diagnostic and key in ('read_ms', 'cooperative_budget_overrun'):
                removed.append(list(path + (key,)))
                continue
            if key in ('extension', 'extensions', 'unknown_mod_fields'):
                out[key] = copy.deepcopy(child)
            elif key in ('semantics', 'notes') and isinstance(child, list) and all(isinstance(s, str) for s in child):
                kept = [v for s in child if (v := note(s)) is not None]
                if kept:
                    out[key] = kept
                if len(kept) != len(child):
                    removed.append(list(path + (key,)))
            elif key == 'field_semantics' and isinstance(child, dict) and all(isinstance(s, str) for s in child.values()):
                kept = {k: v for k, s in child.items() if (v := note(s)) is not None}
                if kept:
                    out[key] = kept
            elif key in ('meaning', 'work_types_meaning', 'medical_care_context') and isinstance(child, str):
                if (v := note(child)) is not None:
                    out[key] = v
            else:
                out[key] = walk(child, path + (key,))
        return out

    return walk(value), seen, removed


@dataclass(frozen=True)
class PrunedEvidence:
    content: str
    source_chars: int
    source_sha256: str
    omissions: tuple

    @property
    def view_chars(self):
        return len(self.content)

    def metrics(self):
        return dict(format='json', projection='request_scoped_content', source_chars=self.source_chars,
                    view_chars=self.view_chars, source_sha256=self.source_sha256,
                    view_sha256=digest(self.content), omission_count=len(self.omissions),
                    omissions=list(self.omissions[:128]))


class ContextPruner:
    guide = GUIDE

    def __init__(self):
        self.seen_notes = set()

    def compile(self, value, *, arguments=None, max_view_chars=None):
        raw = canonical(value)
        projected, seen, omitted = prune(value, arguments, seen_notes=self.seen_notes)
        content = canonical(projected)
        if max_view_chars is not None and len(content) > max_view_chars:
            raise ValueError('evidence_view_limit')
        self.seen_notes = seen
        return PrunedEvidence(content, len(raw), digest(raw), tuple(omitted))
