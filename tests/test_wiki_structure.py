"""The Wort pilot: exact reads, complete sibling groups, no intent aliases."""

import json

from plugin.plugins.neko_rimworld.agent.wiki_tools import WikiStore

from .test_wiki_tools import entry, write_snapshot

RECIPE = "Verse.RecipeDef/Make_Wort"


def recipe():
    noise = [{"name": f"noise{i}.count", "value": "0"} for i in range(40)]
    return entry(
        "Make_Wort",
        "制作麦芽汁",
        key=RECIPE,
        type="Verse.RecipeDef",
        description="使用啤酒花制作麦芽汁。",
        facts=noise
        + [
            {"name": "workAmount", "value": "1000", "type": "System.Single"},
            {"name": "products.count", "value": "1"},
            {"name": "products[0].thingDef", "value": "Verse.ThingDef/Wort", "type": "Verse.ThingDef"},
            {"name": "products[0].count", "value": "5", "type": "System.Int32"},
            {"name": "products[0].quality", "value": "Normal"},
            {"name": "recipeUsers", "value": "null", "type": "null"},
            {"name": "workSpeedStat", "value": "RimWorld.StatDef/DrugCookingSpeed", "type": "RimWorld.StatDef"},
        ],
        related=[],
    )


def store(tmp_path):
    write_snapshot(
        tmp_path,
        [
            recipe(),
            entry("Wort", "麦芽汁"),
            entry("Count", "总督", type="RimWorld.RoyalTitleDef", key="RimWorld.RoyalTitleDef/Count"),
            entry("DrugCookingSpeed", "酿酒速度", type="RimWorld.StatDef", key="RimWorld.StatDef/DrugCookingSpeed"),
        ],
    )
    return WikiStore(tmp_path)


def test_exact_path_returns_siblings_and_requested_null(tmp_path):
    result = store(tmp_path).query(RECIPE + " products[0].count recipeUsers")
    e = result["entries"][0]
    values = {f["name"]: f["value"] for f in e["facts"]}
    assert values["products[0].count"] == "5"
    assert values["products[0].thingDef"] == "Verse.ThingDef/Wort"
    assert values["recipeUsers"] == "null"
    assert all(not n.startswith("noise") for n in values)
    assert e["read_complete"] is True
    assert result["status"] == "found"


def test_invalid_key_never_silently_switches_to_wort(tmp_path):
    r = store(tmp_path).query("Verse.RecipeDef/BrewWort workAmount 麦芽汁")
    assert r["status"] == "not_found" and not r["entries"]
    assert r["requested_key"] == "Verse.RecipeDef/BrewWort"


def test_missing_field_not_replaced_with_arbitrary_summary(tmp_path):
    r = store(tmp_path).query(RECIPE + " nonexistentField")
    e = r["entries"][0]
    assert e["facts"] == []
    assert e["missing_fields"] == ["nonexistentField"]
    assert r["status"] == "found"  # the object exists, not the requested field


def test_prefix_read_returns_all_product_fields(tmp_path):
    e = store(tmp_path).query(RECIPE + " products")["entries"][0]
    assert {f["name"] for f in e["facts"]} == {
        "products.count",
        "products[0].thingDef",
        "products[0].count",
        "products[0].quality",
    }
    assert e["read_complete"]


def test_reference_label_is_actual_linked_definition(tmp_path):
    e = store(tmp_path).query(RECIPE + " workSpeedStat")["entries"][0]
    f = next(f for f in e["facts"] if f["name"] == "workSpeedStat")
    assert f["label"] == "酿酒速度"
    assert f["value"] == "RimWorld.StatDef/DrugCookingSpeed"


def test_discovery_keeps_item_and_recipe_candidates(tmp_path):
    r = store(tmp_path).query("麦芽汁 wort 酿造 brew 工作量 workAmount")
    assert {RECIPE, "Verse.ThingDef/Wort"} <= {e["key"] for e in r["entries"]}
    assert r["status"] == "ambiguous"


def test_small_entry_is_not_limited_to_eight_facts(tmp_path):
    facts = [{"name": f"field{i}", "value": str(i)} for i in range(20)]
    write_snapshot(tmp_path, [entry(facts=facts, related=[])])
    e = WikiStore(tmp_path).query("Verse.ThingDef/Steel")["entries"][0]
    assert e["facts"] == facts and e["read_complete"]


def test_long_block_is_not_cut_into_misleading_fragments(tmp_path):
    rec = recipe()
    rec["facts"] = [{"name": f"products[0].field{i}", "value": "说明" * 400} for i in range(20)]
    write_snapshot(tmp_path, [rec])
    r = WikiStore(tmp_path).query(RECIPE + " products[0]")
    e = r["entries"][0]
    assert not e["read_complete"] and e["not_read"]
    assert not e["source_truncated"]
    assert len(json.dumps(r, ensure_ascii=False).encode()) <= 8192


def test_explicit_null_is_not_marked_missing(tmp_path):
    e = store(tmp_path).query(RECIPE + " recipeUsers")["entries"][0]
    assert e["facts"] == [{"name": "recipeUsers", "value": "null", "type": "null"}]
    assert e["missing_fields"] == [] and e["read_complete"]


def test_large_parent_group_falls_back_to_explicit_leaf_fields(tmp_path):
    facts = [{"name": "plant." + ("noise" + str(i)), "value": "说明" * 400} for i in range(40)]
    facts.extend(
        [
            {"name": "plant.growDays", "value": "3", "type": "System.Single"},
            {"name": "plant.harvestYield", "value": "6", "type": "System.Int32"},
            {"name": "plant.harvestedThingDef", "value": "Verse.ThingDef/Rice"},
        ]
    )
    source = entry(
        "Plant_Rice", "水稻", type="Verse.ThingDef", key="Verse.ThingDef/Plant_Rice", facts=facts, related=[]
    )
    write_snapshot(tmp_path, [source])
    result = WikiStore(tmp_path).query("Verse.ThingDef/Plant_Rice plant.growDays plant.harvestYield")
    detail = result["entries"][0]
    assert {row["name"] for row in detail["facts"]} == {"plant.growDays", "plant.harvestYield"}
    assert {row["value"] for row in detail["facts"]} == {"3", "6"}
    assert detail["read_complete"] is True
    assert detail["not_read"] == []
    assert detail["context_not_read"] == ["plant"]
    assert detail["directory"] == [] and detail["field_names"] == []
    assert detail["response_truncated"] and not detail["source_truncated"]
    assert detail["missing_fields"] == []


def test_explicit_leaves_survive_other_groups_and_parent_requests(tmp_path):
    facts = [
        {"name": "settings.text", "value": "x" * 4200},
        {"name": "settings.value", "value": "2"},
        {"name": "plant.noise", "value": "x" * 10000},
        {"name": "plant.growDays", "value": "3"},
        {"name": "plant.harvestYield", "value": "6"},
        {"name": "plant.optional", "value": "null", "type": "null"},
    ]
    write_snapshot(tmp_path, [entry(facts=facts, related=[])])
    for suffix in [
        "settings.value plant plant.growDays plant.harvestYield plant.optional plant.absent",
        "plant.optional plant.harvestYield plant.growDays plant settings.value plant.absent",
    ]:
        result = WikiStore(tmp_path).query("Verse.ThingDef/Steel " + suffix)
        detail = result["entries"][0]
        values = {f["name"]: f["value"] for f in detail["facts"]}
        assert values["plant.growDays"] == "3" and values["plant.harvestYield"] == "6"
        assert values["settings.value"] == "2" and values["plant.optional"] == "null"
        assert detail["not_read"] == ["plant"] and not detail["read_complete"]
        assert detail["missing_fields"] == ["plant.absent"]
        assert len(detail["facts"]) == len(values)  # overlapping requests do not duplicate facts
        assert len(json.dumps(result, ensure_ascii=False).encode()) <= 8192


def test_small_requested_groups_precede_large_groups_regardless_of_query_order(tmp_path):
    facts = [
        {"name": "large.a", "value": "x" * 4000},
        {"name": "large.b", "value": "x" * 1800},
        {"name": "products[0].thingDef", "value": "Verse.ThingDef/Wort"},
        {"name": "products[0].count", "value": "5"},
        {"name": "stats[0].stat", "value": "speed"},
        {"name": "stats[0].value", "value": "2"},
    ]
    write_snapshot(tmp_path, [entry(facts=facts, related=[])])
    for suffix in ["large products stats", "stats large products"]:
        detail = WikiStore(tmp_path).query("Verse.ThingDef/Steel " + suffix)["entries"][0]
        values = {f["name"]: f["value"] for f in detail["facts"]}
        assert values["products[0].count"] == "5"
        assert values["products[0].thingDef"] == "Verse.ThingDef/Wort"
        assert values["stats[0].value"] == "2"
        assert not detail["source_truncated"]


def test_oversized_exact_leaf_is_omitted_without_hiding_other_requests(tmp_path):
    facts = [{"name": "data.huge", "value": "x" * 10000}, {"name": "data.small", "value": "6"}]
    write_snapshot(tmp_path, [entry(facts=facts, related=[])])
    result = WikiStore(tmp_path).query("Verse.ThingDef/Steel data.huge data.small")
    detail = result["entries"][0]
    assert detail["facts"] == [facts[1]]
    assert detail["not_read"] == ["data.huge"] and not detail["read_complete"]
    assert not detail["source_truncated"] and detail["response_truncated"]


def test_oversized_indexed_record_preserves_exact_leaf_and_reference_label(tmp_path):
    rec = recipe()
    rec["facts"].append({"name": "products[0].largeCustomField", "value": "x" * 12000})
    write_snapshot(tmp_path, [rec, entry("Wort", "麦芽汁")])
    result = WikiStore(tmp_path).query(RECIPE + " products[0].thingDef products[0].count recipeUsers")
    detail = result["entries"][0]
    values = {f["name"]: f for f in detail["facts"]}
    assert values["products[0].thingDef"]["label"] == "麦芽汁"
    assert values["products[0].count"]["value"] == "5"
    assert values["recipeUsers"]["type"] == "null"
    assert detail["read_complete"] and not detail["not_read"]
    assert detail["context_not_read"] == ["products[0]"]
