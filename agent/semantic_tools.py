"""Production interaction that exposes look/count instead of the raw query tool.

Why this exists: the Mod-owned directory the query tool needs is 48,927 characters
of field catalogue, sent on every request so the model can write canonical field
paths. Measured in the v1.10 study, that catalogue is 84% of the first-request
prompt. This adapter replaces the tool vocabulary with a closed enum, so the
catalogue is not needed at all: the same 12 pairs held quality level while using
two thirds fewer tokens.

It is a replacement, not an extra: the model does not see `query`, and the
directory is dropped from the prompt. A closed enum cannot express every possible
request, so `semantic_surface_available()` is the single place that decides
whether to engage, and the runtime keeps the Mod's own interface when it declines.

Results are rendered as plain text with their scope, coverage, not_read and
native semantics. Nothing is interned or replaced by a code word, and unknown is
never lowered to zero.

The contract is the one `agent.loop` already calls on `session.interaction`:
prompt, tools, attach_result, request_messages, accept_text, snapshot, aclose,
plus `finalizing` and `supports_finalization`.
"""

from __future__ import annotations

import copy

import os
from typing import Any

from .handoff import attach_agent_handoff, make_submission_tool
from .prompt import build_finalization_prompt, build_system_prompt
from .query_surface import Surface, SurfaceError
from .semantic_surface import (
    TOOL_GUIDE,
    apply_lookup,
    interpret_lookup,
    lookup_spec,
    normalize_object,
    pack,
    plan_count,
    plan_look,
    render,
    render_blocked,
    tool_schemas,
    spatial_tool_schema,
    validate_aspects,
    validate_by,
    validate_detail,
    validate_metric,
)
from .tools import ToolExecutionError, ToolSpec
from .wiki_tools import WIKI_GUIDANCE
from .world_order import order_world_fields

# Only this explicitly approved non-observation tool survives the adapter.
PASSTHROUGH_TOOLS = frozenset({"wiki"})

ENVIRONMENT_SWITCH = "NEKO_SEMANTIC_SURFACE"


def _with_field_semantics(result):
    """Annotate observed measures in place within the projection, never raw evidence."""
    result = copy.deepcopy(result)
    for item in result.get("facts", []):
        facts = item.get("facts", {})
        summary = facts.get("health", {}).get("summary")
        if isinstance(summary, dict) and summary.get("state") != "unavailable":
            meanings = {
                "health_percent": {"unit": "fraction", "meaning": "Overall game health summary, not blood volume or proof of no injuries."},
                "bleed_rate": {"unit": "blood_loss_fraction_per_game_day", "ticks_per_game_day": 60000,
                               "meaning": "Multiply by 100 for percentage points of blood loss per game day; not per second or an ETA."},
                "pain": {"unit": "fraction", "meaning": "Game pain total, separate from injury severity and blood loss."},
            }
            summary["field_semantics"] = {key: value for key, value in meanings.items() if key in summary}
        traits = facts.get("profile", {}).get("traits")
        if isinstance(traits, dict) and traits.get("state") != "unavailable":
            traits["field_semantics"] = {"degree": "Trait variant code, not a numeric speed or stat bonus.",
                                         "description": "Game-generated trait tooltip, including effects when available. Respect suppressed; a trait effect is not the pawn's final stat value."}
    return result


def semantic_surface_requested() -> bool:
    """Whether this deployment wants the semantic surface.

    On by default: the paired study is what promoted it. Setting the environment
    variable to a false value is the supported way back to the Mod's own query
    interface without a code change.
    """
    override = os.environ.get(ENVIRONMENT_SWITCH)
    if override is None:
        return True
    return override.strip().lower() not in {"0", "false", "no", "off"}


def semantic_surface_available(setup: Any) -> bool:
    """Whether this particular connection can be served by the semantic surface.

    All three conditions are structural, not preferences:

    * the Mod must advertise the public query directory, because the closed enum
      compiles onto it;
    * a `query` tool must be present, because that handler is the only native read
      this adapter is allowed to perform;
    * the directory must build the Surface **including its argument schema**, which
      is what the constructor does. Checking only the directory would let a
      directory that lacks the per-root argument branches pass here and then raise
      while the run was already under way.
    """
    if not semantic_surface_requested():
        return False
    directory = getattr(setup, "directory", None)
    if directory is None:
        return False
    tools = {getattr(tool, "name", None): tool for tool in setup.tools}
    if "query" not in tools:
        return False
    try:
        surface = Surface(directory)
        surface.add_query_schema(tools["query"].parameters)
    except (KeyError, TypeError, ValueError):
        return False
    return True


class SemanticInteraction:
    """Expose look/count and render their native results as plain text."""

    supports_finalization = True

    #: Reference answer parts cite data-relative paths into the native payload, and
    #: this interface never shows the model a path: results arrive rendered, with
    #: their scope, coverage and not_read already stated. A model asked for paths
    #: here could only invent them, so the capability is off regardless of what the
    #: connection advertises. The paired study that promoted this surface pinned
    #: references off for the same reason and refused to run the combination.
    references_supported = False

    def __init__(self, setup: Any, session: Any, update: Any) -> None:
        self.setup = setup
        self.session = session
        self.update = update
        self.original = {tool.name: tool for tool in setup.tools}
        self.finalizing = False
        self.surface = Surface(setup.directory)
        self.surface.add_query_schema(self.original["query"].parameters)
        self.sample_identity: tuple[Any, Any] | None = None
        directory = setup.directory if isinstance(getattr(setup, "directory", None), dict) else {}
        self.spatial_supported = (directory.get("spatial_query", {}).get("version") == "spatial_query.v1" and "spatial_query" in self.original)

    # -- runtime contract -------------------------------------------------

    def prompt(self, preferred_cycles: int) -> str:
        """The authored prompt plus the interface guide, and nothing else.

        `setup.context` is deliberately not appended: that is the field catalogue
        this interface exists to remove. The guide states the closed vocabulary
        the model may use instead.
        """
        build = build_finalization_prompt if self.finalizing else build_system_prompt
        prompt = build(preferred_cycles, references_supported=self.references_supported) + "\n\n" + TOOL_GUIDE
        if not self.finalizing and "wiki" in self.original:
            prompt += "\n\n" + WIKI_GUIDANCE
        if not self.finalizing and self.setup.directory.get("world_observation", {}).get("version") == "world_observation.v2.2":
            prompt += "\nUse survey_space for a neighborhood/region, inspect_route for a path AND its environment, and inspect_state for object state/occupancy/reservations. Resolve names directly; no preliminary look is required. user is the intended user/recipient, actor performs interaction/travel. All checks are current and read-only. Distinguish target position from stopping cell, building type from settings, and existing reservations from physical occupancy. Safety conclusions require returned environmental coverage; native Danger is not comprehensive safety. Unsupported facets and hidden cells are unknown.\n"
        return prompt

    def submission(self) -> Any:
        return make_submission_tool(
            self.session.submission,
            self.session.observations,
            self.update,
            references_supported=self.references_supported,
        )

    def tools(self) -> tuple[Any, ...]:
        submit = self.submission()
        if self.finalizing:
            return (submit,)
        handlers = {"look": self.look, "count": self.count}
        specs = []
        semantic_specs = tool_schemas(self.setup.directory)
        if self.spatial_supported:
            semantic_specs.append(spatial_tool_schema(self.setup.directory))
            handlers["inspect_space"] = self.inspect_space
        world = self.setup.directory.get("world_observation", {})
        if world.get("version") == "world_observation.v2.2":
            for spec in world.get("tools", []):
                name = spec["name"]
                if name not in self.original:
                    continue
                async def observe(args, operation=name):
                    native = await self.execute_native(args, tool_name=operation)
                    return order_world_fields(native["data"])
                handlers[name] = observe
                semantic_specs.append(spec)
        for tool in semantic_specs:
            handler = handlers[tool["name"]]

            async def guarded(args, handler=handler):
                try:
                    return await handler(args)
                except (SurfaceError, KeyError, TypeError) as error:
                    raise ToolExecutionError("semantic_request_invalid", str(error)) from error

            specs.append(ToolSpec(tool["name"], tool["description"], tool["parameters"], guarded))
        passthrough = tuple(self.original[name] for name in sorted(PASSTHROUGH_TOOLS) if name in self.original)
        return (*specs, *passthrough, submit)

    def request_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return messages

    def accept_text(self, text: str, finish_reason: Any) -> None:
        return None

    def attach_result(self, result: Any) -> Any:
        return attach_agent_handoff(result)

    def snapshot(self) -> dict[str, Any]:
        return dict(
            native_reads=len(self.session.observations),
            finalizing=self.finalizing,
            spatial_supported=self.spatial_supported,
            # Recorded so the audit shows that a connection advertising references
            # had them suppressed here rather than silently ignored.
            references_suppressed=bool(getattr(self.setup, "references_supported", False)),
        )

    async def aclose(self) -> None:
        return None

    # -- native read ------------------------------------------------------

    async def execute_native(self, request: dict[str, Any], *, tool_name: str = "query") -> dict[str, Any]:
        """Perform the only native read this adapter makes, and record it once.

        A loaded-world change is fatal rather than merged: facts from two loads
        are not the same colony state, so the run must restart instead of
        answering with a mixture.
        """
        observation = await self.original[tool_name].handler(request)
        value = observation.to_dict()
        response = value["response"]
        payload = response["payload"]
        identity = (response["colony_id"], response["load_epoch"])
        if self.sample_identity is not None and identity != self.sample_identity:
            raise ToolExecutionError(
                "world_changed",
                "The loaded world changed; start a new consultation. Do not combine the old facts.",
                recoverable=False,
            )
        self.sample_identity = identity
        self.session.observations.append(value)
        if payload["status"] not in {"ok", "partial"}:
            raise ToolExecutionError(
                payload.get("error_code") or "query_failed",
                payload.get("message") or "Native read failed",
                details={"issues": payload.get("issues", [])},
            )
        return dict(
            observation_id=value["observation_id"],
            status=payload["status"],
            data=pack(payload.get("data"), payload.get("issues", [])),
            issues=payload.get("issues", []),
        )

    async def _resolve(self, obj: dict[str, Any]):
        """Resolve a name or definition, or return candidates instead of a pick.

        An ambiguous or incomplete lookup must never silently choose one object:
        the caller gets the candidates and the scope that produced them.
        """
        spec = lookup_spec(self.surface, obj)
        if spec is None:
            return obj, None, None
        native = await self.execute_native(spec["request"])
        resolution = interpret_lookup(spec, native["data"])
        return apply_lookup(obj, spec, resolution), resolution, native

    # -- tools ------------------------------------------------------------

    async def look(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict) or "object" not in args or "aspects" not in args:
            raise SurfaceError("look requires object and aspects")
        original = normalize_object(args["object"])
        aspects = validate_aspects(original["kind"], args["aspects"])
        detail = validate_detail(args.get("detail", 0))
        obj, resolution, lookup_native = await self._resolve(original)
        if obj is None:
            return render_blocked("look", original, resolution, lookup_native)
        plan = plan_look(self.surface, obj, aspects, detail)
        native = await self.execute_native(plan.request)
        return _with_field_semantics(render(plan, native, resolution))

    async def count(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict) or "object" not in args or "metric" not in args:
            raise SurfaceError("count requires object and metric")
        original = normalize_object(args["object"])
        metric = validate_metric(original["kind"], args["metric"])
        by = validate_by(original["kind"], args.get("by"))
        detail = validate_detail(args.get("detail", 0))
        obj, resolution, lookup_native = await self._resolve(original)
        if obj is None:
            return render_blocked("count", original, resolution, lookup_native)
        plan = plan_count(self.surface, obj, metric, by, detail)
        native = await self.execute_native(plan.request)
        return render(plan, native, resolution)

    async def inspect_space(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict) or not isinstance(args.get("from"), dict):
            raise SurfaceError("inspect_space requires from and to references")
        if isinstance(args.get("to"), list):
            return await self._inspect_many(args)
        if "order_by" in args:
            raise SurfaceError("order_by requires a target list; wrap to in an array")
        if not isinstance(args.get("to"), dict):
            raise SurfaceError("to must be a reference or a bounded target list")
        detail = args.get("detail", "summary")
        if detail not in ("summary", "route"):
            raise SurfaceError('detail must be summary or route')
        capability = self.setup.directory.get("spatial_query", {})
        if "detail" in args and "detail" not in capability.get("parameters", {}).get("properties", {}):
            raise SurfaceError("This connection does not support detail; omit it for reachability only")
        if detail == "route" and capability.get("route_summary") != "native_path_summary.v1":
            raise SurfaceError("This connection does not support path summaries")
        actor = args["from"]
        target = args["to"]
        self._validate_space_ref(actor, "from", {"pawn"})
        self._validate_space_ref(target, "to", {"thing", "cell", "pawn"})
        request = copy.deepcopy(args)
        if target["kind"] == "pawn":
            request["to"]["kind"] = "thing"
        native = await self.execute_native(request, tool_name="spatial_query")
        data = native.get("data")
        if not isinstance(data, dict):
            raise ToolExecutionError("spatial_invalid_result", "The game returned no spatial result.")
        resolved_target = data.get("target")
        if target["kind"] == "pawn" and isinstance(resolved_target, dict) and resolved_target.get("kind") != "pawn":
            raise ToolExecutionError("spatial_target_kind_mismatch", "The resolved target is not a Pawn; use a Pawn ID or a more specific name.")
        # Keep the result claim-shaped. Do not make the model reconstruct reachability
        # from transport/session metadata or unrelated Room/Region fields.
        rendered = {
            "answer": data.get("answer", "unknown"),
            "from": data.get("from"),
            "to": data.get("target"),
            "checked": data.get("checked", {}),
            "explanation": data.get("explanation"),
            "sample": data.get("sample"),
            "candidates": data.get("candidates"),
            "candidates_truncated": data.get("candidates_truncated", False),
            "stage": data.get("stage"),
            "request": data.get("request"),
            "lookup_started_tick": data.get("lookup_started_tick"),
            "lookup_finished_tick": data.get("lookup_finished_tick"),
            "lookup_consistency": data.get("lookup_consistency"),
            "policy": data.get("policy"),
            "meaning": data.get("meaning"),
            "issues": native.get("issues", []),
        }
        if "route" in data:
            route = copy.deepcopy(data["route"])
            # Keep the wire alias for older consumers, but expose one unambiguous
            # quantity to the model. Never turn a route's length into endpoint distance.
            if "path_length" not in route and "grid_distance" in route:
                route["path_length"] = route["grid_distance"]
            route.pop("grid_distance", None)
            if isinstance(route.get("path_length"), dict):
                route["path_length"]["meaning"] = "Sum of segment lengths along this returned path; not straight-line distance between its endpoints."
            route["optimality"] = {'status':'not_evaluated', 'meaning':'Neither shortest nor non-shortest has been established. No shortest-path proof was performed.'}
            route["endpoint_meaning"] = "end is the path stopping cell, not the target's position. Touch can stop beside the target. Use to.position for the actual target location when available; do not infer it when absent."
            rendered["route"] = route
        return rendered

    async def _inspect_many(self, args: dict[str, Any]) -> dict[str, Any]:
        from .spatial_ranking import rank_paths
        targets = args["to"]
        if not 1 <= len(targets) <= 8 or not all(isinstance(t, dict) for t in targets):
            raise SurfaceError("to requires 1..8 target references")
        capability = self.setup.directory.get("spatial_query", {})
        if capability.get("route_summary") != "native_path_summary.v1":
            raise SurfaceError("This connection does not support route comparisons")
        by = args.get("order_by")
        if by is not None and by not in ("path_length", "steps", "native_cost"):
            raise SurfaceError("order_by must be path_length, steps or native_cost")
        detail = args.get("detail", "route" if by else "summary")
        if detail not in ("summary", "route") or by and detail != "route":
            raise SurfaceError("Ordering needs routes; omit detail or use route")
        self._validate_space_ref(args["from"], "from", {"pawn"})
        for i, target in enumerate(targets):
            self._validate_space_ref(target, f"to[{i}]", {"thing", "cell", "pawn"})
        rows = []
        for i, target in enumerate(targets):
            try:
                value = await self.inspect_space({"from":args["from"], "to":target, "detail":detail})
            except ToolExecutionError as error:
                if not error.recoverable: raise
                value = {"answer":"unknown", "error":{"code":error.code,"message":str(error)}}
            rows.append({"input_index":i,"requested_target":copy.deepcopy(target),"result":value})
        return rank_paths(rows, by)

    @staticmethod
    def _validate_space_ref(value: dict[str, Any], path: str, kinds: set[str]) -> None:
        unknown = set(value).difference({"kind", "id", "name", "map", "x", "z"})
        if unknown:
            raise SurfaceError(path + " contains unsupported fields: " + ", ".join(sorted(unknown)))
        for field in ("kind", "id", "name", "map"):
            if field in value and (not isinstance(value[field], str) or not value[field].strip() or len(value[field]) > 256):
                raise SurfaceError(path + "." + field + " must be nonempty text (at most 256 characters)")
        kind = value.get("kind")
        if kind not in kinds:
            raise SurfaceError(path + ".kind must be one of: " + ", ".join(sorted(kinds)))
        if kind == "cell":
            if "id" in value or "name" in value or type(value.get("x")) is not int or type(value.get("z")) is not int:
                raise SurfaceError(path + " cell references require integer x/z and no id/name")
            if any(not -(2**31) <= value[a] < 2**31 for a in ("x", "z")):
                raise SurfaceError(path + " cell coordinates must be 32-bit integers")
            return
        if "x" in value or "z" in value or ("id" in value) == ("name" in value):
            raise SurfaceError(path + " requires exactly one id or name and no coordinates")


def semantic_interaction_factory(setup: Any, session: Any, update: Any):
    """Runtime factory: build the semantic surface, or decline.

    Declining (returning None) is a supported outcome, not an error: the runtime
    then keeps the Mod's own tools, prompt and directory. It happens when the
    deployment switched the surface off, when the connected Mod does not
    advertise a directory, or when the directory cannot compile.
    """
    if not semantic_surface_available(setup):
        return None
    return SemanticInteraction(setup, session, update)


__all__ = [
    "ENVIRONMENT_SWITCH",
    "SemanticInteraction",
    "semantic_interaction_factory",
    "semantic_surface_available",
    "semantic_surface_requested",
]
