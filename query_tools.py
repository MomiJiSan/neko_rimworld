"""Build query backends from the supported Mod-owned public directory."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from typing import Any

from .agent.tools import WORLD_OBSERVATION_VERSIONS, ToolExecutionError, ToolHandler, ToolSetup, ToolSpec
from .bridge import BridgeServer
from .observation import Observation


def make_game_tools(catalog: dict[str, Any], query: ToolHandler) -> ToolSetup:
    """Build only the public-query backend required by look/count."""
    public = catalog.get("public_query")
    if not isinstance(public, dict) or public.get("version") not in {"public_query.v1", "public_query.v2"}:
        raise ToolExecutionError("semantic_surface_unavailable", "A supported public query directory is required; update the RimWorld Mod.", recoverable=False)
    return _public_tools(catalog, public, query)


def _public_tools(catalog: dict[str, Any], public: dict[str, Any], query: ToolHandler) -> ToolSetup:
    """Current Mod owns field names, query semantics and discovery. Legacy tools are not advertised."""
    if not isinstance(public.get("parameters"), dict) or public["parameters"].get("type") != "object":
        raise ToolExecutionError("invalid_catalog", "Invalid public query parameters.")

    async def read(arguments):
        return await query({"operation": "query", "arguments": copy.deepcopy(arguments)})

    async def describe(arguments):
        result = await query({"operation": "describe_query", "arguments": copy.deepcopy(arguments)})
        if not isinstance(result, Observation):
            raise ToolExecutionError("invalid_schema", "Expected schema metadata.")
        payload = result.value["response"]["payload"]
        if payload.get("status") != "ok":
            raise ToolExecutionError(
                payload.get("error_code") or "schema_unavailable", payload.get("message") or "Metadata unavailable."
            )
        data = payload.get("data", {})
        if data.get("metadata_only") is not True or data.get("schema_id") != catalog["schema_id"]:
            raise ToolExecutionError("schema_changed", "Query directory changed; start a fresh consultation.")
        return copy.deepcopy(data)

    directory = {
        key: public[key]
        for key in ("roots", "rules", "common_fields", "field_index", "field_notes", "value_domains", "examples")
        if key in public
    }
    spatial_tools = ()
    capability = catalog.get("spatial_query")
    if (
        isinstance(capability, dict)
        and capability.get("version") == "spatial_query.v1"
        and isinstance(capability.get("parameters"), dict)
    ):
        directory["spatial_query"] = copy.deepcopy(capability)

        async def spatial(arguments):
            return await query({"operation": "spatial_query", "arguments": copy.deepcopy(arguments)})

        spatial_tools = (
            ToolSpec(
                "spatial_query",
                capability.get("description", "Native reachability"),
                copy.deepcopy(capability["parameters"]),
                spatial,
            ),
        )
    world_tools = []
    world = catalog.get("world_observation")
    if isinstance(world, dict) and world.get("version") in WORLD_OBSERVATION_VERSIONS:
        allowed = {"survey_space", "inspect_route", "inspect_state"}
        specs = world.get("tools", [])
        if (
            not isinstance(specs, list)
            or len(specs) != 3
            or {s.get("name") for s in specs if isinstance(s, dict)} != allowed
        ):
            raise ToolExecutionError("invalid_catalog", "Invalid world observation tool set")
        for spec in specs:
            name = spec["name"]
            if spec.get("operation") != name or not isinstance(spec.get("parameters"), dict):
                raise ToolExecutionError("invalid_catalog", "Invalid world observation operation")

            async def observe(arguments, operation=name):
                return await query({"operation": operation, "arguments": copy.deepcopy(arguments)})

            world_tools.append(ToolSpec(name, spec["description"], copy.deepcopy(spec["parameters"]), observe))
        directory["world_observation"] = copy.deepcopy(world)
    behavior = catalog.get("native_behavior")
    if isinstance(behavior, dict) and behavior.get("version") == "native_behavior.v1":
        specs = behavior.get("tools")
        names = {"inspect_actions", "inspect_threats"}
        if not isinstance(specs, list) or len(specs) != 2 or {s.get("name") for s in specs if isinstance(s, dict)} != names:
            raise ToolExecutionError("invalid_catalog", "Invalid native behavior tool set")
        for spec in specs:
            if spec.get("operation") != spec["name"] or not isinstance(spec.get("parameters"), dict) or not isinstance(spec.get("description"), str):
                raise ToolExecutionError("invalid_catalog", "Invalid native behavior operation")

            async def inspect(arguments, operation=spec["name"]):
                return await query({"operation": operation, "arguments": copy.deepcopy(arguments)})

            world_tools.append(ToolSpec(spec["name"], spec["description"], copy.deepcopy(spec["parameters"]), inspect))
        directory["native_behavior"] = copy.deepcopy(behavior)
    if isinstance(catalog.get("game_implementation"), dict):
        directory["game_implementation"] = copy.deepcopy(catalog["game_implementation"])
    context_blocks = (
        (
            "catalog",
            "Game query directory ("
            + catalog["schema_id"]
            + "):\n"
            + json.dumps(directory, ensure_ascii=False, separators=(",", ":")),
        ),
        ("reminder_completion", "\nAnswer once the requested facts are known; read more only for an unresolved fact."),
        (
            "reminder_domain",
            " Native capability flags and category trees are not interchangeable. Never use CountAsResource/readout membership as a default filter for all possessions.",
        ),
        ("reminder_scope", " Report the actual requested scope and preserve unknown versus zero."),
    )
    return ToolSetup(
        tools=(
            ToolSpec(
                "query",
                "Read selected game objects, related facts and Mod-computed aggregates. Same fields and conditions for detail and counts. Batch related requests. No preliminary query required. Use coverage before interpreting missing values.",
                copy.deepcopy(public["parameters"]),
                read,
            ),
            ToolSpec(
                "describe_query",
                "Read canonical fields, native semantics, arguments and supported operations when needed information is missing from the directory. An indexed field name alone does not establish its meaning or allowed values. Metadata only; skip this call when the needed syntax and semantics are already known.",
                {
                    "type": "object",
                    "properties": {
                        "types": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8}
                    },
                    "required": ["types"],
                    "additionalProperties": False,
                },
                describe,
            ),
        )
        + spatial_tools
        + tuple(world_tools),
        context="".join(text for _, text in context_blocks),
        # Live delivery is text-only; stored historical reference parts still resolve.
        references_supported=False,
        directory=copy.deepcopy(directory),
        context_blocks=context_blocks,
    )


class CatalogTools:
    """Connection-scoped, single-flight cache of Mod-authored descriptions; not agent memory."""

    def __init__(self, bridge: Callable[[], BridgeServer | None], query: ToolHandler):
        self._bridge = bridge
        self._query = query
        self._lock = asyncio.Lock()
        self._key: tuple[BridgeServer, str] | None = None
        self._catalog: dict[str, Any] | None = None

    async def load(self) -> ToolSetup:
        async with self._lock:
            bridge = self._bridge()
            epoch = bridge.connection_key if bridge is not None else None
            if bridge is None or epoch is None:
                self._key = None
                self._catalog = None
                return ToolSetup(
                    (), "Game query tools are unavailable: no authenticated game connection. Do not invent live state."
                )
            key = (bridge, epoch)
            if key != self._key:
                self._catalog = None
                self._key = None
                try:
                    observation = await self._query({"operation": "query_catalog", "arguments": {}})
                    if not isinstance(observation, Observation):
                        raise ToolExecutionError("invalid_catalog", "The game did not return a query catalog.")
                    response = observation.value["response"]
                    if (
                        self._bridge() is not bridge
                        or bridge.connection_key != epoch
                        or response["connection_epoch"] != epoch
                    ):
                        raise ToolExecutionError(
                            "connection_changed", "The game connection changed while loading its schema."
                        )
                    payload = response["payload"]
                    if payload["status"] != "ok":
                        raise ToolExecutionError(
                            "catalog_unavailable", str(payload.get("message") or "Query catalog is unavailable.")
                        )
                    catalog = payload["data"]
                    self._validate(catalog)
                    self._catalog = copy.deepcopy(catalog)
                    self._key = key
                except ToolExecutionError as error:
                    return ToolSetup(
                        (), "Game query tools are unavailable: " + str(error) + " Do not invent live state."
                    )
            assert self._catalog is not None

            # Bind tools to this connection. A reconnect requires a new run/schema, not silent reuse.
            async def scoped_query(arguments: dict[str, Any]):
                if self._bridge() is not bridge or bridge.connection_key != epoch:
                    raise ToolExecutionError(
                        "connection_changed",
                        "Game connection changed; start a fresh consultation to reload the schema.",
                    )
                result = await self._query(arguments)
                if isinstance(result, Observation) and result.value["response"]["connection_epoch"] != epoch:
                    raise ToolExecutionError("connection_changed", "The response belongs to a new connection.")
                return result

            return make_game_tools(self._catalog, scoped_query)

    @staticmethod
    def _validate(catalog: object) -> None:
        if not isinstance(catalog, dict):
            raise ToolExecutionError("invalid_catalog", "Catalog must be an object.")
        if not isinstance(catalog.get("schema_id"), str) or not catalog["schema_id"].strip():
            raise ToolExecutionError("invalid_catalog", "Catalog is missing schema_id.")
        if len(json.dumps(catalog, ensure_ascii=False)) > 256_000:
            raise ToolExecutionError("invalid_catalog", "Catalog exceeds the schema context limit.")
