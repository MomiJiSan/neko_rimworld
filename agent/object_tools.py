"""One object-facing reader; native state and legacy facts are internal adapters."""

from __future__ import annotations

import copy
import re

from .model_context import MAX_SOURCE_CHARS, canonical, model_size
from .query_surface import SurfaceError
from .semantic_surface import (
    ASPECTS,
    KINDS,
    PAWN_KINDS,
    THING_KINDS,
    normalize_object,
    supported_aspects,
    validate_aspects,
    validate_detail,
)
from .tool_contracts import MAX_TOOL_RESULT_CHARS, WORLD_OBSERVATION_VERSIONS
from .world_order import order_world_fields

PHYSICAL = (*PAWN_KINDS, *THING_KINDS)


def observed_work(fact):
    values = fact.get("facts", {})
    if isinstance(values.get("work"), dict):
        return values["work"]
    state = values.get("state", {})
    if not isinstance(state, dict):
        return {}
    return state.get("work", state.get("facets", {}).get("work", {}))


def summarize_work_assignments(facts, coverage):
    """Count existing assignment rows, never infer runnable jobs or missing zeros."""
    actors=set();unknown=set();by_type={};complete=True;activities={};precedence=[]
    for fact in facts:
        ident=fact.get("object",{}).get("id")
        if not isinstance(ident,str):complete=False;continue
        if ident in actors:complete=False
        actors.add(ident)
        work=observed_work(fact)
        table=work.get("work_types",[]) if isinstance(work,dict) else []
        if not isinstance(table,list):table=[]
        job=work.get("current_job") if isinstance(work,dict) else None
        activities[ident]=job.get("definition") if isinstance(job,dict) else None
        assigned=[row for row in table if isinstance(row,dict) and type(row.get("priority")) is int and row["priority"]>0]
        order_known=all(type(row.get("natural_priority")) in (int,float) for row in assigned)
        precedence.append({"actor_id":ident,"current_job":activities[ident],
            "normal_assigned_type_order":[{"definition":row.get("definition"),"priority":row["priority"],"natural_priority":row["natural_priority"]}
                for row in sorted(assigned,key=lambda row:(row["priority"],-row["natural_priority"],row.get("definition","")))] if order_known else None,
            "order_known":order_known})
        if not isinstance(work,dict) or work.get("status")!="observed" or work.get("work_types_complete") is not True:
            complete=False;unknown.add(ident)
        for row in table:
            if not isinstance(row,dict):complete=False;unknown.add(ident);continue
            definition=row.get("definition")
            if not isinstance(definition,str):complete=False;unknown.add(ident);continue
            entry=by_type.setdefault(definition,{"assigned":set(),"disabled":set(),"unassigned":set()})
            priority=row.get("priority")
            if type(priority) is int and priority>0:entry["assigned"].add(ident)
            elif priority==0 and type(priority) is int:entry["unassigned"].add(ident)
            elif type(priority) is not int:complete=False;unknown.add(ident)
            if row.get("disabled") is True:entry["disabled"].add(ident)
    exact=bool(actors) and complete and coverage.get("listing")=="complete" and coverage.get("matched")==len(actors)
    return {
        "scope":"Objects actually read for this look selection, before display-size cropping; not automatically the whole colony.",
        "actor_ids":sorted(actors),"unknown_or_incomplete_work_actor_ids":sorted(unknown),
        "complete_for_selection":exact,
        "actor_readings":precedence,
        "types":[{"definition":name,"assigned_count":len(value["assigned"]) if exact else None,
            "assigned_lower_bound":len(value["assigned"]),"assigned_actor_ids":sorted(value["assigned"]),
            "unassigned_count":len(value["unassigned"]) if exact else None,"unassigned_actor_ids":sorted(value["unassigned"]),
            "wandering_assigned_actor_ids":sorted(a for a in value["assigned"] if activities.get(a)=="GotoWander"),
            "disabled_actor_ids":sorted(value["disabled"])} for name,value in sorted(by_type.items())],
        "meaning":"Derived from original native work tables retained in facts. Assigned means observed priority > 0; wandering is the observed GotoWander job, not proof of action eligibility. Normal assigned-type order uses lower assigned priority first, then higher natural_priority, not array position; actual available Jobs and emergency work still matter. Null counts mean incomplete coverage. Assignment does not explain why a pawn is idle."
    }

class WorkReadings:
    """An observation-backed work table across pages of one unchanged sample."""

    def __init__(self):
        self.key = None
        self.facts = {}
        self.positions = set()
        self.sources = []

    def summarize(self, reading):
        sample = reading.get("sample", {})
        tick = sample.get("game_tick")
        state_samples = reading.get("state_observations", [])
        stable = bool(state_samples) and type(tick) is int and all(
            source.get("sample", {}).get("load_epoch") == sample.get("load_epoch")
            and source.get("sample", {}).get("started_tick") == tick
            and source.get("sample", {}).get("finished_tick") == tick
            and not source.get("changed_during_read")
            for source in state_samples
        )
        key = canonical({"scope": reading.get("scope", {}), "load_epoch": sample.get("load_epoch"), "tick": tick}) if stable and sample.get("load_epoch") else None
        if key is None:
            self.__init__()
            return summarize_work_assignments(reading["facts"], reading["coverage"])
        if key != self.key:
            self.__init__()
            self.key = key
        for fact in reading["facts"]:
            ident = fact.get("object", {}).get("id")
            if isinstance(ident, str):
                # Keep work only; earlier native observations retain its provenance.
                self.facts[ident] = {"object": {"id": ident}, "facts": {"work": copy.deepcopy(observed_work(fact))}}
        page = reading.get("page", {})
        offset = page.get("offset", 0)
        if type(offset) is int and offset >= 0:
            self.positions.update(range(offset, offset + len(reading["facts"])))
        source = {"selection_sample": copy.deepcopy(sample), "page": copy.deepcopy(page),
                  "state_observation_ids": [s.get("observation_id") for s in state_samples]}
        if source not in self.sources:
            self.sources.append(source)
        coverage = copy.deepcopy(reading["coverage"])
        matched = coverage.get("matched")
        covered = type(matched) is int and len(self.facts) == matched and self.positions == set(range(matched))
        coverage["listing"] = "complete" if covered else "partial"
        result = summarize_work_assignments(list(self.facts.values()), coverage)
        result["scope"] = "Union of work tables read in this consultation for the same selection, load epoch and unchanged game tick; not an atomic snapshot."
        result["source_pages"] = copy.deepcopy(self.sources)
        result["sample"] = {"load_epoch": sample["load_epoch"], "game_tick": tick, "atomic": False}
        return result


OBJECT_GUIDE = (
    "Use look for object facts and count for population quantities/grouping. "
    "look accepts the same object selectors; capabilities on things matches any listed native capability, "
    "intersected with other selectors. Identity discovery does not scan environment. "
    "look defaults to current state for physical objects/cells; request selected aspects for small reads, "
    "and stats for exact game-computed attributes. An unknown StatDef may return native discovery matches; these are names, not computed values or automatic replacements. "
    "actor interacts/travels, user receives/uses. Use health.capacities for Pawn capacities, not guessed StatDef names. "
    "Resolve names directly; prefer id/ids for stable IDs. An exact ID already observed in this run can be normalized from name. No preliminary ID lookup is required. "
    "include_held/include_fogged retain their inclusive polarity and scope defaults. "
    "object.location.position is the canonical own-position cell; held_position is separate. A null location is not proof that the object is absent; retain its applicability and coverage. "
    "Use wiki for definition names/types when available; definition knowledge is not a current actor value. "
    "survey_space reads region and environment; inspect_route reads path and environment; "
    "inspect_space remains the lightweight reachability/path-ranking tool. "
    "Read per-observation scope, coverage and unknowns. Neither occupancy nor reservation "
    "is a complete action check; native Danger is not overall safety."
)
STATE_ASPECTS = ("state", "health", "skills", "gear", "work")
STATE_FACETS = ("condition", "reservations", "containment", "bed", "door", "production", "plant", "turret", "trap", "storage", "construction", "power", "fuel", "battery", "temperature_control", "ammunition", "breakdown", "switch", "rot", "manning", "explosive", "environment")
GEAR_FIELDS = ("carried", "equipment", "apparel", "inventory")


def observed_id_reference(reference, observations):
    """Recover a known physical ID without guessing from its spelling or reading again."""
    if not isinstance(reference, dict) or "name" not in reference or set(reference) & {"id", "ids", "names"}:
        return reference
    term, kind = reference["name"], reference.get("kind")
    if kind not in ("pawn", "thing") or not isinstance(term, str):
        return reference
    matches, conflicting_names = set(), set()

    def visit(value, population=None):
        if isinstance(value, dict):
            root = value.get("from")
            if root in ("pawn", "pawns", "spawned_free_colonists"):
                population = "pawn"
            elif root in ("thing", "things"):
                population = "thing"
            observed_kind = value.get("kind")
            if observed_kind not in ("pawn", "thing"):
                observed_kind = "pawn" if value.get("runtime_type") == "Verse.Pawn" else population
            held_map = value.get("map_held")
            map_id = (
                value.get("map_id") or value.get("map") or (held_map.get("id") if isinstance(held_map, dict) else None)
            )
            requested_map = reference.get("map")
            in_scope = isinstance(map_id, str) and (requested_map in (None, "current", map_id))
            ident = value.get("id")
            if observed_kind == kind and isinstance(ident, str) and in_scope:
                if ident == term:
                    matches.add((ident, map_id))
                if term in (value.get("name"), value.get("label")) and ident != term:
                    conflicting_names.add(ident)
            for key, child in value.items():
                if key not in ("request", "requested_target"):
                    visit(child, population)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, population)

    for observation in observations:
        response = observation.get("response", {}) if isinstance(observation, dict) else {}
        payload = response.get("payload", {})
        if payload.get("status") in ("ok", "partial"):
            visit(payload.get("data"))
    if len(matches) != 1 or conflicting_names:
        return reference
    result = copy.deepcopy(reference)
    result["id"] = result.pop("name")
    return result


def _belongs(key, fields):
    return any(key == field or key.startswith(field + "_") for field in fields)


def state_contract(directory):
    world = directory.get("world_observation", {})
    if world.get("version") not in WORLD_OBSERVATION_VERSIONS:
        return None
    for tool in world.get("tools", []):
        if tool.get("name") == "inspect_state":
            properties = tool.get("parameters", {}).get("properties", {})
            required = {
                "targets",
                "stats",
                "actor",
                "user",
                "sections",
                "include_fogged",
                "detail_limit",
                "exact_lookup",
            }
            sections = properties.get("sections", {}).get("items", {}).get("enum", [])
            if required <= properties.keys() and {"identity", *STATE_ASPECTS} <= set(sections):
                return tool
    return None


def extend_look_schema(spec, contract):
    """Keep one selector/aspects interface, exposing only negotiated capabilities."""
    spec = copy.deepcopy(spec)
    spec["description"] = (
        "Find objects and read their requested facts in one call. Physical objects support state "
        "(all current native facets), plus selected health/skills/gear/work for pawns. "
        "Work includes assignments and current jobs. Identity discovery does not inspect environment. "
        "Other aspects retain biography, mood, resources and definition facts. "
        "Use stats for exact game-computed StatDefs. actor performs interaction/travel; user is "
        "the intended recipient; omitted actor defaults to user. Context checks are observations, not action eligibility guarantees. "
        "Resolve names directly; prefer id/ids for stable IDs. Already observed IDs can be normalized from name. Do not look up IDs first. "
        "object.location gives canonical position and held_position cells without removing legacy fields. "
        "Unknown StatDefs may provide discovery.matches and matching_definition_kind; names are not computed values. "
        "Scope, coverage and unavailable facts are explicit."
    )
    props = spec["parameters"]["properties"]
    cell = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "x", "z"],
        "properties": {
            "kind": {"const": "cell"},
            "map": {"type": "string", "default": "current"},
            "x": {"type": "integer", "minimum": -2147483648, "maximum": 2147483647},
            "z": {"type": "integer", "minimum": -2147483648, "maximum": 2147483647},
        },
    }
    props["offset"] = {
        "type": "integer",
        "minimum": 0,
        "maximum": 8192,
        "default": 0,
        "description": "Continue object rows using returned page.next_offset. Each page is a fresh observation, not a frozen snapshot.",
    }
    props["object"] = {"oneOf": [props["object"], cell]}
    props["aspects"]["items"]["enum"] = list(ASPECTS) + ["state"]
    props["detail"]["description"] = (
        "Bounds long Pawn/work detail lists (conditions, body parts, gear, queued jobs); 0 uses reader defaults. Skills, capacities and work-type assignments remain complete when observed, including mod-added definitions. Other facets report their own bounds. Does not change selection or requested stats."
    )
    props["aspects"]["description"] = (
        "Omitted: current state for physical objects/cells, identity otherwise. Select only needed aspects for smaller reads. state already contains native health, skills, gear, work and device facets; mood/biography are separate."
    )
    for key in ("stats", "actor", "user"):
        props[key] = copy.deepcopy(contract["parameters"]["properties"][key])
        if key in ("actor", "user"):
            props[key] = {
                "type": "object",
                "additionalProperties": False,
                "description": "Pawn context: provide exactly one of id or name, not both. Optional map defaults to current.",
                "properties": {
                    **{name: copy.deepcopy(props[key]["properties"][name]) for name in ("id", "name", "map")},
                    "kind": {"type": "string", "const": "pawn"},
                },
                "oneOf": [
                    {"required": ["id"], "not": {"required": ["name"]}},
                    {"required": ["name"], "not": {"required": ["id"]}},
                ],
            }
    # Replace legacy per-kind constraints as well as the top-level vocabulary.
    spec["parameters"]["allOf"] = [
        {
            "if": {
                "properties": {"object": {"properties": {"kind": {"const": kind}}, "required": ["kind"]}},
                "required": ["object"],
            },
            "then": {
                "properties": {
                    "aspects": {
                        "items": {"enum": list(supported_aspects(kind)) + (["state"] if kind in PHYSICAL else [])}
                    }
                }
            },
        }
        for kind in KINDS
    ] + [
        {
            "if": {"properties": {"object": {"properties": {"kind": {"const": "cell"}}, "required": ["kind"]}}},
            "then": {
                "properties": {"aspects": {"items": {"enum": ["identity", "state"]}}, "offset": {"const": 0}},
                "not": {"required": ["stats"]},
            },
        },
        {
            "not": {
                "properties": {
                    "aspects": {
                        "allOf": [{"contains": {"enum": list(STATE_ASPECTS)}}, {"contains": {"const": "distribution"}}]
                    }
                },
                "required": ["aspects"],
            }
        },
    ]
    spec["parameters"]["allOf"].extend(
        [
            {
                "if": {
                    "properties": {
                        "object": {"properties": {"kind": {"not": {"enum": [*PHYSICAL, "cell"]}}}, "required": ["kind"]}
                    }
                },
                "then": {"not": {"anyOf": [{"required": [key]} for key in ("stats", "actor", "user")]}},
            },
            {
                "if": {"anyOf": [{"required": ["actor"]}, {"required": ["user"]}]},
                "then": {"properties": {"aspects": {"contains": {"enum": list(STATE_ASPECTS)}}}},
            },
        ]
    )
    spec["parameters"]["required"] = ["object"]
    return spec


class ObjectReader:
    def __init__(self, interaction):
        self.interaction = interaction
        self.work_readings = WorkReadings()

    async def look(self, args):
        if not isinstance(args, dict) or not isinstance(args.get("object"), dict):
            raise SurfaceError("look requires object")
        unknown = set(args) - {
            "object",
            "aspects",
            "detail",
            "stats",
            "actor",
            "user",
            "offset",
            *({"include", "focus"}),
        }
        if unknown:
            raise SurfaceError("look does not accept: " + ", ".join(sorted(unknown)))
        include = args.get("include", [])
        if not isinstance(include, list) or any(v not in ("body_parts",) for v in include):
            raise SurfaceError("look include accepts body_parts")
        # Model-only inclusion controls never enter the native request.
        focus = args.get("focus", {})
        if (
            not isinstance(focus, dict)
            or set(focus) - {"skills", "work", "capacities", "state"}
            or any(
                not isinstance(v, list)
                or not 1 <= len(v) <= 32
                or any(not isinstance(n, str) or not n or len(n) > 256 for n in v)
                for v in focus.values()
            )
        ):
            raise SurfaceError("focus selects skills/work/capacities or physical state facets")
        if "state" in focus and (len(set(focus["state"])) != len(focus["state"]) or set(focus["state"]) - set(STATE_FACETS)):
            raise SurfaceError("focus.state requires distinct registered physical facet names")
            # Native requests below are built from explicit native keys. Retain
            # these controls here so page fitting uses the requested model view.
        raw_object = args["object"]
        kind = raw_object.get("kind")
        if kind in ("pawn", "thing"):
            raw_object = observed_id_reference(
                raw_object, getattr(getattr(self.interaction, "session", None), "observations", ())
            )
        cell = kind == "cell"
        obj = copy.deepcopy(raw_object) if cell else normalize_object(raw_object)
        if kind in ("pawn", "thing") and len(obj.get("names", [])) == 1:
            reference = {"kind": kind, "name": obj["names"][0]}
            if "map" in obj:
                reference["map"] = obj["map"]
            recovered = observed_id_reference(
                reference, getattr(getattr(self.interaction, "session", None), "observations", ())
            )
            if "id" in recovered:
                obj["ids"] = [recovered["id"]]
                obj.pop("names")
        aspects = args.get("aspects", ["state"] if kind in PHYSICAL or cell else ["identity"])
        if not isinstance(aspects, list) or not aspects or any(not isinstance(a, str) for a in aspects):
            raise SurfaceError("aspects must be a nonempty string array")
        aspects = list(dict.fromkeys(aspects))
        aspects = list(
            dict.fromkeys(
                [
                    *aspects,
                    *(
                        {"skills": "skills", "work": "work", "capacities": "health", "state": "state"}[k]
                        for k in args.get("focus", {})
                    ),
                ]
            )
        )
        detail = validate_detail(args.get("detail", 0))
        offset = args.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 8192 or cell and offset:
            raise SurfaceError("offset must be 0..8192 for object populations; cells use zero")
        state = [a for a in aspects if a in STATE_ASPECTS]
        if state and "distribution" in aspects:
            raise SurfaceError("Use count for quantities/grouping; look for object state")
        legacy = [a for a in aspects if a not in STATE_ASPECTS]
        if kind not in PHYSICAL and not cell:
            if state or any(k in args for k in ("stats", "actor", "user")):
                raise SurfaceError("state/stats/context require physical objects or cells")
            return await self.interaction._look_legacy({**args, "aspects": aspects})
        if cell:
            if set(aspects) - {"identity", "state"} or "stats" in args:
                raise SurfaceError("cells support identity/state, not Pawn/Thing stats")
            if set(obj) - {"kind", "map", "x", "z"} or any(type(obj.get(k)) is not int for k in ("x", "z")):
                raise SurfaceError("cell requires integer x/z and optional map")
        else:
            validate_aspects(kind, [a for a in aspects if a != "state"]) if any(a != "state" for a in aspects) else None
        stats = args.get("stats")
        if "stats" in args and (
            not isinstance(stats, list)
            or not 1 <= len(stats) <= 16
            or any(not isinstance(x, str) or not x for x in stats)
        ):
            raise SurfaceError("stats requires 1..16 exact StatDef names")
        for key in ("actor", "user"):
            if key in args:
                ref = args[key]
                if (
                    not isinstance(ref, dict)
                    or set(ref) - {"id", "name", "kind", "map"}
                    or ("id" in ref) == ("name" in ref)
                    or ref.get("kind", "pawn") != "pawn"
                    or not isinstance(ref.get("id", ref.get("name")), str)
                    or not ref.get("id", ref.get("name"))
                ):
                    raise SurfaceError(key + " requires one Pawn id or name and optional map")
        if any(k in args for k in ("actor", "user")) and not state:
            raise SurfaceError("actor/user context requires a state aspect")
        if not cell and not state and stats is None and not any(k in args for k in ("actor", "user")):
            return self._fit_page(
                self._normalize_identities(await self.interaction._look_legacy({**args, "aspects": aspects})),
                args,
            )
        direct = (
            kind in ("pawn", "thing")
            and offset == 0
            and not set(obj) - {"kind", "ids", "names", "map", "include_fogged"}
            and len(obj.get("ids", obj.get("names", []))) == 1
            and not set(legacy) - {"identity"}
        )
        if cell or direct:
            base = {
                "status": "ok",
                "tool": "look",
                "object": obj,
                "scope": {"object": obj, "include_fogged": False if cell else obj.get("include_fogged", True)},
                "coverage": {},
                "facts": [{"object": obj, "facts": {}}],
                "not_read": [],
            }
        else:
            # Identity selection uses the established population/scope/ambiguity contract.
            # Mood/biography still use their existing reader, but no duplicate health/work read.
            base = await self.interaction._look_legacy(
                {
                    "object": raw_object,
                    "aspects": list(dict.fromkeys(["identity", *legacy])),
                    "detail": detail,
                    "offset": offset,
                }
            )
            if not base.get("facts"):
                return base
        return await self._enrich(base, obj, args, aspects, detail, direct=direct)

    async def _enrich(self, base, obj, args, aspects, detail, *, direct=False):
        cell = obj["kind"] == "cell"
        state = [aspect for aspect in aspects if aspect in STATE_ASPECTS]
        stats = args.get("stats")
        base = copy.deepcopy(base)
        base["requested_aspects"] = aspects
        base["not_read"] = [s for s in base.get("not_read", []) if not s.startswith("aspects not requested:")]
        base["state_observations"] = []
        focused_state = args.get("focus", {}).get("state")
        directory = getattr(getattr(self.interaction, "setup", None), "directory", {})
        contract = state_contract(directory)
        native_facets = bool(contract and "facets" in contract["parameters"]["properties"])
        if focused_state:
            base["state_facet_selection"] = {"requested": list(focused_state), "native_selected": native_facets}
        base["consistency"] = (
            "Selection and state have separate samples below; no atomic snapshot or silent merge across loads."
        )
        if direct:
            base["consistency"] = (
                "Identity and state come from the reported native observation; this is not an atomic snapshot."
            )
        facts = base["facts"]
        for offset in range(0, len(facts), 8):
            batch = facts[offset : offset + 8]
            if direct:
                target = {"kind": obj["kind"]}
                target["id" if "ids" in obj else "name"] = obj.get("ids", obj.get("names"))[0]
                if "map" in obj:
                    target["map"] = obj["map"]
                elif obj["kind"] == "thing" and "names" in obj:
                    target["map"] = "current"
                targets = [target]
            else:
                targets = [self._target(f["object"], obj) for f in batch]
            request = {
                "targets": targets,
                "sections": state or ["identity"],
                "include_fogged": False if cell else obj.get("include_fogged", True),
                "detail": "details" if detail else "summary",
            }
            if focused_state and native_facets:
                request["facets"] = list(focused_state)
            if direct:
                request["exact_lookup"] = True
            if detail:
                request["detail_limit"] = detail
            for key in ("stats", "actor", "user"):
                if key in args:
                    request[key] = copy.deepcopy(args[key])
            native = await self.interaction.execute_native(request, tool_name="inspect_state")
            data = order_world_fields(native["data"])
            base["state_observations"].append(
                {
                    "observation_id": native.get("observation_id") or data.get("observation_id"),
                    "sample": data.get("sample"),
                    "status": native.get("status"),
                    "context": data.get("context"),
                    "issues": native.get("issues", []),
                    "read_error": data.get("read_error"),
                    "changed_during_read": data.get("changed_during_read", False),
                }
            )
            if native["status"] != "ok":
                base["status"] = "partial"
            rows = data.get("objects", [])
            if len(rows) > len(batch) or native["status"] == "ok" and len(rows) != len(batch):
                raise SurfaceError("native state response does not match the requested target count")
            if len(rows) < len(batch):
                base["status"] = "partial"
                base["state_failure"] = {
                    key: copy.deepcopy(data[key])
                    for key in ("read_error", "resolution", "changed_during_read")
                    if key in data
                }
                rows = [
                    *rows,
                    *(
                        {"status": "unavailable", "reason": "state_read_incomplete", "requested_target": target}
                        for target in targets[len(rows) :]
                    ),
                ]
            for item, row in zip(batch, rows):
                item["state_sample"] = {
                    "tick": row.get("sample_tick"),
                    "observation_id": native.get("observation_id") or data.get("observation_id"),
                }
                if row.get("status") in ("unavailable", "unsupported") or "facets" not in row and not cell:
                    item["state"] = copy.deepcopy(row)
                    if direct and obj.get("names") and re.fullmatch(r"Thing_[A-Za-z0-9_]+\d+", obj["names"][0]):
                        base.setdefault("input_hints", []).append(
                            {
                                "code": "possible_id_in_name",
                                "field": "object.name" if "name" in args["object"] else "object.names",
                                "value": obj["names"][0],
                                "suggested_field": "object.id",
                                "message": "This unresolved exact display name resembles a stable Thing ID. If you intended an ID, retry with object.id; names are never silently coerced.",
                            }
                        )
                    base["status"] = "partial"
                    continue
                if cell:
                    item["facts"]["state" if "state" in state else "identity"] = (
                        row if "state" in state else row["target"]
                    )
                    continue
                if direct:
                    identity = row.get("target", {})
                    if not identity.get("id") or "ids" in obj and identity["id"] != obj["ids"][0]:
                        raise SurfaceError("native state target identity mismatch")
                    item["object"] = self._identity(identity)
                    item["facts"]["identity"] = {"object": copy.deepcopy(item["object"])}
                    base["coverage"].update({"matched": 1, "listing": "complete", "scan": "complete"})
                if row.get("target", {}).get("id") != item["object"].get("id"):
                    raise SurfaceError("native state target identity mismatch")
                item["state_coverage"] = row.get("coverage")
                facets = row["facets"]
                pawn_state = facets.get("pawn")
                if (
                    isinstance(pawn_state, dict)
                    and pawn_state.get("status") == "observed"
                    and "health_summary_fraction" in pawn_state
                ):
                    pawn_state["field_semantics"] = {
                        "health_summary_fraction": "Native overall health summary, not proof of no injuries or impaired capacities.",
                        "bleed_fraction_per_game_day": "Blood loss fraction per game day, not per second or a survival forecast.",
                        "pain_fraction": "Current pain fraction, separate from injury severity and blood loss.",
                    }
                if "state" in state:
                    visible_facets = (
                        {k: v for k, v in facets.items() if k not in ("pawn", "work", "stats")}
                    )
                    item["facts"]["state"] = {
                        "facets": visible_facets,
                        "unsupported_components": row.get("unsupported_components"),
                    }
                pawn = facets.get("pawn", {})
                for aspect in state:
                    if aspect == "state":
                        continue
                    if aspect == "work":
                        value = facets.get("work", {"status": "not_applicable"})
                    elif pawn.get("status") != "observed":
                        value = pawn or {"status": "not_applicable"}
                    elif aspect == "health":
                        value = {
                            k: v for k, v in pawn.items() if not _belongs(k, (*GEAR_FIELDS, "skills", "traits"))
                        }
                    elif aspect == "skills":
                        value = {k: v for k, v in pawn.items() if k == "status" or _belongs(k, ("skills",))}
                    else:
                        value = {k: v for k, v in pawn.items() if k == "status" or _belongs(k, GEAR_FIELDS)}
                    item["facts"][aspect] = value
                if stats is not None:
                    item["facts"]["stats"] = (
                        facets.pop("stats", {"status": "not_applicable"})
                        if "state" in state
                        else facets.get("stats", {"status": "not_applicable"})
                    )
        base["coverage"]["state_facets_read_successfully"] = (
            all(f.get("state_coverage", {}).get("registered_facets_read_successfully") is True for f in facts)
            if not cell
            else base["status"] == "ok"
        )
        if base["coverage"]["state_facets_read_successfully"] is False:
            base["status"] = "partial"
        base["not_read"].append(
            "Omitted aspects are not evidence of absence. stats are computed only when requested; contextual predicates require the relevant actor/user."
        )
        if obj["kind"] in PAWN_KINDS and ("work" in aspects or "state" in aspects):
            base["work_assignments"] = self.work_readings.summarize(base)
        return self._fit_page(self._normalize_identities(base), args)

    @staticmethod
    def _canonical_identity(value):
        """Add one stable location shape while retaining every legacy field.

        A held position never substitutes for an unavailable own position.
        The enclosing map can complete an own-position cell only when the
        returned identity explicitly says the Thing is spawned.
        """
        identity = copy.deepcopy(value)
        if not isinstance(identity, dict) or not identity.get("id"):
            return identity
        status = identity.get("status") if isinstance(identity.get("status"), dict) else {}
        raw = status.get("position") if isinstance(status.get("position"), dict) else identity.get("position")

        def cell(position, *, own=False):
            if not isinstance(position, dict) or any(type(position.get(k)) is not int for k in ("x", "z")):
                return None
            map_id = position.get("map", position.get("map_id"))
            if not map_id and own and identity.get("spawned") is True:
                map_id = identity.get("map_id") or (identity.get("map_held") or {}).get("id") or identity.get("map")
            if not isinstance(map_id, str) or not map_id:
                return None
            return {"kind": "cell", "map": map_id, "x": position["x"], "z": position["z"]}

        own = cell(raw, own=True)
        if identity.get("spawned") is False:
            own = None
        held = cell(identity.get("held_position"))
        identity["location"] = {"position": own, "held_position": held}
        # Keep both map spellings for clients already using either path.
        if own is not None:
            if isinstance(identity.get("position"), dict):
                identity["position"] = {**identity["position"], **own, "map_id": own["map"]}
            if isinstance(status.get("position"), dict):
                identity["status"] = {**status, "position": {**status["position"], **own, "map_id": own["map"]}}
        return identity

    @classmethod
    def _normalize_identities(cls, result):
        result = copy.deepcopy(result)
        for row in result.get("facts", []):
            if isinstance(row.get("object"), dict):
                row["object"] = cls._canonical_identity(row["object"])
            identity = row.get("facts", {}).get("identity")
            if isinstance(identity, dict) and isinstance(identity.get("object"), dict):
                identity["object"] = cls._canonical_identity(identity["object"])
        return result

    @staticmethod
    def _fit_page(base, arguments=None):
        facts = base.get("facts", [])
        original = len(facts)
        while (len(canonical(base)) > MAX_SOURCE_CHARS) or model_size(
            base, arguments=arguments, context_enabled=True
        ) > MAX_TOOL_RESULT_CHARS - 4096:
            if len(facts) <= 1:
                raise SurfaceError("Object facts exceed output bound; select fewer aspects or smaller detail")
            facts.pop()
        if len(facts) < original:
            base["coverage"]["listing"] = "partial"
            page = base.setdefault("page", {})
            page["returned"] = len(facts)
            page["has_more"] = True
            page["next_offset"] = (
                page.get("offset", 0) + len(facts) if base["coverage"].get("scan") == "complete" else None
            )
            page["reduced_for_size"] = True
            base["not_read"].append(
                "Additional object rows omitted for output size; use page.next_offset. Returned object facts are not pruned."
            )
        return base

    @staticmethod
    def _identity(native):
        identity = copy.deepcopy(native)
        definition = native.get("definition")
        identity["definition"] = {"def_name": definition, "label": native.get("definition_label")}
        identity["map_id"] = native.get("map")
        identity["map_held"] = {"id": native["map"]} if native.get("map") else None
        membership = native.get("pawn_membership") or {}
        identity.update(membership)
        if native.get("kind") == "pawn":
            identity["is_prisoner_of_colony"] = membership.get("is_prisoner")
            identity["is_slave_of_colony"] = membership.get("is_slave")
            identity["status"] = {"position": copy.deepcopy(native.get("position"))}
        else:
            identity["label"] = native.get("name")
        return identity

    @staticmethod
    def _target(identity, obj):
        if obj["kind"] == "cell":
            return copy.deepcopy(obj)
        target = {"id": identity["id"]}
        held = identity.get("map_held")
        candidates = (identity.get("map_id"), identity.get("map"), held.get("id") if isinstance(held, dict) else None, obj.get("map"))
        map_id = next((value for value in candidates if isinstance(value, str) and value), None)
        if map_id:
            target["map"] = map_id
        return target
