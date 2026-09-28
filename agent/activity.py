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
    if not isinstance(args, Mapping) or set(args) - {"kind", "actors", "subject", "start", "destinations", "stats"}:
        _invalid("Use kind, actors, destinations, optional subject/start and stats.")
    kind = args.get("kind")
    if kind not in ("move", "transfer"):
        _invalid("kind must be move or transfer.")
    if kind == "move" and "subject" in args or kind == "transfer" and "start" in args:
        _invalid("move has no subject; transfer starts at subject and accepts no start.")
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
    return dict(kind=kind, actors=actors, subject=subject, start=start, destination=destination, stats=list(stats))


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

    async def _resolve(self, ref):
        value = await self.interaction.look(dict(object=ref, aspects=["identity"], detail=0))
        facts = value.get("facts", [])
        if value.get("status") != "ok" or len(facts) != 1:
            raise ToolExecutionError("activity_reference_unresolved", "Choose one complete object reference.", details={"resolution": value.get("resolution"), "facts": facts})
        if ref["kind"] == "cell":
            return _ref(facts[0]["facts"]["identity"], ("cell",))
        result = self._identity(facts[0]["object"], ref["kind"])
        if ref.get("id") and ref["id"] != result["id"]:
            raise ToolExecutionError("activity_reference_mismatch", "Read returned a different identity.")
        return result

    async def resolve(self, activity):
        activity = copy.deepcopy(activity)
        activity["actors"] = [await self._resolve(ref) for ref in activity["actors"]]
        for role in ("subject", "start"):
            if activity[role] is not None:
                activity[role] = await self._resolve(activity[role])
        destination = activity["destination"]
        if "candidates" in destination:
            refs = [await self._resolve(ref) for ref in destination["candidates"]]
        else:
            value = await self.interaction.look(dict(object=destination["selector"], aspects=["identity"], detail=0))
            if value.get("status") != "ok" or value.get("coverage", {}).get("listing") != "complete":
                raise ToolExecutionError("activity_destination_partial", "Narrow the selector or choose explicit destinations.")
            refs = [self._identity(row["object"], "thing") for row in value.get("facts", [])]
        activity["destination"] = {"candidates": _refs(refs, MAX_DESTINATIONS)}
        activity["actors"] = _refs(activity["actors"], MAX_ACTORS, ("pawn",))
        roles = [*activity["actors"], *refs, activity["subject"], activity["start"]]
        if len({r["map"] for r in roles if r}) != 1:
            raise ToolExecutionError("activity_map_mismatch", "All activity roles must share a loaded map.")
        return activity

    async def request(self, operation, arguments):
        if operation == "look":
            return await self.interaction.look(arguments)
        return await self.interaction._world_read(arguments, operation)


def _project(value, operation):
    """Preserve native scope/coverage; omit route geometry, never hazard factors."""
    data = copy.deepcopy(value.get("data") if isinstance(value.get("data"), Mapping) else value)
    data["status"] = value.get("status", "partial")
    if operation == "inspect_route":
        if value.get("observation_id"):
            data["observation_id"] = value["observation_id"]
        if value.get("sample"):
            data["sample"] = copy.deepcopy(value["sample"])
        route = data.get("route", {})
        route.pop("segments", None)
        for key in ("objects", "environment", "region", "page"):
            data.pop(key, None)
        data["not_shown"] = ["route.segments", "corridor objects/environment/region"]
        if not route:
            data["status"] = "partial"
    elif not data.get("facts") or not data.get("state_observations"):
        data["status"] = "partial"
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
    actors, destinations = activity["actors"], activity["destination"]["candidates"]
    if not destinations or len(actors) * len(destinations) > MAX_PAIRS:
        _invalid(f"Select 1..{MAX_PAIRS} actor/destination pairs.")
    reads, requests, candidates = {}, {}, []
    for actor in actors:
        for destination in destinations:
            stages = []
            def add(kind, operation, args, origin=None, target=None):
                key = _key([operation, args])
                if key not in requests:
                    requests[key] = (f"r{len(requests)+1}", operation, args)
                stages.append(dict(kind=kind, actor=actor, subject=activity["subject"], origin=origin, destination=target, read=requests[key][0]))
            add("actor_state", "look", dict(object=actor, aspects=["health", "skills", "work"], detail=0,
                                          **({"stats": activity["stats"]} if activity["stats"] else {})))
            if activity["kind"] == "transfer":
                subject = activity["subject"]
                add("approach", "inspect_route", dict(actor=actor, to=subject, detail="summary"), actor, subject)
                add("transfer", "inspect_route", dict(actor=actor, start=subject, to=destination, detail="summary"), subject, destination)
            else:
                start = activity["start"]
                add("move", "inspect_route", dict(actor=actor, to=destination, detail="summary", **({"start": start} if start else {})), start or actor, destination)
            user = activity["subject"] if activity["kind"] == "transfer" else actor
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
        read = _project(value, operation)
        current = _sample(read).get("load_epoch")
        if current and epoch and current != epoch:
            raise ToolExecutionError("world_changed", "Activity observations span loaded worlds.", recoverable=False)
        epoch = current or epoch
        reads[read_id] = read
        # Reserve space for rank references and pending-read markers. No candidate
        # is dropped and no partially serialized fact is sent as a complete read.
        if len(_key(result)) > MAX_TOOL_RESULT_CHARS - 24_000:
            reads[read_id] = dict(status="partial", error=dict(code="activity_output_limit",
                message="Read smaller actor/destination sets; the raw observation is retained in the consultation."))
            output_full = True
    result["comparisons"] = _comparisons(candidates, reads, activity["stats"])
    result["status"] = "ok" if all(r.get("status") == "ok" for r in reads.values()) else "partial"
    return result


def make_activity_tool(interaction):
    async def handler(arguments):
        return await evaluate_activity(arguments, SemanticActivityDataSource(interaction))
    return ToolSpec("evaluate_activity", "Compare move or transfer candidates. Returns actor health/work/stats, separate approach/transfer routes and destination state with actor/user context. Reads are shared; stages reference reads. Native coverage and unknowns remain explicit; no action or success prediction.", schema(), handler)
