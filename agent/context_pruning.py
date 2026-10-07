"""Request-scoped content pruning. Model messages remain ordinary JSON.

No dictionaries, references, patches, model summarizer, or additional reads.
Raw tool results remain in the audit. Never merge observations or infer facts.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .serialization import canonical, digest

GUIDE = (
    "Ordinary JSON. Equal nested map/world/sample/context fields inherit their parent; "
    "repeated IDs within one read refer to its full identity. environment_shared supplies "
    "both views, with own fields overriding; path and corridor scopes remain separate. "
    "Named measurements use actual definition names. _xz contains ordered [x,z] coordinates "
    "in the declared/inherited map. field_states_by_status lists null fields by status/reason. "
    "Body-part keys use body_parts_owner and part index. Equal counts, same_tick and delivery "
    "aliases reconstruct from retained rows/ticks/sequence; partial coverage remains explicit. "
    "Omitted geometry/body_parts are not inspected. Missing transport/status aliases do not "
    "establish completeness; original evidence remains in the audit. "
    "health_percent is overall health, not blood volume or absence of injuries; bleed_rate "
    "is blood-loss fraction per game day (60000 ticks). Trait degree is a variant, not a stat bonus."
)

# Only these exact, code-authored explanations are covered by GUIDE. Changed
# text may qualify a different contract and must remain visible.
FORMAT_EXPLANATIONS = {
    "consistency": "Read-only samples over the reported interval; same tick does not guarantee an atomic snapshot of UI or other mod changes.",
    "bounds_policy": "Intersection with loaded map bounds; original requested footprint remains in geometry.",
    "environment_meaning": "Path nodes and surrounding corridor inspected over reported interval; not a carry-job simulation, not all future hazards.",
    "type_meaning": "HospitalBed is a building definition; medical_designation is a separate setting, including ordinary beds.",
    "roof_support_objects_format": "Identity/location references; component and containment metadata omitted. Use inspect_state for full object state. No support rows removed by this formatting; roof_support_objects_total still describes the observed total.",
    "not_read": ["Omitted aspects are not evidence of absence. stats are computed only when requested; contextual predicates require the relevant actor/user."],
    "not_modeled": ["future fire spread", "future enemy movement", "projectile travel or explosion blast simulation", "arbitrary mod hazards", "roof collapse prediction"],
}


# Exact supported member explanations covered by GUIDE; unknown additions stay.
EXPLAINED_SEMANTICS = frozenset(('A display name or definition that is ambiguous, missing or only partially scanned is never resolved to a single object.', 'An incomplete scan yields a lower bound (or unavailable), never an exact total.', 'Exact display names and definitions are resolved against a complete scan; an incomplete scan or several exact matches never resolves to one object.', 'Holder/container membership selects current containment; it is not faction or property ownership, and it does not establish usable, tradable or colony property.', 'Pawn includes humans, animals and mechanoids regardless of faction; Pawn is not a human, colonist or ownership identity.', 'Unknown is not zero and not empty: an unavailable read, an incomplete scan and a legitimate zero or empty collection are different facts.', 'age_ticks: Memory age in game ticks; null for non-memory thoughts.', 'aptitude: Game skill Aptitude, distinct from learned level, XP and passion.', 'bleed_rate: Native BleedRateTotal: blood-loss fraction per game day (60000 ticks). Multiply by 100 for percentage points per game day; never per second. This rate alone is not a computed survival-time estimate.', 'colonist_count: Number of spawned free colonists; excludes caravans/prisoners.', 'def_name: Native Faction.def.defName, for example PlayerColony. A definition name identifies the faction type, not a specific faction instance.', "def_name: Native ThingDef.defName; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", "degree: Trait variant identifier, not a numeric stat bonus. The game-generated description reports this trait's effects; suppression is separate.", "destroyed: Native Thing.Destroyed; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", 'guest_status: Native Pawn.GuestStatus (Guest/Prisoner/Slave); null when the pawn has no such status.', 'has_visible_threat: Game-provided active visible hostile threat signal, not an overall safety assessment.', 'health_percent is "Game summaryHealth.SummaryHealthPercent: an overall game-computed health indicator. Report it separately from conditions and body-part HP; this read is not a blood-volume measurement. A value of 1 does not establish a cause or a general rule about which conditions affect it." A value of 1 is therefore not "fully healthy".', 'health_percent: Game summaryHealth.SummaryHealthPercent: an overall game-computed health indicator. Report it separately from conditions and body-part HP; this read is not a blood-volume measurement. A value of 1 does not establish a cause or a general rule about which conditions affect it.', 'held_by is the nearest containing Pawn (containment, not ownership); null means no Pawn holds this instance. held_by_faction is that Pawn\'s faction: null means the holder has no faction (or there is no holder) - it does NOT mean the read failed. Use held_by_faction.is_player instead of inferring "ours" from containment.', 'hit_points/max_hit_points are the native Thing getters for this instance. A null with no issue at its path is the game returning no value for this member (some definitions carry no durability); that is neither zero nor a failed read. An issue at the path means the read failed.', "hit_points: Native Thing.HitPoints; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", 'id: Stable map ID within the save.', 'id: Transport metadata; not a native gameplay member.', 'instant: Game CurInstantLevelPercentage: current instantaneous/target mood from thoughts, not a historical observation.', 'is_colonist: Native Pawn.IsColonist: player-faction colonist. Distinct from IsFreeColonist (a colonist can be a slave or prisoner).', 'is_current: Whether this is the currently viewed map.', 'is_free_colonist: Native Pawn.IsFreeColonist: this pawn is a free member of the player faction. This is the native membership test, not an inference from kind, name or carried items.', 'is_player: Native Faction.IsPlayer: true only for the player\'s own faction. This is the native membership test for "is this ours"; it is not an ownership guess from inventory, containment or pawn kind.', "is_player_home: Game's player-home map flag.", 'is_prisoner_of_colony: Native Pawn.IsPrisonerOfColony.', 'is_slave_of_colony: Native Pawn.IsSlaveOfColony.', "label: Native Thing.Label; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", "label: Native ThingDef.label; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", 'level: Current mood need fraction, not the instantaneous target.', "map_id: Map ID attached to this Pawn by the selected root. The general pawns root includes held Pawns using their containing map (native MapHeld); this field alone does not establish spawned status. The spawnedFreeColonists root selects only FreeColonistsSpawned; that root membership already establishes spawned free-colonist status. status.position uses the Pawn's own spawned position.", "map_id: This Pawn's own spawned map position; null when not spawned. A null position does not establish absence from a loaded map or caravan membership.", "max_hit_points: Native Thing.MaxHitPoints; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", 'mood_offset: Effective mood offset for this group as computed by the game. Do not sum raw memory offsets as a substitute.', 'mood_offset: Individual thought offset; grouping/stacking can change its effective contribution.', 'name: Display name; not a unique identifier.', "name: Native Faction.Name: the faction's own display name.", 'pain: Game PainTotal aggregate, distinct from injury severity and health percentage. This aggregate alone does not attribute pain to individual listed conditions.', "parent_holder is this instance's immediate container, named by its native type: Verse.Map means the instance is spawned on that map rather than inside any container; a tracker/owner type (for example Verse.Pawn_InventoryTracker or Verse.ThingOwner) or a building type means it is inside that holder. Two instances sharing a parent_holder have the same immediate holder - containment, not ownership, and it never proves a location beyond what that type states.", 'player_relation: Native Faction.PlayerRelationKind (Hostile/Neutral/Ally) toward the player faction. Only a relation, not an action or a threat assessment.', 'records counts matching objects; units sums stack_count of those objects. A record is not necessarily an item or a human.', 'runtime_type: Actual CLR class name from GetType().FullName; an exact type, not its base-class hierarchy, faction or construction history.', 'runtime_type: Transport metadata; not a native gameplay member.', 'severity: Raw Hediff severity; meaning/range depends on its definition. Not a universal 0..1 health fraction.', 'spawned: Native Thing.Spawned: this instance itself is spawned on a map. false does not establish that it is outside loaded maps; a containing holder can be on a map.', 'stack_count: Native Thing.stackCount: units in this instance. count measures records; sum(stack_count) measures units. Non-item Things can also have stack_count.', "total_human_edible_nutrition: Native ResourceCounter.TotalHumanEdibleNutrition; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", "value: Native ResourceCountEntry.value; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", "x: Native IntVec3.x; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", "x: This Pawn's own spawned map position; null when not spawned. A null position does not establish absence from a loaded map or caravan membership.", "z: Native IntVec3.z; selected getter only. Interpret null using this member's meaning; an issue at its path means failed/unavailable.", "z: This Pawn's own spawned map position; null when not spawned. A null position does not establish absence from a loaded map or caravan membership."))


def _subset(small, large):
    return (
        isinstance(small, dict)
        and isinstance(large, dict)
        and all(k in large and (v == large[k] or _subset(v, large[k])) for k, v in small.items())
    )


def _aliases(x):
    """Remove copies only when their retained counterpart has equal facts."""
    if isinstance(x, list):
        return [_aliases(v) for v in x]
    if not isinstance(x, dict):
        return x
    out = {k: _aliases(v) for k, v in x.items()}
    if isinstance(out.get("scope"), dict) and out.get("object") == out["scope"].get("object"):
        out.pop("object", None)
    if isinstance(out.get("facts"), dict) and isinstance(out.get("object"), dict):
        identity = out["facts"].get("identity")
        if isinstance(identity, dict) and set(identity) == {"object"} and _subset(identity["object"], out["object"]):
            out["facts"].pop("identity")
    # Legacy profile exposes both profile.age and age, etc.
    if isinstance(out.get("profile"), dict):
        for key in ("age", "backstories", "traits"):
            if key in out and key in out["profile"] and out[key] == out["profile"][key]:
                out.pop(key)
    if isinstance(out.get("location"), dict):
        for key in ("position", "held_position"):
            v = out.get(key)
            retained = out["location"].get(key)
            if isinstance(v, dict) and isinstance(retained, dict):
                normalized = {k: z for k, z in v.items() if k != "map_id"}
                if normalized == retained and v.get("map_id", retained.get("map")) == retained.get("map"):
                    out.pop(key)
        if isinstance(out.get("status"), dict) and set(out["status"]) == {"position"}:
            pos = out["status"]["position"]
            retained = out["location"].get("position")
            if (
                isinstance(pos, dict)
                and isinstance(retained, dict)
                and {k: z for k, z in pos.items() if k != "map_id"} == retained
                and pos.get("map_id", retained.get("map")) == retained.get("map")
            ):
                out.pop("status")
    if "grid_distance" in out and out.get("grid_distance") == out.get("path_length"):
        out.pop("grid_distance")
    if isinstance(out.get("pawn_membership"), dict) and _subset(out["pawn_membership"], out):
        out.pop("pawn_membership")
    return out


def _identity(value):
    if not isinstance(value, dict):
        return value
    return {
        k: copy.deepcopy(v)
        for k, v in value.items()
        if k
        in {
            "id",
            "name",
            "kind",
            "definition",
            "map",
            "map_id",
            "map_held",
            "location",
            "position",
            "held_position",
            "spawned",
            "destroyed",
            "position_kind",
            "faction",
        }
    }


def route_view(value, *, environment=False, objects=False):
    """Return the requested read categories, not every incidental native read."""
    omit = set()
    if not environment:
        omit.update({"environment_contexts", "region", "environment", "path_environment"})
    if not objects:
        omit.update({"objects", "objects_summary", "object_state_scope", "page"})
    result = {k: copy.deepcopy(v) for k, v in value.items() if k not in omit}
    result["environment_included"] = environment
    result["objects_included"] = objects
    return result


def survey_view(value, arguments):
    """Keep the survey footprint; select categories only when explicitly asked."""
    result = copy.deepcopy(value)
    included = arguments.get("include", [])
    if "include" in arguments:
        environment = "environment" in included
        objects = bool(arguments.get("objects", "objects" in included) or arguments.get("cursor"))
        if not environment:
            for key in ("environment_contexts", "environment", "path_environment"):
                result.pop(key, None)
        if not objects:
            for key in ("objects", "objects_summary", "object_state_scope", "page"):
                result.pop(key, None)
        result.update(environment_included=environment, objects_included=objects)
        region = result.get("region")
        if "geometry" not in included and isinstance(region, dict) and "area_zone_intersections" in region:
            region.pop("area_zone_intersections")
            region["area_zone_intersections_included"] = False
    # Bind the native spatial distance to its actual origin, never to an actor
    # or the closest node on a route. This is a name change, not a computation.
    region = result.get("region")
    geometry = region.get("geometry") if isinstance(region, dict) else None
    center = geometry.get("resolved_center") if isinstance(geometry, dict) else None
    objects = result.get("objects")
    if isinstance(center, dict) and isinstance(objects, list):
        for row in objects:
            relation = row.get("spatial_relation") if isinstance(row, dict) else None
            if isinstance(relation, dict) and "geometric_distance" in relation and "distance_to_region_center" not in relation:
                relation["distance_to_region_center"] = relation.pop("geometric_distance")
    summary = result.get("objects_summary")
    selection = arguments.get("select", {})
    selected = selection.get("capabilities") if isinstance(selection, dict) else None
    if isinstance(summary, dict) and isinstance(selected, list) and selected and isinstance(summary.get("capabilities"), list):
        rows = summary["capabilities"]
        if all(isinstance(row, dict) and isinstance(row.get("capability"), str) for row in rows):
            summary["capabilities"] = [row for row in rows if row["capability"] in selected]
            summary["capabilities_selection"] = list(selected)
    return result


def activity_observations(data):
    """Drop the old duplicate candidate graph/rankings, retaining every read."""
    if not isinstance(data.get("reads"), dict) or not isinstance(data.get("candidates"), list):
        return data
    emitted, rows = set(), []
    for candidate in data["candidates"]:
        for stage in candidate.get("stages", []):
            key = stage.get("read")
            if key in emitted or key not in data["reads"]:
                continue
            emitted.add(key)
            rows.append(
                {
                    **{k: copy.deepcopy(v) for k, v in stage.items() if k != "read" and v is not None},
                    "observation": data["reads"][key],
                    **({"read_options": copy.deepcopy(data["read_options"][key])} if key in data.get("read_options", {}) else {}),
                }
            )
    if emitted != set(data["reads"]):
        return data  # Unknown/unbound extension must not silently disappear.
    return {**{k: v for k, v in data.items() if k not in ("reads", "candidates", "comparisons", "read_options")}, "observations": rows}


def focus_facts(data, focus):
    """Select explicitly requested named measurements; never guess relevance."""
    if not focus or not isinstance(data, dict):
        return
    for row in data.get("facts", []):
        facts = row.get("facts", {})
        facets = facts.get("state", {}).get("facets", {})
        if "state" in focus and isinstance(facts.get("state"), dict):
            requested = focus["state"]
            state = facts["state"]
            if isinstance(state.get("facets"), dict):
                state["facets"] = {name: copy.deepcopy(facets.get(name, {"status": "not_observed", "reason": "facet_not_returned_in_this_read"})) for name in requested}
                state["selected_facets"] = list(requested)
                state["all_facets_requested"] = False
            elif "environment" in state or "reservations" in state:
                for name in ("environment", "reservations"):
                    if name not in requested:
                        state.pop(name, None)
                state["selected_facets"] = list(requested)
            if isinstance(row.get("state_coverage"), dict):
                row["state_coverage"]["selected_state_facets"] = list(requested)
                row["state_coverage"]["all_state_facets_requested"] = False
                if any(isinstance(v, dict) and v.get("status") in ("not_observed", "partial", "unavailable", "unsupported") for v in state.get("facets", {}).values()):
                    row["state_coverage"]["registered_facets_read_successfully"] = False
                    if isinstance(data.get("coverage"), dict):
                        data["coverage"]["state_facets_read_successfully"] = False
        collections = {
            "skills": (facts.get("skills", facets.get("pawn", {})), "skills"),
            "work": (facts.get("work", facets.get("work", {})), "work_types"),
            "capacities": (facts.get("health", facets.get("pawn", {})), "capacities"),
        }
        for name, requested in focus.items():
            if name not in collections:
                continue
            parent, key = collections[name]
            values = parent.get(key) if isinstance(parent, dict) else None
            collection = values if isinstance(values, dict) and isinstance(values.get("rows"), list) else None
            if collection is not None:
                values = collection["rows"]
            if not isinstance(values, list):
                if isinstance(parent, dict) and parent:
                    parent[key + "_selection"] = {
                        "requested": list(requested),
                        "status": "unavailable",
                        "reason": "collection_not_observed_in_this_read",
                    }
                continue  # Preserve unavailable/legacy values without invention.

            def matches(v, term):
                return isinstance(v, dict) and term in (v.get("definition"), v.get("def_name"), v.get("label"))

            selected = [v for v in values if any(matches(v, term) for term in requested)]
            source_complete = (
                parent.get(key + "_complete") is True
                if collection is None
                else collection.get("listing") == "complete" and collection.get("state") in ("complete", "empty")
            )
            if collection is None:
                parent[key] = selected
            else:
                collection["rows"] = selected
                collection["returned"] = len(selected)
                collection["matched"] = len(selected) if source_complete else None
                collection["known_matches"] = len(selected)
                collection["listing"] = "complete" if source_complete else "partial"
                collection["state"] = "complete" if source_complete else "partial"
                collection["has_more"] = False if source_complete else None
                collection["offset"] = 0
                collection.setdefault("scope", {})["display_selection"] = list(requested)
            parent[key + "_selection"] = {
                "requested": list(requested),
                "unmatched": [term for term in requested if not any(matches(v, term) for v in values)],
                "scope": "requested names only; completeness applies to this selection. Unmatched has no observed value, not zero or disabled.",
            }
            parent[key + "_complete"] = source_complete
            parent[key + "_total"] = len(selected)


def _duplicate_work_summary(data):
    """A derived view is redundant only if these visible rows reproduce it."""
    summary = data.get("work_assignments")
    facts = data.get("facts")
    if not isinstance(summary, dict) or not isinstance(facts, list) or not facts:
        return False
    if len(summary.get("source_pages", [])) > 1:
        return False  # A union can contain observations absent from this page.
    if summary.get("source_pages"):
        page = summary["source_pages"][0]
        if not isinstance(page, dict) or page != {
            "selection_sample": data.get("sample", {}), "page": data.get("page", {}),
            "state_observation_ids": [row.get("observation_id") for row in data.get("state_observations", [])],
        }:
            return False
    if summary.get("sample"):
        sample = data.get("sample", {})
        if summary["sample"] != {"load_epoch": sample.get("load_epoch"), "game_tick": sample.get("game_tick"), "atomic": False}:
            return False
    from .object_tools import summarize_work_assignments

    expected = summarize_work_assignments(facts, data.get("coverage", {}))
    if summary.get("meaning", expected["meaning"]) != expected["meaning"]:
        return False
    if summary.get("scope") not in {
        expected["scope"],
        "Union of work tables read in this consultation for the same selection, load epoch and unchanged game tick; not an atomic snapshot.",
    }:
        return False
    # Scope/sample remain in the original row/page metadata. All computed work
    # facts, unknowns and completeness must agree, including actor-bound order.
    metadata = {"scope", "sample", "source_pages", "meaning"}
    return {k: v for k, v in summary.items() if k not in metadata} == {
        k: v for k, v in expected.items() if k not in metadata
    }


def _native_receipt(value):
    """Strip known wire bookkeeping, retaining the complete operation payload."""
    raw = value.get("data")
    if not isinstance(raw, dict) or not isinstance(raw.get("response"), dict):
        return value, False
    response = raw["response"]
    allowed = {"v", "type", "connection_epoch", "colony_id", "load_epoch", "seq", "request_id", "game_tick", "snapshot_sequence", "payload"}
    if set(raw) - {"observation_id", "received_at", "request", "response"} or set(response) - allowed:
        return value, False
    if not isinstance(response.get("payload"), dict):
        return value, False
    if set(response["payload"]) & {"observation_id", "received_at", "request", "sample"}:
        return value, False
    result = copy.deepcopy(value)
    result["data"] = {
        **copy.deepcopy(response["payload"]),
        **{k: copy.deepcopy(raw[k]) for k in ("observation_id", "received_at", "request") if k in raw},
        "sample": {k: copy.deepcopy(response[k]) for k in (
            "colony_id", "load_epoch", "connection_epoch", "game_tick", "snapshot_sequence"
        ) if k in response},
    }
    return result, True


def _record_view(value, arguments, removed):
    """Prune implementation prose and reconstructible aliases, not game values."""
    selector = arguments.get("object", {})
    if isinstance(selector, dict) and str(selector.get("kind", "")).endswith("_defs"):
        return value  # Definition prose/types are requested knowledge, not runtime noise.
    explanations = {"meaning", "field_semantics", "capability_meanings", "work_types_meaning", "medical_care_context"}
    grouped_type = arguments.get("by") == "runtime_type"

    def bound_identities(node, path):
        # Identity reuse is local to ONE observation. Never share identities
        # across reads, samples or worlds; never coalesce hazard predicates.
        node = dict(node)
        def identical_identity(identity, known):
            if not isinstance(identity, dict) or not identity.get("id") or len(identity) <= 1:
                return False
            matches = [full for full in known if isinstance(full, dict) and full.get("id") == identity["id"]]
            return bool(matches) and all(identity == full for full in matches)

        route = node.get("route")
        contexts = node.get("environment_contexts")
        if isinstance(route, dict) and isinstance(contexts, list):
            known = [route.get("actor"), route.get("target")]
            contexts = copy.deepcopy(contexts)
            for i, context in enumerate(contexts):
                pawn = context.get("pawn") if isinstance(context, dict) else None
                if identical_identity(pawn, known):
                    context["pawn"] = {"id": pawn["id"]}
                    removed.append(list((*path, "environment_contexts", i, "duplicate_identity")))
            node["environment_contexts"] = contexts
        corridor, on_path = node.get("environment"), node.get("path_environment")
        if isinstance(corridor, dict) and isinstance(on_path, dict) and isinstance(corridor.get("attack_influences"), list) and isinstance(on_path.get("attack_influences"), list):
            known = [row.get("source") for row in corridor.get("attack_influences", []) if isinstance(row, dict)]
            on_path = copy.deepcopy(on_path)
            for i, row in enumerate(on_path.get("attack_influences", [])):
                source = row.get("source") if isinstance(row, dict) else None
                if identical_identity(source, known):
                    row["source"] = {"id": source["id"]}
                    removed.append(list((*path, "path_environment", "attack_influences", i, "duplicate_identity")))
            node["path_environment"] = on_path
        return node

    def identity_copies(node, path):
        # Each native route, or one object fact row, is a separate binding frame.
        # Never reuse an entity's representation across reads or object rows.
        if not (isinstance(node.get("route"), dict) or
                isinstance(node.get("object"), dict) and isinstance(node.get("facts"), dict)):
            return node
        roles = {"object", "target", "actor", "user", "subject", "origin", "destination", "source", "pawn", "parent_holder"}
        fields = {"id", "kind", "name", "definition", "definition_label", "map", "map_id", "map_held",
                  "spawned", "destroyed", "position", "position_xz", "position_map", "held_position",
                  "held_position_xz", "held_position_map", "position_kind", "faction", "location",
                  "runtime_type", "native_type", "capabilities", "parent_holder"}
        versions = {}

        def eligible(value, role):
            return role in roles and isinstance(value, dict) and isinstance(value.get("id"), str) and len(value) > 2 and set(value) <= fields

        def scan(value, role=None):
            if eligible(value, role):
                versions.setdefault(value["id"], set()).add(canonical(value))
            if isinstance(value, dict):
                for key, child in value.items():
                    if key not in ("extension", "extensions", "unknown_mod_fields", "observation", "observations", "state_observations"):
                        scan(child, key)
            elif isinstance(value, list):
                for child in value:
                    scan(child, role)

        scan(node)
        emitted = set()

        def emit(value, role=None, local=()):
            if eligible(value, role) and len(versions[value["id"]]) == 1:
                if value["id"] in emitted:
                    removed.append(list((*path, *local, "same_read_identity")))
                    return {"id": value["id"]}
                emitted.add(value["id"])
            if isinstance(value, dict):
                return {key: copy.deepcopy(child) if key in ("extension", "extensions", "unknown_mod_fields", "observation", "observations", "state_observations")
                        else emit(child, key, (*local, key)) for key, child in value.items()}
            if isinstance(value, list):
                return [emit(child, role, (*local, i)) for i, child in enumerate(value)]
            return value

        return emit(node)

    def shared_environment(node, path):
        corridor, on_path = node.get("environment"), node.get("path_environment")
        if not isinstance(corridor, dict) or not isinstance(on_path, dict) or "environment_shared" in node:
            return node
        # These two views are from ONE native read. Each retains its own scope,
        # aggregate counts, hazards and errors; equal values are not a claim that
        # the corridor and path have the same footprint or coverage.
        common = {
            key: copy.deepcopy(corridor[key])
            for key in (
                "map_context", "rooms", "temperature_c", "glow_fraction",
                "path_cell_coverage", "path_gas_bands", "path_gas_bands_listing_complete",
                "path_gas_bands_scope", "path_environment_bands", "path_environment_bands_complete",
                "path_environment_bands_total", "path_environment_bands_included",
                "roof_support_objects", "roof_support_objects_complete",
                "roof_support_objects_total", "roof_support_objects_included",
            )
            if key in corridor and key in on_path
            and canonical(corridor[key]) == canonical(on_path[key])
        }
        if not common or len(canonical(common)) < 80:
            return node
        node = dict(node)
        node["environment_shared"] = common
        for name, original in (("environment", corridor), ("path_environment", on_path)):
            node[name] = {key: child for key, child in original.items() if key not in common}
            for key in common:
                removed.append(list((*path, name, key, "same_read_environment_shared")))
        return node

    def activity_world(node, path):
        rows = node.get("observations")
        if not isinstance(rows, list) or len(rows) < 2 or not all(
            isinstance(row, dict) and isinstance(row.get("observation"), dict) for row in rows
        ):
            return node
        common = {}
        for key in ("colony_id", "load_epoch", "connection_epoch"):
            values = [row["observation"].get(key) for row in rows]
            if all(isinstance(v, str) and v for v in values) and len(set(values)) == 1:
                if key not in node or node[key] == values[0]:
                    common[key] = values[0]
        if not common:
            return node
        node = {**common, **node}
        node["observations"] = [
            {**row, "observation": {key: child for key, child in row["observation"].items() if key not in common}}
            for row in rows
        ]
        for i in range(len(rows)):
            for key in common:
                removed.append(list((*path, "observations", i, "observation", key, "inherited_world")))
        return node

    def walk(node, inherited=None, path=()):
        inherited = inherited or {}
        if isinstance(node, list):
            return [walk(child, inherited, (*path, i)) for i, child in enumerate(node)]
        if not isinstance(node, dict):
            return node
        node = shared_environment(identity_copies(bound_identities(node, path), path), path)
        if isinstance(value, dict) and value.get("tool") == "evaluate_activity":
            node = activity_world(node, path)
        shared = {**inherited, **{k: v for k, v in node.items() if k in (
            "map", "colony_id", "load_epoch", "connection_epoch", "game_tick", "sample", "context"
        ) and (isinstance(v, dict) or isinstance(v, (str, int)) and not isinstance(v, bool))}}
        result = {}
        for key, child in node.items():
            if key in ("extension", "extensions", "unknown_mod_fields", "work_assignments"):
                result[key] = copy.deepcopy(child)
                continue
            omit = key in explanations
            omit |= key == "native_type"
            omit |= key == "received_at"  # Transport receipt time, not the game sample.
            sample = node.get("sample")
            if key == "delivery_sample" and isinstance(child, dict) and isinstance(sample, dict):
                omit |= bool(child) and set(child) <= {"game_tick", "snapshot_sequence"} and all(
                    type(v) is int and type(sample.get("finished_tick" if k == "game_tick" else k)) is int
                    and v == sample["finished_tick" if k == "game_tick" else k] for k, v in child.items()
                )
            if key == "same_tick" and type(child) is bool:
                omit |= type(node.get("started_tick")) is int and type(node.get("finished_tick")) is int and child == (node["started_tick"] == node["finished_tick"])
            omit |= key == "status" and child in ("observed", "ok") and len(node) > 1
            omit |= key == "issues" and child == []
            omit |= key == "read_error" and child is None
            omit |= key == "requested_stats" and child == []
            omit |= key in ("args", "where") and child == {} and "scope" in path
            omit |= key == "known_matches" and type(child) is int and type(node.get("matched")) is int and child == node["matched"]
            if key == "returned" and type(child) is int and isinstance(node.get("rows"), list):
                omit |= child == len(node["rows"])
            if key.endswith("_total") and type(child) is int and node.get(key[:-6] + "_complete") is True:
                collection = node.get(key[:-6])
                named = node.get(key[:-6] + "_by_definition")
                omit |= isinstance(collection, list) and child == len(collection)
                omit |= isinstance(named, dict) and child == len(named)
            if key == "semantics" and isinstance(child, list):
                kept = [v for v in child if not isinstance(v, str) or v not in EXPLAINED_SEMANTICS]
                if len(kept) != len(child):
                    removed.append(list((*path, key, "known_member_explanations")))
                if kept:
                    result[key] = kept
                continue
            omit |= key in FORMAT_EXPLANATIONS and child == FORMAT_EXPLANATIONS[key]
            if key in ("observation_started_tick", "observation_finished_tick") and isinstance(node.get("sample"), dict):
                sample_key = "started_tick" if key == "observation_started_tick" else "finished_tick"
                omit |= type(child) is int and child == node["sample"].get(sample_key)
            omit |= key == "runtime_type" and not grouped_type
            omit |= key == "holder_type" and child in ("Verse.Map", "RimWorld.Planet.Settlement", "RimWorld.Planet.World")
            if key == "holder_chain" and isinstance(child, dict) and isinstance(child.get("rows"), list):
                omit |= bool(child["rows"]) and all(isinstance(row, dict) and row.get("thing_id") is None and row.get("runtime_type") in (
                    "Verse.Map", "RimWorld.Planet.Settlement", "RimWorld.Planet.World"
                ) for row in child["rows"])
            omit |= key == "held_position" and node.get("position_kind") == "own" and isinstance(child, dict) and child == node.get("position")
            omit |= key == "map_id" and child is not None and "map" in node and child == node["map"]
            omit |= key == "definition_label" and isinstance(child, str) and isinstance(node.get("definition"), dict) and child == node["definition"].get("label")
            omit |= key == "label" and isinstance(child, str) and child == node.get("definition")
            if key == "grid_distance" and isinstance(child, dict) and isinstance(node.get("path_length"), dict):
                omit |= type(child.get("value")) in (int, float) and all(child.get(k) == node["path_length"].get(k) for k in ("value", "unit"))
            omit |= key in inherited and child is not None and not isinstance(child, (dict, list)) and child == inherited[key]
            omit |= key in ("sample", "context") and key in inherited and isinstance(child, dict) and canonical(child) == canonical(inherited[key])
            if omit:
                removed.append(list((*path, key)))
            else:
                result[key] = walk(child, shared, (*path, key))
        return result

    projected = walk(value)

    def named_values(node, path=()):
        if isinstance(node, list):
            return [named_values(child, (*path, i)) for i, child in enumerate(node)]
        if not isinstance(node, dict):
            return node
        result = {}
        # These are field-level null states, not object/predicate values. Group
        # identical statuses/reasons and retain all field names explicitly.
        field_states = node.get("field_states")
        existing = node.get("field_states_by_status", {})
        mergeable = isinstance(existing, dict) and all(
            isinstance(status, str) and isinstance(reasons, dict) and all(
                isinstance(reason, str) and isinstance(names, list) and all(isinstance(name, str) for name in names)
                for reason, names in reasons.items()
            ) for status, reasons in existing.items()
        )
        grouped, remaining, added = copy.deepcopy(existing) if mergeable else {}, {}, set()
        existing_names = {name for reasons in grouped.values() for names in reasons.values() for name in names}
        if isinstance(field_states, dict) and mergeable:
            for name, state in field_states.items():
                if (name in node and node[name] is None and isinstance(state, dict)
                    and set(state) == {"status", "reason"}
                    and isinstance(state["status"], str) and state["status"]
                    and isinstance(state["reason"], str) and name not in existing_names):
                    grouped.setdefault(state["status"], {}).setdefault(state["reason"], []).append(name)
                    added.add(name)
                else:
                    remaining[name] = copy.deepcopy(state)
            if added:
                result["field_states_by_status"] = grouped
        for key, child in node.items():
            if key in ("extension", "extensions", "unknown_mod_fields", "work_assignments"):
                result[key] = copy.deepcopy(child)
                continue
            if added and key == "field_states_by_status":
                continue
            if added and key == "field_states":
                if remaining:
                    result[key] = named_values(remaining, (*path, key))
                continue
            if key in added:
                removed.append(list((*path, key, "null_with_explicit_field_state")))
                continue
            if key in ("body_parts", "condition_part_details") and "body_parts_owner" not in node:
                rows = child.get("rows") if isinstance(child, dict) else child
                if isinstance(rows, list) and len(rows) > 1 and all(isinstance(r, dict) and isinstance(r.get("key"), str) and type(r.get("index")) is int for r in rows):
                    prefixes = {r["key"].removesuffix('/part:'+str(r["index"])) for r in rows}
                    if len(prefixes) == 1:
                        prefix = next(iter(prefixes)); pieces = prefix.split('/body:')
                        if len(pieces) == 2 and all(pieces) and all(r['key'] == prefix+'/part:'+str(r['index']) for r in rows):
                            result["body_parts_owner"] = {"pawn_id":pieces[0],"body_definition":pieces[1]}
                            short = [{k:copy.deepcopy(v) for k,v in r.items() if k != 'key'} for r in rows]
                            result[key] = {**copy.deepcopy(child),"rows":short} if isinstance(child,dict) else short
                            removed.append(list((*path,key,"repeated_body_key_prefix")))
                            continue
            rows = child.get("rows") if isinstance(child, dict) else child
            if key in ("skills", "capacities", "work_types") and isinstance(rows, list) and rows and key + "_by_definition" not in node and not (isinstance(child, dict) and "by_definition" in child):
                names = [row.get("definition") if isinstance(row, dict) else None for row in rows]
                if all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names):
                    values = {row["definition"]: named_values({k: v for k, v in row.items() if k != "definition"}, (*path, key, row["definition"])) for row in rows}
                    if isinstance(child, dict):
                        result[key] = {**{k: named_values(v, (*path, key, k)) for k, v in child.items() if k != "rows"}, "by_definition": values}
                    else:
                        result[key + "_by_definition"] = values
                    removed.append(list((*path, key, "repeated_definition_keys")))
                    continue
            if key in ("position", "held_position", "start", "end") and key + "_xz" not in node and key + "_map" not in node and isinstance(child, dict) and set(child) <= {"kind", "map", "x", "z"} and child.get("kind", "cell") == "cell" and type(child.get("x")) in (int, float) and type(child.get("z")) in (int, float):
                result[key + "_xz"] = [child["x"], child["z"]]
                if "map" in child:
                    result[key + "_map"] = child["map"]
                removed.append(list((*path, key, "cell_axis_keys")))
                continue
            if key == "example_cells" and key + "_xz" not in node and key + "_map" not in node and isinstance(child, list) and child and all(
                isinstance(cell, dict) and set(cell) <= {"kind", "map", "x", "z"}
                and cell.get("kind", "cell") == "cell" and isinstance(cell.get("map"), str)
                and type(cell.get("x")) is int and type(cell.get("z")) is int
                for cell in child
            ) and len({cell["map"] for cell in child}) == 1:
                result[key + "_xz"] = [[cell["x"], cell["z"]] for cell in child]
                result[key + "_map"] = child[0]["map"]
                removed.append(list((*path, key, "cell_axis_keys")))
                continue
            result[key] = named_values(child, (*path, key))
        return result

    return named_values(projected)


def condition_anatomy(value):
    """Retain condition-linked anatomy, not an unrequested full body roster."""
    if not (
        isinstance(value, dict)
        and all(isinstance(value.get(k), list) for k in ("conditions", "damaged_present_parts", "missing_parts", "body_parts"))
        and (isinstance(value.get("capacities"), list) or isinstance(value.get("capacities_by_definition"), dict))
    ):
        return value
    result = dict(value)
    indices = {r.get("part") for r in value["conditions"] if isinstance(r, dict) and type(r.get("part")) is int}
    indices.update(r.get("index") for group in ("damaged_present_parts", "missing_parts")
                   for r in value[group] if isinstance(r, dict) and type(r.get("index")) is int)
    result["condition_part_details"] = [r for r in result.pop("body_parts") if not isinstance(r, dict) or r.get("index") in indices]
    result["body_parts_included"] = False
    if "body_parts_complete" in result:
        result["body_parts_source_complete"] = result.pop("body_parts_complete")
    return result


def prune(value, arguments=None, *, seen_notes=None):
    """Filter content by the actual operation/request, not task-name keywords."""
    arguments = arguments or {}
    included = arguments.get("include", [])
    geometry = "geometry" in included
    body_parts = "body_parts" in included
    seen = set() if seen_notes is None else set(seen_notes)
    if isinstance(value, dict) and value.get("tool") == "inspect_threats":
        # Threat predicates, visibility, target/subject bindings and even unknown
        # extensions stay intact. Generic identity filtering is inappropriate here.
        projected, changed = _native_receipt(value)
        removed = ["native_transport_bookkeeping"] if changed else []
        return _record_view(copy.deepcopy(projected), arguments, removed), seen, removed
    if isinstance(value, dict) and value.get("tool") not in (
        None,
        "look",
        "count",
        "inspect_route",
        "inspect_space",
        "inspect_state",
        "survey_space",
        "evaluate_activity",
    ):
        return copy.deepcopy(value), seen, []
    removed = []
    value = _aliases(copy.deepcopy(value))
    tool = value.get("tool") if isinstance(value, dict) else None
    data = value.get("data", value) if isinstance(value, dict) else value

    if tool == "evaluate_activity" and "data" in value and isinstance(data, dict):
        value["data"] = data = activity_observations(data)
        if any("read_options" in row for row in data.get("observations", [])):
            for i, row in enumerate(data["observations"]):
                options = row.pop("read_options", {})
                operation = "look" if row.get("kind") in ("actor_state", "destination_state") else "inspect_route"
                projected, seen, paths = prune({"tool": operation, "data": row["observation"]}, options, seen_notes=seen)
                row["observation"] = projected["data"]
                removed.extend([["data", "observations", i, *path] if isinstance(path, list) else ["data", "observations", i, path] for path in paths])
            return _record_view(value, arguments, removed), seen, removed
        for row in data.get("observations", []):
            if row.get("kind") == "actor_state":
                focus_facts(row.get("observation"), arguments.get("actor_focus", {}))

    if tool == "count" and isinstance(data, dict) and isinstance(data.get("listing"), list):
        # Count's facts list contains a second identity-only copy of the listing.
        facts = data.get("facts", [])
        listing = {r.get("id"): r for r in data["listing"] if isinstance(r, dict) and r.get("id")}
        if facts and all(
            isinstance(r, dict)
            and not r.get("facts")
            and _subset(r.get("object"), listing.get(r.get("object", {}).get("id")))
            for r in facts
        ):
            data.pop("facts")
            removed.append("duplicate_count_identities")

    if tool == "look" and isinstance(data, dict):
        if _duplicate_work_summary(data):
            data.pop("work_assignments")
            removed.append("duplicate_work_summary")
        focus_facts(data, arguments.get("focus", {}))
        aspects = arguments.get("aspects", data.get("requested_aspects", []))
        if aspects and "identity" not in aspects:
            for row in data.get("facts", []):
                if isinstance(row, dict) and isinstance(row.get("object"), dict):
                    row["object"] = _identity(row["object"])
                    row.get("facts", {}).pop("identity", None)
            removed.append("unrequested_identity_details")

    if tool == "survey_space" and "data" in value and isinstance(data, dict):
        selected = survey_view(data, arguments)
        if selected != data:
            removed.append("requested_survey_categories")
        value["data"] = data = selected

    explicit_identity = tool == "count" or "identity" in arguments.get("aspects", [])

    if tool == "inspect_route" and "data" in value and isinstance(data, dict):
        value["data"] = data = route_view(
            data,
            environment="environment" in included,
            objects=bool(arguments.get("include_objects") or arguments.get("cursor") or "objects" in included),
        )

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
        if (
            not geometry
            and not explicit_identity
            and "id" in x
            and any(k in x for k in ("position", "held_position", "location"))
        ):
            x = _identity(x)
        # Internal implementation taxonomy is not the requested game state.
        # Keep count/explicit identity types, including parent_holder types.
        if "id" in x and ("position" in x or "held_position" in x or "location" in x):
            x.pop("capability_meanings", None)
            if not explicit_identity:
                x.pop("runtime_type", None)
                x.pop("holder_chain", None)
        if "unsupported_components" in x and isinstance(x["unsupported_components"], list):
            x["unsupported_components_count"] = len(x.pop("unsupported_components"))
        if not body_parts:
            selected = condition_anatomy(x)
            if selected is not x:
                x = selected
                removed.append(list(path + ("body_parts",)))
        if (
            not body_parts
            and isinstance(x.get("body_parts"), dict)
            and isinstance(x["body_parts"].get("rows"), list)
            and "summary" in x
            and "conditions" in x
        ):
            parts = copy.deepcopy(x["body_parts"])
            original = parts["rows"]
            # Historical query snapshots use collection envelopes. Retain every
            # damaged or unknown body part; do not turn a partial list complete.
            parts["rows"] = [
                r
                for r in original
                if not isinstance(r, dict)
                or type(r.get("health")) not in (int, float)
                or type(r.get("max_health")) not in (int, float)
                or r["health"] != r["max_health"]
                or r.get("missing") is True
            ]
            parts["returned"] = len(parts["rows"])
            if len(parts["rows"]) != len(original):
                parts["listing"] = "partial"
            parts["display_filter"] = "damaged, missing or unknown; full body_parts not requested"
            x["body_parts"] = parts
        if not geometry and "gases" in x and "coverage" in x:
            bands = x.get("path_environment_bands")
            if isinstance(bands, list) and bands:
                gas_bands = []
                for band in bands:
                    environment = band.get("environment") if isinstance(band, dict) else None
                    gases = environment.get("gases_0_255") if isinstance(environment, dict) else None
                    if not isinstance(gases, dict) or any(type(value) not in (int, float) or value != 0 for value in gases.values()):
                        gas_bands.append(copy.deepcopy(band))
                x["path_gas_bands"] = gas_bands
                x["path_gas_bands_listing_complete"] = x.get("path_environment_bands_complete")
                x["path_gas_bands_scope"] = "Observed nonzero or unknown gas bands only; path/visibility coverage remains separate. Other environment geometry was not requested."
            for key in ("path_environment_bands", "roof_support_objects"):
                if key in x:
                    x.pop(key)
                    x.pop(key + "_complete", None)
                    x[key + "_included"] = False
                    removed.append(list(path + (key,)))
            for group in ("attack_influences", "local_hazards"):
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
                        if isinstance(row.get("source"), dict) and row.get("checked_from") == row["source"].get(
                            "position"
                        ):
                            row.pop("checked_from", None)
                        if isinstance(row.get("path_indices"), list):
                            row["path_indices_count"] = len(row.pop("path_indices"))
                        if "source" in row:
                            row["source"] = _identity(row["source"])
                        filtered.append(row)
                    x[group] = filtered
        if (
            not geometry
            and all(k in x for k in ("status", "start", "end", "steps"))
            and isinstance(x.get("segments"), list)
        ):
            x["door_segments"] = [s for s in x.pop("segments") if not isinstance(s, dict) or s.get("door") is not None]
            x["geometry_included"] = False
            removed.append(list(path + ("segments",)))
        out = {}
        for key, child in x.items():
            # Measurement/debug metadata is not a game observation. Keep the
            # full original result and omission paths outside model context.
            if key in ("read_ms", "cooperative_budget_overrun"):
                removed.append(list(path + (key,)))
                continue
            if key in ("extension", "extensions", "unknown_mod_fields"):
                out[key] = copy.deepcopy(child)
            elif key in ("semantics", "notes") and isinstance(child, list) and all(isinstance(s, str) for s in child):
                kept = [v for s in child if (v := note(s)) is not None]
                if kept:
                    out[key] = kept
                if len(kept) != len(child):
                    removed.append(list(path + (key,)))
            elif (
                key == "field_semantics" and isinstance(child, dict) and all(isinstance(s, str) for s in child.values())
            ):
                kept = {k: v for k, s in child.items() if (v := note(s)) is not None}
                if kept:
                    out[key] = kept
            elif key in ("meaning", "work_types_meaning", "medical_care_context") and isinstance(child, str):
                if (v := note(child)) is not None:
                    out[key] = v
            else:
                out[key] = walk(child, path + (key,))
        return out

    projected = walk(value)
    if tool in {"look", "count", "inspect_route", "inspect_state", "inspect_space", "survey_space", "evaluate_activity"}:
        projected = _record_view(projected, arguments, removed)
    return projected, seen, removed


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
        return dict(
            format="json",
            projection="request_scoped_content",
            source_chars=self.source_chars,
            view_chars=self.view_chars,
            source_sha256=self.source_sha256,
            view_sha256=digest(self.content),
            omission_count=len(self.omissions),
            omissions=list(self.omissions[:128]),
        )


class ContextPruner:
    guide = GUIDE

    def __init__(self):
        self.seen_notes = set()

    def compile(self, value, *, arguments=None, max_view_chars=None):
        raw = canonical(value)
        projected, seen, omitted = prune(value, arguments, seen_notes=self.seen_notes)
        content = canonical(projected)
        if max_view_chars is not None and len(content) > max_view_chars:
            raise ValueError("evidence_view_limit")
        self.seen_notes = seen
        return PrunedEvidence(content, len(raw), digest(raw), tuple(omitted))
