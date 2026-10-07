"""Input and field contracts exercised by the rescue composition study."""

import copy

import pytest
from jsonschema import ValidationError, validate

from plugin.plugins.neko_rimworld.agent import semantic_surface as s
from plugin.plugins.neko_rimworld.agent.query_surface import Surface


def test_exact_thing_set_preserves_scope_and_does_not_change_single_thing():
    obj = s.normalize_object(
        {
            "kind": "things",
            "ids": ["medicine1", "medicine2"],
            "map": "Map_7",
            "include_held": False,
            "include_fogged": False,
        }
    )
    assert s.route_object(obj) == (
        "things",
        {
            "map_id": "Map_7",
            "include_held": False,
            "visible_only": True,
        },
        {"id": {"in": ["medicine1", "medicine2"]}},
    )
    assert s.route_object(s.normalize_object({"kind": "thing", "id": "medicine1"})) == (
        "thing",
        {"id": "medicine1"},
        None,
    )
    with pytest.raises(s.SurfaceError, match="exactly one"):
        s.normalize_object({"kind": "thing", "ids": ["medicine1", "medicine2"]})
    for selector in ({"definition": "MedicineIndustrial"}, {"category": "Item"}, {"name": "medicine"}):
        with pytest.raises(s.SurfaceError):
            s.normalize_object({"kind": "things", "ids": ["medicine1"], **selector})


def test_category_enum_is_native_and_only_applies_to_physical_things():
    directory = {"value_domains": {"Verse.ThingCategory": {"values": ["Item", "Building", "Pawn"]}}}
    original = copy.deepcopy(directory)
    schema = s.tool_schemas(directory)[0]["parameters"]
    validate({"object": {"kind": "things", "category": "Item"}, "aspects": ["identity"]}, schema)
    with pytest.raises(ValidationError):
        validate({"object": {"kind": "things", "category": "Medicine"}, "aspects": ["identity"]}, schema)
    # thing_defs uses the separate native ThingCategoryDef content-membership argument.
    validate({"object": {"kind": "thing_defs", "category": "Medicine"}, "aspects": ["identity"]}, schema)
    assert directory == original
    assert "things: ids, names" in schema["properties"]["object"]["description"]
    assert "enum" not in s.tool_schemas()[0]["parameters"]["properties"]["object"]["properties"]["category"]


def test_pawn_target_alias_is_public_for_single_and_batch_without_changing_directory():
    native = s.spatial_tool_schema()["parameters"]
    native["properties"]["to"]["properties"]["kind"]["enum"] = ["thing", "cell"]
    # Native SpatialContract restricts id/name branches as well as the top enum.
    for form in native["properties"]["to"]["oneOf"][:2]:
        form["properties"] = {"kind": {"const": "thing"}}
        form["allOf"] = [{"not": {"anyOf": [{"required": ["x"]}, {"required": ["z"]}]}}]
    directory = {"spatial_query": {"parameters": native, "route_summary": "native_path_summary.v1"}}
    original = copy.deepcopy(directory)
    schema = s.spatial_tool_schema(directory)["parameters"]
    actor = {"kind": "pawn", "id": "helper"}
    target = {"kind": "pawn", "id": "patient"}
    validate({"from": actor, "to": target}, schema)
    validate({"from": actor, "to": [target], "order_by": "steps"}, schema)
    with pytest.raises(ValidationError):
        validate({"from": {"kind": "thing", "id": "helper"}, "to": target}, schema)
    with pytest.raises(ValidationError):
        validate({"from": actor, "to": {"kind": "pawn", "x": 1, "z": 2}}, schema)
    assert directory == original


def test_identity_positions_and_trait_description_are_capability_gated():
    directory = {
        "roots": [{"from": "pawn", "type": "Pawn"}, {"from": "thing", "type": "Thing"}],
        "field_index": {
            "Pawn": {"id": "String", "status": "Status", "profile": "Profile"},
            "Status": {"position": "Position"},
            "Position": {"x": "Int", "z": "Int", "map_id": "String"},
            "Thing": {"id": "String", "position": "Position"},
            "Profile": {"traits": "Trait[]"},
            "Trait": {"description": "String"},
        },
    }
    surface = Surface(directory)
    assert {"status.position.x", "status.position.z", "status.position.map_id"} <= set(
        s._pawn_entries(surface, "identity", 0)[0]["fields"]
    )
    assert {"position.x", "position.z"} <= set(s._supported(surface, "thing", s.IDENTITY_FIELDS["thing"]))
    assert "description" in s._pawn_entries(surface, "profile", 5)[-1]["fields"]
    directory["field_index"]["Status"] = {}
    directory["field_index"]["Trait"] = {}
    old = Surface(directory)
    assert s._pawn_entries(old, "identity", 0)[0]["fields"] == ["id"]
    assert "description" not in s._pawn_entries(old, "profile", 5)[-1]["fields"]
