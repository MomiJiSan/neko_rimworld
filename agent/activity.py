"""Compose existing object/path reads; one interface and one copy of each read.

The consultation runtime owns deadlines and loaded-world invalidation. Native
readers own facts, coverage and unknowns. This module owns only role binding,
request reuse and presentation of a bounded move/transfer comparison.
"""
from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from typing import Protocol

from .spatial_ranking import rank_paths
from .tools import MAX_TOOL_RESULT_CHARS, ToolExecutionError, ToolSpec
from .model_context import MAX_SOURCE_CHARS, model_size, enabled

MAX_ACTORS, MAX_DESTINATIONS, MAX_PAIRS = 4, 8, 16
ROUTE_METRICS = ("steps", "path_length", "native_cost")


def _key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _invalid(message):
    raise ToolExecutionError("invalid_activity_arguments", message)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


def _ref(value, kinds=("pawn", "thing", "cell")):
    if not isinstance(value, dict) or set(value) - {"kind", "id", "name", "map", "x", "z"}:
        _invalid("Use a Pawn/Thing id or name, or a cell with x/z and optional map.")
    kind = value.get("kind")
    if kind not in kinds or "map" in value and not _text(value["map"]):
        _invalid("Invalid reference kind or map.")
    if kind == "cell":
        if set(value) & {"id", "name"} or any(type(value.get(k)) is not int or not -(2**31) <= value[k] < 2**31 for k in ("x", "z")):
            _invalid("Cells require 32-bit x/z and no id/name.")
    elif ("id" in value) == ("name" in value) or set(value) & {"x", "z"} or not _text(value.get("id", value.get("name"))):
        _invalid("An object requires exactly one nonempty id or name.")
    return copy.deepcopy(value)


def _refs(values, maximum, kinds=("pawn", "thing", "cell")):
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        _invalid(f"Provide 1..{maximum} references.")
    refs = [_ref(v, kinds) for v in values]
    if len({_key(v) for v in refs}) != len(refs):
        _invalid("Duplicate candidates; use one reference per object.")
    return refs


def normalize_activity(args):
    extra = {'include', 'actor_aspects', 'actor_focus', 'user', 'margin'} if enabled() else set()
    if not isinstance(args, Mapping) or set(args) - {"kind", "actors", "subject", "start", "destinations", "stats", *extra}:
        _invalid("Use kind, actors, destinations, optional subject/start and stats.")
    kind = args.get("kind")
    if kind not in (('move', 'transfer', 'state') if enabled() else ('move', 'transfer')):
        _invalid('kind must be move, transfer' + (' or state.' if enabled() else '.'))
    if kind == "move" and "subject" in args or kind == "transfer" and "start" in args:
        _invalid("move has no subject; transfer starts at subject and accepts no start.")
    if kind == 'state' and any(k in args for k in ('subject', 'start')):
        _invalid('state compares destination state with actor/user context; it has no journey subject/start.')
    actors = _refs(args.get("actors"), MAX_ACTORS, ("pawn",))
    subject = _ref(args.get("subject"), ("pawn", "thing")) if kind == "transfer" else None
    start = _ref(args["start"]) if "start" in args else None
    stats = args.get("stats", [])
    if not isinstance(stats, list) or len(stats) > 8 or any(not _text(v) for v in stats) or len(set(stats)) != len(stats):
        _invalid("stats accepts at most eight distinct StatDef names.")
    destinations = args.get("destinations")
    if isinstance(destinations, list):
        destination = {"candidates": _refs(destinations, MAX_DESTINATIONS)}
    else:
        if not isinstance(destinations, dict) or set(destinations) != {"selector"}:
            _invalid("destinations is a reference list or {selector: {kind: things, ...}}.")
        selector = destinations["selector"]
        if not isinstance(selector, dict) or set(selector) - {"kind", "capabilities", "definition", "map"} or selector.get("kind") != "things" or not set(selector) & {"definition", "capabilities"}:
            _invalid("A selector requires kind=things and capabilities or definition.")
        if any(not _text(selector[k]) for k in ("definition", "map") if k in selector):
            _invalid("Invalid selector definition/map.")
        caps = selector.get("capabilities", [])
        if "capabilities" in selector and (not isinstance(caps, list) or not 1 <= len(caps) <= 16 or any(not _text(v) for v in caps)):
            _invalid("capabilities requires 1..16 names from the connected Mod.")
        destination = {"selector": copy.deepcopy(selector)}
    if len(actors) * len(destination.get("candidates", [])) > MAX_PAIRS:
        _invalid(f"At most {MAX_PAIRS} actor/destination pairs per activity.")
    result = dict(kind=kind, actors=actors, subject=subject, start=start, destination=destination, stats=list(stats))
    if enabled():
        result['user'] = _ref(args['user'], ('pawn',)) if 'user' in args else None
        margin = args.get('margin', 2)
        if type(margin) is not int or not 0 <= margin <= 12:
            _invalid('margin must be an integer in 0..12')
        result['margin'] = margin
        focus = args.get('actor_focus', {})
        if not isinstance(focus, dict) or set(focus) - {'skills', 'work', 'capacities'} or any(
            not isinstance(v, list) or not 1 <= len(v) <= 32 or any(not _text(n) for n in v) for v in focus.values()
        ):
            _invalid('actor_focus selects skills/work/capacities by exact definition names or labels.')
        result['actor_focus'] = copy.deepcopy(focus)
        for key, allowed in (
            ('include', ('approach', 'destination_state', 'environment', 'objects', 'geometry')),
            ('actor_aspects', ('health', 'skills', 'work', 'profile', 'gear', 'mood')),
        ):
            selected = args.get(key, [])
            if not isinstance(selected, list) or any(not isinstance(v, str) or v not in allowed for v in selected) or len(set(selected)) != len(selected):
                _invalid(f'{key} accepts distinct values from {", ".join(allowed)}.')
            result[key] = list(selected)
        result['actor_aspects'] = list(dict.fromkeys([*result['actor_aspects'],
            *({'skills': 'skills', 'work': 'work', 'capacities': 'health'}[k] for k in focus)]))
        if kind == 'move' and 'approach' in result['include']:
            # move already is the actor/start-to-destination leg. A redundant
            # selection must not require a second model request to remove it.
            result['include'].remove('approach')
        if kind == 'state' and set(result['include']) - {'destination_state'}:
            _invalid('state has no paths; approach/environment/objects/geometry require move or transfer.')
    return result


def schema():
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    def ref(kind):
        props = {"kind": {"const": kind}, "map": text}
        result = {"type": "object", "additionalProperties": False, "required": ["kind"], "properties": props}
        if kind == "cell":
            props.update({k: {"type": "integer", "minimum": -(2**31), "maximum": 2**31-1} for k in ("x", "z")})
            result["required"] += ["x", "z"]
        else:
            props.update(id=text, name=text)
            result["oneOf"] = [{"required": ["id"], "not": {"required": ["name"]}}, {"required": ["name"], "not": {"required": ["id"]}}]
        return result
    any_ref = {"oneOf": [ref(k) for k in ("pawn", "thing", "cell")]}
    selector = {"type": "object", "additionalProperties": False, "required": ["kind"],
                "properties": {"kind": {"const": "things"}, "definition": text, "map": text,
                               "capabilities": {"type": "array", "minItems": 1, "maxItems": 16, "items": text}},
                "anyOf": [{"required": ["definition"]}, {"required": ["capabilities"]}]}
    return {"type": "object", "additionalProperties": False, "required": ["kind", "actors", "destinations"],
            "properties": {"kind": {"enum": ["move", "transfer"]},
                           "actors": {"type": "array", "minItems": 1, "maxItems": MAX_ACTORS, "uniqueItems": True, "items": ref("pawn")},
                           "subject": {"oneOf": [ref("pawn"), ref("thing")]}, "start": any_ref,
                           "stats": {"type": "array", "maxItems": 8, "uniqueItems": True, "items": text},
                           "destinations": {"oneOf": [
                               {"type": "array", "minItems": 1, "maxItems": MAX_DESTINATIONS, "uniqueItems": True, "items": any_ref},
                               {"type": "object", "additionalProperties": False, "required": ["selector"], "properties": {"selector": selector}}]}},
            "allOf": [{"if": {"properties": {"kind": {"const": "transfer"}}}, "then": {"required": ["subject"], "not": {"required": ["start"]}}},
                      {"if": {"properties": {"kind": {"const": "move"}}}, "then": {"not": {"required": ["subject"]}}}]}


class ActivityDataSource(Protocol):
    source_kind: str
    async def resolve(self, activity): ...
    async def request(self, operation, arguments): ...


class SemanticActivityDataSource:
    source_kind = "native"

    def __init__(self, interaction):
        self.interaction = interaction

    @staticmethod
    def _identity(obj, kind):
        map_id = obj.get("map") or obj.get("map_id") or (obj.get("map_held") or {}).get("id")
        if not obj.get("id") or not map_id:
            raise ToolExecutionError("activity_reference_unresolved", "Object has no stable ID or loaded map.")
        return dict(kind=kind, id=obj["id"], map=map_id)

    async def _resolve(self, ref, role):
        # IDs/cells are native references, not discovery queries. Preserve the
        # caller's map default; do not replace it with another role's map.
        if "id" in ref or ref["kind"] == "cell":
            return copy.deepcopy(ref)
        request = dict(targets=[ref], sections=["identity"], exact_lookup=True)
        try:
            native = await self.interaction.execute_native(request, tool_name="inspect_state")
        except ToolExecutionError as error:
            if not error.recoverable or error.code == "world_changed":
                raise
            raise ToolExecutionError(error.code, str(error), details={**error.details, "role": role, "reference": ref}) from error
        data = native.get("data") or {}
        rows = data.get("objects", [])
        if native.get("status") != "ok" or len(rows) != 1 or not rows[0].get("target", {}).get("id"):
            raise ToolExecutionError("activity_reference_unresolved", "Choose an exact ID from the returned candidates; do not repeat the same ambiguous name.",
                                     details={"role": role, "reference": ref, "resolution": data.get("resolution"),
                                              "objects": rows, "issues": native.get("issues", [])})
        return self._identity(rows[0]["target"], ref["kind"])

    async def resolve(self, activity):
        activity = copy.deepcopy(activity)
        activity["actors"] = [await self._resolve(ref, f"actors[{i}]") for i, ref in enumerate(activity["actors"])]
        for role in ("subject", "start", *(['user'] if activity.get('user') else [])):
            if activity[role] is not None:
                activity[role] = await self._resolve(activity[role], role)
        destination = activity["destination"]
        if "candidates" in destination:
            refs = [await self._resolve(ref, f"destinations[{i}]") for i, ref in enumerate(destination["candidates"])]
        else:
            value = await self.interaction.look(dict(object=destination["selector"], aspects=["identity"], detail=0))
            if value.get("status") != "ok" or value.get("coverage", {}).get("listing") != "complete":
                raise ToolExecutionError("activity_destination_partial", "Narrow the selector or choose explicit destinations.")
            refs = [self._identity(row["object"], "thing") for row in value.get("facts", [])]
        activity["destination"] = {"candidates": _refs(refs, MAX_DESTINATIONS)}
        activity["actors"] = _refs(activity["actors"], MAX_ACTORS, ("pawn",))
        roles = [*activity["actors"], *refs, activity["subject"], activity["start"], activity.get('user')]
        maps = {r["map"] for r in roles if r and r.get("map") not in (None, "current")}
        if len(maps) > 1:
            raise ToolExecutionError("activity_map_mismatch", "All activity roles must share a loaded map.")
        return activity

    async def request(self, operation, arguments):
        if operation == "look":
            return await self.interaction.look(arguments)
        return await self.interaction._world_read(arguments, operation)


def _compact_identity(value):
    """A location reference, not a second full object inspection."""
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    keys = ("id", "kind", "name", "map", "map_id", "position", "held_position", "spawned", "is_free_colonist", "is_prisoner_of_colony", "is_slave_of_colony")
    result = {key: copy.deepcopy(value[key]) for key in keys if key in value}
    if "definition" in value: result["definition"] = copy.deepcopy(value["definition"])
    return result


def _project(value, operation):
    """One model view per observation; preserve factors, scope and failure causes."""
    raw = value.get("data") if isinstance(value.get("data"), Mapping) else value
    data = {"status": value.get("status", "partial")}
    for key in ("error", "issues", "observation_id", "sample"):
        if key in value:
            data[key] = copy.deepcopy(value[key])
    omitted = list(data.get("not_shown", []))
    if operation == "inspect_route":
        route = copy.deepcopy(raw.get("route", {}))
        if "segments" in route:
            route.pop("segments")
            omitted.append("route.segments")
        # grid_distance is the wire compatibility alias of path_length.
        if "path_length" in route:
            route.pop("grid_distance", None)
        # Actor, target and full prose meanings are already bound by the stage.
        # Keep the route numbers/units; native geometry remains in the ledger.
        for key in ("actor", "target", "meaning", "grid_distance", "read_ms", "cooperative_budget_overrun", "segments_coverage"):
            if key in route:
                route.pop(key)
                omitted.append("route." + key)
        for key in ("context", "environment_contexts", "objects", "objects_summary", "object_state_scope", "environment", "region", "page", "consistency", "delivery_sample"):
            if key in data:
                data.pop(key)
                omitted.append(key)
        env = copy.deepcopy(raw.get("path_environment") or {})
        for key in ("path_environment_bands", "roof_support_objects", "rooms", "field_semantics", "roof_support_objects_format", "map_context", "water_cells", "snow_cells", "polluted_cells", "glow_fraction"):
            if key in env:
                env.pop(key)
                omitted.append("path_environment." + key)
        # Preserve hazard values, status, affected counts and coverage. Drop
        # repeated source activity snapshots and long per-cell index arrays.
        for key in ("attack_influences", "local_hazards"):
            for row in env.get(key, []):
                if "source" in row:
                    row["source"] = _compact_identity(row["source"])
                for detail in ("activity", "checked_from", "example_cells"):
                    if detail in row:
                        row.pop(detail)
                        omitted.append("path_environment." + key + "." + detail)
                if "path_indices" in row:
                    row["path_indices_count"] = len(row.pop("path_indices"))
                    omitted.append("path_environment." + key + ".path_indices")
        data["route"] = route
        data["path_environment"] = env
        data["changed_during_read"] = raw.get("changed_during_read", False)
        if not route:
            data["status"] = "partial"
    else:
        for key in ("facts", "state_observations", "coverage", "scope", "requested_aspects", "not_read", "context"):
            if key in raw: data[key] = copy.deepcopy(raw[key])
        for row in data.get("facts", []):
            # Identity is bound by the candidate/stage. Keep native facets and
            # their coverage; omit repeated object locations and body-part tables.
            row.get("facts", {}).pop("identity", None)
            if "object" in row:
                row["object"] = _compact_identity(row["object"])
            facts = row.get("facts", {})
            health = facts.get("health", {})
            if isinstance(health, Mapping) and "body_parts" in health:
                health.pop("body_parts")
                health["body_parts_not_shown"] = True
                omitted.append("facts.health.body_parts")
            if isinstance(health, Mapping): health.pop("field_semantics", None)
            work = facts.get("work", {})
            if isinstance(work, Mapping):
                for detail_name in ("queued_jobs", "queued_jobs_total", "queued_jobs_complete", "work_types_meaning", "medical_care_context"):
                    if detail_name in work:
                        work.pop(detail_name)
                        omitted.append("facts.work." + detail_name)
            for facet in facts.values():
                if isinstance(facet, Mapping): facet.pop("meaning", None)
        if not data.get("facts") or not data.get("state_observations"):
            data["status"] = "partial"
    if omitted:
        data["not_shown"] = sorted(set(omitted))
    return data


def _sample(read):
    samples = [o.get("sample") for o in read.get("state_observations", [])] + [read.get("sample")]
    return next((s for s in samples if s and s.get("load_epoch")), {})


def _comparisons(candidates, reads, stats):
    # Reuse the shipped path comparator; group by traveller, stage, origin and
    # sample. Only destinations vary inside each group. Partial rows stay visible.
    groups = {}
    stat_groups = {}
    seen = set()
    for candidate in candidates:
        for stage in candidate["stages"]:
            read_id = stage["read"]
            if (stage["kind"], read_id) in seen:
                continue
            seen.add((stage["kind"], read_id))
            read = reads[read_id]
            sample = _sample(read)
            stable = (read.get("status") == "ok" and not read.get("changed_during_read")
                      and bool(sample.get("load_epoch")) and sample.get("started_tick") is not None
                      and sample.get("started_tick") == sample.get("finished_tick"))
            if stage["kind"] in ("move", "approach", "transfer"):
                scope = dict(actor=candidate["actor"], stage=stage["kind"], origin=stage["origin"],
                             load_epoch=sample.get("load_epoch"), tick=sample.get("started_tick"))
                entry = dict(read=read_id, result={"from": candidate["actor"], "route": read.get("route", {}) if stable else {}})
                groups.setdefault(_key(scope), (scope, []))[1].append(entry)
            elif stage["kind"] == "actor_state":
                values = [v for f in read.get("facts", []) for v in f.get("facts", {}).get("stats", {}).get("values", [])]
                for name in stats:
                    matching = [v for v in values if v.get("definition") == name]
                    raw = matching[0] if len(matching) == 1 else {}
                    value = raw.get("value")
                    valid = stable and raw.get("status") == "observed" and type(value) in (float, int) and math.isfinite(value)
                    scope = dict(stat=name, load_epoch=sample.get("load_epoch"), tick=sample.get("started_tick"))
                    stat_groups.setdefault(_key(scope), (scope, []))[1].append(dict(actor=candidate["actor"], read=read_id, value=value if valid else None))
    output = []
    for scope, rows in groups.values():
        for metric in ROUTE_METRICS:
            ranking = rank_paths(rows, metric)
            output.append(dict(scope=scope, metric=metric, entries=[dict(read=r["read"], rank=r.get("rank")) for r in ranking["results"]]))
    for scope, rows in stat_groups.values():
        rows.sort(key=lambda row: (row["value"] is None, -(row["value"] or 0)))
        last, rank = None, None
        for i, row in enumerate(rows):
            if row["value"] is not None and (rank is None or row["value"] != last):
                last, rank = row["value"], i + 1
            row["rank"] = rank if row["value"] is not None else None
        output.append(dict(scope=scope, direction="descending", entries=rows))
    return output


async def evaluate_activity(arguments, source: ActivityDataSource):
    activity = await source.resolve(normalize_activity(arguments))
    selective = enabled()
    actors, destinations = activity["actors"], activity["destination"]["candidates"]
    if not destinations or len(actors) * len(destinations) > MAX_PAIRS:
        _invalid(f"Select 1..{MAX_PAIRS} actor/destination pairs.")
    reads, requests, candidates = {}, {}, []
    for actor in actors:
        for destination in destinations:
            stages = []
            def add(kind, operation, args, origin=None, target=None):
                if selective and operation == 'inspect_route':
                    args = dict(args, margin=activity['margin'])
                    if activity.get('user'):
                        args['user'] = activity['user']
                key = _key([operation, args])
                if key not in requests:
                    requests[key] = (f"r{len(requests)+1}", operation, args)
                stages.append(dict(kind=kind, actor=actor, subject=activity["subject"], origin=origin, destination=target, read=requests[key][0]))
            aspects = activity.get('actor_aspects', []) if selective else ['health', 'skills', 'work']
            if aspects or activity['stats']:
                add("actor_state", "look", dict(object=actor, aspects=aspects or ['identity'], detail=0,
                                              **({"stats": activity["stats"]} if activity["stats"] else {})))
            if activity["kind"] == "transfer":
                subject = activity["subject"]
                if not selective or 'approach' in activity['include']:
                    add("approach", "inspect_route", dict(actor=actor, to=subject, detail="summary"), actor, subject)
                add("transfer", "inspect_route", dict(actor=actor, start=subject, to=destination, detail="summary"), subject, destination)
            elif activity['kind'] == 'move':
                start = activity["start"]
                add("move", "inspect_route", dict(actor=actor, to=destination, detail="summary", **({"start": start} if start else {})), start or actor, destination)
            user = activity.get('user') or (activity["subject"] if activity["kind"] == "transfer" else actor)
            if activity['kind'] == 'state' or not selective or 'destination_state' in activity['include']:
                add("destination_state", "look", dict(object=destination, aspects=["state"], actor=actor, detail=0,
                                                     **({"user": user} if user and user["kind"] == "pawn" else {})), target=destination)
            candidates.append(dict(actor=actor, subject=activity["subject"], destination=destination, stages=stages))
    result = dict(kind=activity["kind"], source_kind=source.source_kind, candidates=candidates, reads=reads,
                  comparisons=[], status="partial", limitations=["Current, sequential observations; not an atomic snapshot.",
                  "Transfer start is hypothetical; carry effects, action eligibility and future hazards remain unknown.",
                  "Ranks compare observed paths or the same StatDef, not ETA, safety or job success."])
    epoch, output_full = None, False
    for read_id, operation, args in requests.values():
        if output_full:
            reads[read_id] = dict(status="not_computed", reason="activity_output_limit")
            continue
        try:
            value = await source.request(operation, copy.deepcopy(args))
        except ToolExecutionError as error:
            if not error.recoverable or error.code == "world_changed":
                raise
            value = dict(status="partial", error=dict(code=error.code, message=str(error), details=error.details))
        if value.get("status") == "world_changed":
            raise ToolExecutionError("world_changed", "Discard activity observations after a world change.", recoverable=False)
        if selective and operation == 'inspect_route':
            from .context_pruning import route_view
            raw = value.get('data') if isinstance(value.get('data'), Mapping) else value
            read = route_view({**raw, 'status': value.get('status', raw.get('status', 'partial'))},
                environment='environment' in activity['include'], objects='objects' in activity['include'])
        else:
            read = _project(value, operation)
        current = _sample(read).get("load_epoch")
        if current and epoch and current != epoch:
            raise ToolExecutionError("world_changed", "Activity observations span loaded worlds.", recoverable=False)
        epoch = current or epoch
        reads[read_id] = read
        # Measure the composed result after adding this projected read. Preserve
        # every completed read and mark only the next work as not computed.
        sized = {'tool': 'evaluate_activity', 'data': result} if selective else result
        if (enabled() and len(_key(result)) > MAX_SOURCE_CHARS) or model_size(sized, arguments=arguments) > MAX_TOOL_RESULT_CHARS - 8_000:
            reads[read_id] = dict(status="partial", error=dict(code="activity_output_limit",
                message="Narrow the actor/destination set; raw observation remains in the consultation ledger."))
            output_full = True
    if not selective:
        result["comparisons"] = _comparisons(candidates, reads, activity["stats"])
    result["status"] = "ok" if all(r.get("status") == "ok" for r in reads.values()) else "partial"
    if selective:
        # Ordinary query results, without an additional candidate/read-reference
        # graph or automatic ranks. Each completed native read appears once.
        emitted = set()
        observations = []
        for candidate in candidates:
            for stage in candidate['stages']:
                if stage['read'] in emitted:
                    continue
                emitted.add(stage['read'])
                observations.append({**{k: copy.deepcopy(v) for k, v in stage.items() if k != 'read' and v is not None},
                                     'observation': reads[stage['read']]})
        limits = [result['limitations'][0]]
        limits.append('Current object predicates/reservations do not establish complete action eligibility or future state.'
                      if activity['kind'] == 'state' else result['limitations'][1])
        return dict(kind=activity['kind'], status=result['status'], observations=observations,
                    requested={'include': activity['include'], 'actor_aspects': activity['actor_aspects'], 'stats': activity['stats']},
                    limitations=limits)
    return result


def make_activity_tool(interaction):
    async def handler(arguments):
        return await evaluate_activity(arguments, SemanticActivityDataSource(interaction))
    parameters = schema()
    description = "Compare move or transfer candidates. Returns actor health/work/stats, separate approach/transfer routes and destination state with actor/user context. Reads are shared; stages reference reads. Native coverage and unknowns remain explicit; no action or success prediction."
    if enabled():
        parameters['properties']['kind']['enum'] = ['move', 'transfer', 'state']
        parameters['properties']['kind']['description'] = 'move/transfer reads paths. state reads only destination state for each actor/user pair; use it for contextual predicates, occupancy or reservations without requesting paths.'
        parameters['properties']['include'] = {'type': 'array', 'uniqueItems': True,
            'items': {'enum': ['approach', 'destination_state', 'environment', 'objects', 'geometry']},
            'description': 'Select additional content in this call: approach adds actor-to-subject paths for transfer; destination_state checks recipient/actor context; environment returns path/corridor hazards, coverage and tolerances (request for safety questions); objects lists corridor objects; geometry includes full path segments. Default paths only.'}
        parameters['properties']['user'] = copy.deepcopy(parameters['properties']['actors']['items'])
        parameters['properties']['user']['description'] = 'Optional recipient Pawn for route environmental tolerances and destination state; does not replace the traveller.'
        parameters['properties']['margin'] = {'type': 'integer', 'minimum': 0, 'maximum': 12, 'default': 2,
            'description': 'Corridor width in cells around the path when environmental context is needed.'}
        parameters['properties']['actor_aspects'] = {'type': 'array', 'uniqueItems': True,
            'items': {'enum': ['health', 'skills', 'work', 'profile', 'gear', 'mood']},
            'description': 'Only requested actor facts are read, once per actor. Default none. stats independently requests exact StatDefs.'}
        parameters['properties']['actor_focus'] = {'type': 'object', 'additionalProperties': False,
            'properties': {key: {'type': 'array', 'minItems': 1, 'maxItems': 32, 'items': {'type': 'string'}}
                           for key in ('skills', 'work', 'capacities')},
            'description': 'Only these exact named skills/work types/capacities; includes their aspects automatically. Other actor_aspects stay as requested.'}
        description = 'Compare all actors/destinations in one call. state: contextual destination predicates/occupancy/reservations only, no paths. move: actor/start-to-destination paths. transfer: subject-to-destination paths using each actor. Names/selectors resolve here; no preliminary ID/count/region query needed. Request environment for safety, approach for a first leg, destination_state alongside paths only if needed, actor_aspects/focus for relevant actor facts. No action, ETA or success prediction.'
    return ToolSpec('evaluate_activity', description, parameters, handler)
