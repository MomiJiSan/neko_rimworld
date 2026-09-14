"""Build model tools from the Mod-owned schema and template directory."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from typing import Any

from .agent.tools import ToolExecutionError, ToolHandler, ToolSetup, ToolSpec
from .bridge import BridgeServer
from .observation import Observation


def make_game_tools(catalog: dict[str, Any], query: ToolHandler) -> ToolSetup:
    public = catalog.get("public_query")
    if isinstance(public, dict) and public.get("version") in {"public_query.v1", "public_query.v2"}:
        return _public_tools(catalog, public, query)
    description_contract = catalog.get("result_description_contract")
    references_supported = (
        isinstance(description_contract, dict) and description_contract.get("version") == "observed_values.v1"
    )
    resource_contract = catalog.get("resource_contract")
    resources_available = (
        isinstance(resource_contract, dict)
        and resource_contract.get("version") in {"resource_evidence.v1", "resource_evidence.v2", "resource_evidence.v3"}
        and isinstance(catalog.get("resource_parameters"), dict)
    )
    separate_resources = resources_available and resource_contract["version"] == "resource_evidence.v3"
    info_parameters = copy.deepcopy(catalog["template_parameters"])
    resource_description = ""
    resource_context = ""
    if resources_available:
        info_parameters["properties"]["resources"] = copy.deepcopy(catalog["resource_parameters"])
        info_parameters.pop("required", None)
        info_parameters["oneOf"] = [{"required": ["resources"]}, {"required": ["requests"]}]
        resource_description = (
            "For colony resources, stocks, inventory or quantities, use resources:{} for a bounded "
            "supplies overview on the current map, or resources:{def_names:[...]} for exact definitions. "
            "The game computes quantities, ID deduplication and scope overlaps. Use this instead of paging "
            "through raw map things or a resource-counter dictionary. Choose exactly one mode: resources OR requests. "
            "Use observed def_names; if an exact definition is unknown and needed, discover it with the existing "
            "requests target thing_defs with the selectors in its schema. Put selectors inside target and sections beside target. "
            "Native classification selectors, when available, establish category membership; name substring search alone does not. "
            "Definition names are not category names. "
        )
        lower_bound_field = (
            "known_units" if resource_contract["version"] == "resource_evidence.v2" else "known_quantities"
        )
        page_field = "row_page" if resource_contract["version"] == "resource_evidence.v2" else "view"
        resource_context = (
            "\n\nResource evidence contract:\n"
            + json.dumps(resource_contract, ensure_ascii=False)
            + "\nUse get_game_info(resources={}) first for a resource overview; no preliminary colony query is necessary. "
            "Default supplies excludes rock/corpse noise but includes stored-resource definitions, weapons and apparel. "
            "Explicit def_names bypass only focus, retaining selected map/scopes and ground visibility. "
            "Use exact queries only to drill into needed details. Do not repeat arithmetic already computed by the game. "
            f"Respect coverage and {page_field} completeness. A missing cached entry is not a zero; a partial physical quantity "
            f"is unknown, with {lower_bound_field} as a lower bound. Report each quantity with its observed scope. "
            "An item scan does not count facilities; differing scope totals do not diagnose a cache failure. "
            "Specific zero-valued definitions do not establish a zero for a broader category."
        )

    resource_tools = []
    if separate_resources:
        info_parameters = copy.deepcopy(catalog["template_parameters"])
        resource_description = ""
        resource_context = (
            "\n\nResource evidence contract:\n"
            + json.dumps(resource_contract, ensure_ascii=False)
            + "\nread_resources reads quantities across explicitly selected scopes. No purpose default or preliminary query is required. "
            "Use schema-driven definition_where/instance_where or exact observed def_names. All types use the same conditions. "
            "Request include_total:true when a combined physical total answers the question. Cached is separate. "
            "Cite the needed returned paths at submit_response instead of retyping measurements. Additional detail reads require an unresolved fact."
        )

        async def resources(arguments):
            return await query({"operation": "resource_report", "arguments": copy.deepcopy(arguments)})

        resource_tools.append(
            ToolSpec(
                name="read_resources",
                description="Read Mod-computed scoped quantities, physical ID unions and optional whole-request totals. Conditions follow the shared public schema; no business category presets. Exact zero, unknown, lower bound and cached absence differ. Results include descriptions for fact references.",
                parameters=copy.deepcopy(catalog["resource_parameters"]),
                handler=resources,
            )
        )

    async def templates(arguments: dict[str, Any]):
        if "resources" in arguments and not separate_resources:
            if not resources_available:
                raise ToolExecutionError(
                    "resource_report_unavailable",
                    "Upgrade the game Mod for resource evidence; legacy queries remain available.",
                )
            if set(arguments) != {"resources"} or not isinstance(arguments["resources"], dict):
                raise ToolExecutionError("invalid_arguments", "Choose resources object OR legacy requests, not both.")
            return await query({"operation": "resource_report", "arguments": copy.deepcopy(arguments["resources"])})
        return await query({"operation": "template_query", "arguments": arguments})

    async def exact(arguments: dict[str, Any]):
        return await query({"operation": "graphql", "arguments": arguments})

    descriptions: dict[tuple, dict[str, Any]] = {}

    async def describe(arguments: dict[str, Any]):
        names = arguments.get("types")
        include = arguments.get("include_templates", False)
        if (
            set(arguments) - {"types", "include_templates"}
            or not isinstance(names, list)
            or not 1 <= len(names) <= 8
            or any(not isinstance(name, str) or not name or len(name) > 128 for name in names)
            or type(include) is not bool
        ):
            raise ToolExecutionError(
                "invalid_arguments", "Select 1..8 type names and an optional include_templates boolean."
            )
        key = (tuple(sorted(set(names))), include)
        # Cached metadata still checks connection/schema through the scoped query on first read.
        # The per-run cache is not game state and is never converted into a live Observation.
        if key in descriptions:
            return copy.deepcopy(descriptions[key])
        observation = await query({"operation": "describe_schema", "arguments": arguments})
        if not isinstance(observation, Observation):
            raise ToolExecutionError("invalid_schema", "Expected a schema description response.")
        payload = observation.value["response"]["payload"]
        if payload.get("status") != "ok":
            raise ToolExecutionError(
                str(payload.get("error_code") or "schema_unavailable"),
                str(payload.get("message") or "Schema description failed."),
            )
        data = payload.get("data")
        if (
            not isinstance(data, dict)
            or data.get("schema_id") != catalog["schema_id"]
            or data.get("metadata_only") is not True
        ):
            raise ToolExecutionError("schema_changed", "Description does not match this run's schema.")
        descriptions[key] = copy.deepcopy(data)
        return copy.deepcopy(data)

    return ToolSetup(
        tools=tuple(resource_tools)
        + (
            ToolSpec(
                name="get_game_info",
                description=resource_description
                + (
                    "Select an object, then one or more of its available information sections. "
                    "The supplied schema describes each target and the fields its section templates return. "
                    "Combine sections for the same object to inspect related facts together, or include multiple targets. "
                    "Repeated targets merge into one nested object; execution.bindings links each request to its result. "
                    "All requests share one sample and total budget. Native members and holder/storage relations retain their meanings; haulable groups are not pawn-specific permission. "
                    "Use query_game for fields or nested pages not covered by section templates."
                ),
                parameters=info_parameters,
                handler=templates,
            ),
            ToolSpec(
                name="query_game",
                description="Read precisely selected game fields using the supplied GraphQL schema. Multiple fields and roots are allowed in one query.",
                parameters=copy.deepcopy(catalog["query_parameters"]),
                handler=exact,
            ),
            ToolSpec(
                name="describe_game_schema",
                description="Read supported native types, original member semantics, arguments, component projections and optional section templates. Metadata only; not a live game observation. Use for exact fields not described by the common templates.",
                parameters={
                    "type": "object",
                    "properties": {
                        "types": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1, "maxLength": 128},
                            "minItems": 1,
                            "maxItems": 8,
                        },
                        "include_templates": {"type": "boolean"},
                    },
                    "required": ["types"],
                    "additionalProperties": False,
                },
                handler=describe,
            ),
        ),
        references_supported=references_supported,
        context=(
            "Read-only game query directory (schema_id="
            + catalog["schema_id"]
            + "):\n"
            + json.dumps({"types": catalog["type_index"], "roots": catalog["query_roots"]}, ensure_ascii=False)
            + "\n"
            + catalog["query_rules"]
            + "\nChoose the target object first, then combine the sections that answer the question. "
            "Use returned object IDs for follow-up queries; names can match multiple objects. "
            "Use describe_game_schema for exact member types and original semantics; the full SDL is not injected by default. "
            "Prefer section templates when they cover the needed facts; otherwise query exact fields. "
            "Do not read the same facts again merely to follow a tool order. "
            "Keep related evidence joined by object identity, not display name. "
            "Inspect collection coverage and issue paths before interpreting missing data; an unread page is not an empty collection. "
            "Follow the catalog's paging rules and retry incomplete objects with fewer fields."
            + resource_context
            + (
                "\nFact-reference answer parts are supported for game observations."
                if references_supported
                else "\nThis Mod does not advertise result descriptions. Submit text answer parts; automatic fact references are unavailable for this connection."
            )
            + (
                "\nShared selection rules:\n" + json.dumps(catalog["selection_contract"], ensure_ascii=False)
                if "selection_contract" in catalog
                else ""
            )
        ),
    )


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
            raise ToolExecutionError(payload.get("error_code") or "schema_unavailable", payload.get("message") or "Metadata unavailable.")
        data = payload.get("data", {})
        if data.get("metadata_only") is not True or data.get("schema_id") != catalog["schema_id"]:
            raise ToolExecutionError("schema_changed", "Query directory changed; start a fresh consultation.")
        return copy.deepcopy(data)

    directory = {key: public[key] for key in ("roots", "rules", "common_fields", "field_index", "field_notes", "value_domains", "examples") if key in public}
    context_blocks = (
        ("catalog", "Game query directory (" + catalog["schema_id"] + "):\n" + json.dumps(directory, ensure_ascii=False, separators=(",", ":"))),
        ("reminder_completion", "\nAnswer once the requested facts are known; read more only for an unresolved fact."),
        ("reminder_domain", " Native capability flags and category trees are not interchangeable. Never use CountAsResource/readout membership as a default filter for all possessions."),
        ("reminder_scope", " Report the actual requested scope and preserve unknown versus zero."),
    )
    return ToolSetup(
        tools=(
            ToolSpec("query", "Read selected game objects, related facts and Mod-computed aggregates. Same fields and conditions for detail and counts. Batch related requests. No preliminary query required. Use coverage before interpreting missing values.", copy.deepcopy(public["parameters"]), read),
            ToolSpec("describe_query", "Read canonical fields, native semantics, arguments and supported operations when needed information is missing from the directory. An indexed field name alone does not establish its meaning or allowed values. Metadata only; skip this call when the needed syntax and semantics are already known.", {
                "type": "object", "properties": {"types": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8}},
                "required": ["types"], "additionalProperties": False,
            }, describe),
        ),
        context="".join(text for _,text in context_blocks),
        # The compact query contract does not advertise v1.3 path-composition descriptors.
        # Use the already supported text submission protocol; do not invent references.
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
        for key in ("schema_id", "schema", "query_rules"):
            if not isinstance(catalog.get(key), str) or not catalog[key].strip():
                raise ToolExecutionError("invalid_catalog", "Catalog is missing " + key + ".")
        for key in ("template_parameters", "query_parameters"):
            if not isinstance(catalog.get(key), dict) or catalog[key].get("type") != "object":
                raise ToolExecutionError("invalid_catalog", "Catalog has an invalid tool parameter schema.")
        if not isinstance(catalog.get("type_index"), list) or not isinstance(catalog.get("query_roots"), list):
            raise ToolExecutionError("invalid_catalog", "Update the RimWorld Mod: native schema directory is missing.")
        if len(json.dumps(catalog, ensure_ascii=False)) > 256_000:
            raise ToolExecutionError("invalid_catalog", "Catalog exceeds the schema context limit.")
