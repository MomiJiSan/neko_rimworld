"""Agent-facing activity surfaces and their native observation adapter."""
from __future__ import annotations

import copy
import os
from typing import Any, Mapping

from .activity_contracts import DEFAULT_INSPECTS, MAX_ACTORS, MAX_DESTINATIONS, METRICS, PREFERENCES
from .activity_executor import evaluate_activity
from .tools import ToolExecutionError, ToolSpec


def activity_variant() -> str | None:
    value = os.environ.get("NEKO_ACTIVITY_VARIANT", "").strip().lower()
    return value if value in {"conservative", "simplified", "hybrid"} else None


def _ref_schema(kind: str) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    props = {"kind": {"const": kind}, "map": text}
    schema = {"type": "object", "additionalProperties": False,
              "required": ["kind"], "properties": props}
    if kind == "cell":
        props.update({k: {"type": "integer", "minimum": -(2**31), "maximum": 2**31-1} for k in ("x", "z")})
        schema["required"] += ["x", "z"]
    else:
        props.update(id=text, name=text)
        schema["oneOf"] = [
            {"required": ["id"], "not": {"required": ["name"]}},
            {"required": ["name"], "not": {"required": ["id"]}},
        ]
    return schema


def _array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum,
            "maxItems": maximum, "uniqueItems": True}


def _schema(simple: bool) -> dict[str, Any]:
    ref = {"oneOf": [_ref_schema(k) for k in ("pawn", "thing", "cell")]}
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    selector = {"type": "object", "additionalProperties": False, "required": ["kind"],
                "properties": {"kind": {"const": "things"}, "map": text, "definition": text,
                               "capabilities": _array(text, 16, 1)},
                "anyOf": [{"required": ["definition"]}, {"required": ["capabilities"]}]}
    destinations = {"oneOf": [
        _array(ref, MAX_DESTINATIONS, 1),
        {"type": "object", "additionalProperties": False, "required": ["candidates"],
         "properties": {"candidates": _array(ref, MAX_DESTINATIONS, 1)}},
        {"type": "object", "additionalProperties": False, "required": ["selector"],
         "properties": {"selector": selector}},
    ]}
    props = {"kind": {"type": "string", "enum": ["move", "transfer"]},
             "subject": {"oneOf": [_ref_schema(k) for k in ("pawn", "thing")]},
             "start": ref, "stats": _array(text, 8)}
    actors = _array(_ref_schema("pawn"), MAX_ACTORS, 1)
    if simple:
        props.update(actors=actors, destinations=destinations,
                     preferences=_array({"type": "string", "enum": list(PREFERENCES)}, 3))
    else:
        props.update(actor={"type": "object", "additionalProperties": False, "required": ["candidates"],
                            "properties": {"candidates": actors}}, destination=destinations,
                     inspect=_array({"type": "string", "enum": sorted(set(sum(DEFAULT_INSPECTS.values(), ())))}, 4, 1),
                     compare=_array({"type": "string", "enum": list(METRICS)}, 4),
                     detail={"type": "string", "enum": ["summary", "details"], "default": "summary"})
    rules = []
    for kind, inspections in DEFAULT_INSPECTS.items():
        then = ({"required": ["subject"], "not": {"required": ["start"]}} if kind == "transfer"
                else {"not": {"required": ["subject"]}})
        if not simple:
            then["properties"] = {"inspect": {"items": {"enum": list(inspections)}}}
        rules.append({"if": {"properties": {"kind": {"const": kind}}, "required": ["kind"]}, "then": then})
    if simple:
        rules.append({"if": {"required": ["preferences"], "properties": {"preferences": {"contains": {"const": "higher_observed_stat"}}}},
                      "then": {"required": ["stats"], "properties": {"stats": {"minItems": 1}}}})
    else:
        rules += [
            {"if": {"required": ["stats", "inspect"], "properties": {"stats": {"minItems": 1}}},
             "then": {"properties": {"inspect": {"contains": {"const": "actor_state"}}}}},
            {"if": {"required": ["inspect"], "properties": {"inspect": {"not": {"contains": {"enum": ["move_route", "approach_route", "transfer_route"]}}}}},
             "then": {"required": ["compare"], "properties": {"compare": {"maxItems": 0}}}},
        ]
    return {"type": "object", "additionalProperties": False,
            "required": ["kind", "actors", "destinations"] if simple else ["kind", "actor", "destination"],
            "properties": props, "allOf": rules}


def _conservative_schema() -> dict[str, Any]:
    return _schema(False)


def _simplified_schema() -> dict[str, Any]:
    return _schema(True)


class SemanticActivityDataSource:
    source_kind = "native"

    def __init__(self, interaction: Any) -> None:
        self.interaction = interaction

    async def _resolve_ref(self, ref: Mapping[str, Any]) -> dict[str, Any]:
        if ref.get("kind") == "cell":
            cell = copy.deepcopy(dict(ref))
            cell.setdefault("map", "current")
            return cell
        # Resolve IDs too: an ID alone does not bind a loaded map or prove existence.
        result = await self.interaction.look({"object": dict(ref), "aspects": ["identity"], "detail": 0})
        if result.get("status") != "ok" or result.get("coverage", {}).get("listing") != "complete":
            raise ToolExecutionError("activity_reference_partial", "Activity reference resolution is incomplete.")
        facts = result.get("facts", [])
        if len(facts) != 1 or not isinstance(facts[0].get("object"), Mapping):
            raise ToolExecutionError("activity_reference_ambiguous", "Activity reference must resolve to exactly one object.")
        obj = facts[0]["object"]
        map_id = obj.get("map") or obj.get("map_id")
        if not obj.get("id") or not map_id:
            raise ToolExecutionError("activity_reference_unresolved", "Activity reference requires a stable ID and loaded map.")
        if ref.get("id") and ref["id"] != obj["id"]:
            raise ToolExecutionError("activity_reference_mismatch", "The observed identity differs from the requested ID.")
        return {"kind": ref["kind"], "id": obj["id"], "map": map_id}

    async def resolve(self, activity: Mapping[str, Any]) -> Mapping[str, Any]:
        resolved = copy.deepcopy(activity)
        resolved["actors"] = [await self._resolve_ref(ref) for ref in activity["actors"]]
        for role in ("subject", "start"):
            if activity.get(role) is not None:
                resolved[role] = await self._resolve_ref(activity[role])
        destination = activity["destination"]
        if "candidates" in destination:
            refs = [await self._resolve_ref(ref) for ref in destination["candidates"]]
        else:
            result = await self.interaction.look({"object": copy.deepcopy(destination["selector"]), "aspects": ["identity"], "detail": 0})
            if result.get("status") != "ok" or result.get("coverage", {}).get("listing") != "complete":
                raise ToolExecutionError("activity_destination_partial", "Destination discovery is incomplete; choose explicit candidates.")
            rows = result.get("facts", [])
            if not rows:
                raise ToolExecutionError("activity_no_destinations", "The selector returned no destinations.")
            if len(rows) > MAX_DESTINATIONS:
                raise ToolExecutionError("activity_combination_limit", "Too many destinations; choose explicit candidates.",
                                         details={"limit": MAX_DESTINATIONS, "matched": len(rows)})
            refs = []
            for row in rows:
                obj = row.get("object", {})
                map_id = obj.get("map") or obj.get("map_id")
                if not obj.get("id") or not map_id:
                    raise ToolExecutionError("activity_reference_unresolved", "Destination has no stable ID or loaded map.")
                refs.append({"kind": "thing", "id": obj["id"], "map": map_id})
        resolved["destination"] = {"candidates": refs}
        refs_all = [*resolved["actors"], *refs, resolved.get("subject"), resolved.get("start")]
        maps = {ref["map"] for ref in refs_all if ref and ref.get("map") not in (None, "current")}
        if len(maps) > 1:
            raise ToolExecutionError("activity_map_mismatch", "All activity roles must be on one resolved map.")
        if len(maps) == 1:
            for ref in refs_all:
                if ref and ref.get("kind") == "cell" and ref.get("map") == "current":
                    ref["map"] = next(iter(maps))
        resolved["resolution"] = {"status": "complete", "stable_ids": True}
        return resolved

    async def request(self, operation: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation == "look":
            return await self.interaction.look(dict(arguments))
        if operation == "inspect_route":
            return await self.interaction._world_read(dict(arguments), "inspect_route")
        raise ToolExecutionError("activity_unsupported_operation", f"Activity operation {operation} is unavailable.")


def make_activity_tool(interaction: Any, variant: str) -> ToolSpec:
    schema = _simplified_schema() if variant in {"simplified", "hybrid"} else _conservative_schema()
    description = (
        "Evaluate one bounded move or transfer activity. The evaluator expands stages, preserves actor/subject/origin/destination "
        "and returns observed routes, object conditions, coverage and unknowns. It never performs a game action or claims a best/safe Job."
    )

    async def handler(arguments: dict[str, Any]) -> Mapping[str, Any]:
        try:
            return await evaluate_activity(arguments, SemanticActivityDataSource(interaction), variant=variant)
        except ToolExecutionError:
            raise
        except Exception as error:
            raise ToolExecutionError(getattr(error, "code", "activity_invalid"), str(error),
                                     details=getattr(error, "details", {})) from error

    return ToolSpec("evaluate_activity", description, schema, handler, max_result_chars=131_072)


__all__ = ["activity_variant", "make_activity_tool", "SemanticActivityDataSource"]
