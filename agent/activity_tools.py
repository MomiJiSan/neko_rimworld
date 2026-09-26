"""Agent-facing activity evaluation surfaces."""

from __future__ import annotations

import copy
import os
from typing import Any, Mapping

from .activity_executor import evaluate_activity
from .tools import ToolExecutionError, ToolSpec


def activity_variant() -> str | None:
    value = os.environ.get("NEKO_ACTIVITY_VARIANT", "").strip().lower()
    return value if value in {"conservative", "simplified", "hybrid"} else None


def _ref_schema(kind: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind"],
        "properties": {
            "kind": {"const": kind},
            "id": {"type": "string", "minLength": 1, "maxLength": 256},
            "name": {"type": "string", "minLength": 1, "maxLength": 256},
            "map": {"type": "string", "minLength": 1, "maxLength": 256},
            "x": {"type": "integer"},
            "z": {"type": "integer"},
        },
    }


def _conservative_schema() -> dict[str, Any]:
    ref = {"oneOf": [_ref_schema("pawn"), _ref_schema("thing"), _ref_schema("cell")]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "actor", "destination"],
        "properties": {
            "kind": {"type": "string", "enum": ["move", "transfer"]},
            "actor": {"type": "object", "additionalProperties": False, "required": ["candidates"], "properties": {"candidates": {"type": "array", "minItems": 1, "maxItems": 8, "items": _ref_schema("pawn")}}},
            "subject": ref,
            "destination": {"oneOf": [{"type": "array", "minItems": 1, "maxItems": 8, "items": {"oneOf": [_ref_schema("thing"), _ref_schema("cell")]}}, {"type": "object", "additionalProperties": False, "required": ["candidates"], "properties": {"candidates": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"oneOf": [_ref_schema("thing"), _ref_schema("cell")]} }}}, {"type": "object", "additionalProperties": False, "required": ["selector"], "properties": {"selector": {"type": "object"}}}]},
            "inspect": {"type": "array", "items": {"type": "string", "enum": ["actor_state", "approach_route", "transfer_route", "destination_state"]}, "minItems": 1, "maxItems": 4},
            "compare": {"type": "array", "items": {"type": "string", "enum": ["steps", "path_length", "native_cost", "path_hazards"]}, "maxItems": 4},
            "detail": {"type": "string", "enum": ["summary", "details"], "default": "summary"},
        },
    }


def _simplified_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "actors", "destinations"],
        "properties": {
            "kind": {"type": "string", "enum": ["move", "transfer"]},
            "actors": {"type": "array", "minItems": 1, "maxItems": 8, "items": _ref_schema("pawn")},
            "subject": {"oneOf": [_ref_schema("pawn"), _ref_schema("thing")]},
            "destinations": {"oneOf": [{"type": "array", "minItems": 1, "maxItems": 8, "items": {"oneOf": [_ref_schema("thing"), _ref_schema("cell")]}}, {"type": "object", "additionalProperties": False, "required": ["selector"], "properties": {"selector": {"type": "object"}}}]},
            "preferences": {"type": "array", "items": {"type": "string", "enum": ["shorter_route", "lower_observed_hazard", "higher_observed_stat"]}, "maxItems": 3},
        },
    }


class SemanticActivityDataSource:
    source_kind = "native"

    def __init__(self, interaction: Any) -> None:
        self.interaction = interaction

    async def resolve(self, activity: Mapping[str, Any]) -> Mapping[str, Any]:
        destination = activity["destination"]
        if "candidates" in destination:
            return copy.deepcopy(activity)
        selector = copy.deepcopy(destination["selector"])
        result = await self.interaction.look({"object": selector, "aspects": ["identity"], "detail": 0})
        if result.get("status") != "ok":
            raise ToolExecutionError("activity_destination_partial", "Destination discovery was partial; choose explicit candidates.")
        rows = result.get("facts", []) if isinstance(result, Mapping) else []
        refs = []
        for row in rows:
            obj = row.get("object") if isinstance(row, Mapping) else None
            if isinstance(obj, Mapping) and obj.get("id"):
                refs.append({"kind": "thing", "id": obj["id"], "map": obj.get("map") or obj.get("map_id")})
        if not refs:
            raise ToolExecutionError("activity_no_destinations", "The destination selector returned no resolved objects.")
        resolved = copy.deepcopy(activity)
        resolved["destination"] = {"candidates": refs[:8]}
        return resolved

    async def request(self, operation: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation == "look":
            return await self.interaction.look(dict(arguments))
        if operation == "inspect_route":
            return await self.interaction._world_read(dict(arguments), "inspect_route")
        raise ToolExecutionError("activity_unsupported_operation", f"Activity operation {operation} is unavailable.")


def make_activity_tool(interaction: Any, variant: str) -> ToolSpec:
    schema = _simplified_schema() if variant == "simplified" else _conservative_schema()
    description = (
        "Evaluate one bounded move or transfer activity. The evaluator expands stages, preserves actor/subject/origin/destination "
        "and returns observed routes, object conditions, coverage and unknowns. It never performs a game action or claims a best/safe Job."
    )

    async def handler(arguments: dict[str, Any]) -> Mapping[str, Any]:
        try:
            result = await evaluate_activity(arguments, SemanticActivityDataSource(interaction), variant=variant)
        except ToolExecutionError:
            raise
        except Exception as error:
            code = getattr(error, "code", "activity_invalid")
            details = getattr(error, "details", {})
            raise ToolExecutionError(code, str(error), details=details) from error
        return result

    return ToolSpec("evaluate_activity", description, schema, handler, max_result_chars=131_072)


__all__ = ["activity_variant", "make_activity_tool", "SemanticActivityDataSource"]
