from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.plugins.neko_rimworld import knowledge as knowledge_module
from plugin.plugins.neko_rimworld.agent import ToolCall, wiki_tools
from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError, ToolRegistry
from plugin.plugins.neko_rimworld.agent.wiki_tools import WikiStore, knowledge_directory, make_wiki_tool


def entry(name="Steel", label="steel", **changes):
    return {
        "key": "Verse.ThingDef/" + name,
        "type": "Verse.ThingDef",
        "def_name": name,
        "label": label,
        "description": "Definition description",
        "source_mod": "Core",
        "package_id": "ludeon.rimworld",
        "facts": [{"name": "stackLimit", "value": "75"}],
        "related": [{"key": "Verse.ThingCategoryDef/ResourcesRaw", "label": "raw resources"}],
        "truncated": False,
        **changes,
    }


def write_snapshot(directory: Path, entries=None, **changes):
    snapshot = {
        "format": "neko.defs.v1",
        "game_version": "1.6",
        "language": "English",
        "generated_at": "2026-09-01T00:00:00Z",
        "complete": True,
        "issues": [],
        "mods": [{"package_id": "ludeon.rimworld", "name": "Core"}],
        "entries": [entry()] if entries is None else entries,
        **changes,
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "current.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return path


def test_directory_contract(monkeypatch, tmp_path):
    monkeypatch.delenv("NEKO_RIMWORLD_KNOWLEDGE_DIR", raising=False)
    monkeypatch.setattr(wiki_tools.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert knowledge_directory() == tmp_path / "xdg/neko-rimworld/knowledge"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert knowledge_directory() == tmp_path / ".local/share/neko-rimworld/knowledge"
    monkeypatch.setattr(wiki_tools.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "windows"))
    assert knowledge_directory() == tmp_path / "windows/neko-rimworld/knowledge"
    monkeypatch.setenv("NEKO_RIMWORLD_KNOWLEDGE_DIR", str(tmp_path / "override"))
    assert knowledge_directory() == tmp_path / "override"


@pytest.mark.asyncio
async def test_one_minimal_tool_returns_mapping_not_observation(tmp_path):
    write_snapshot(tmp_path)
    store = WikiStore(tmp_path)
    assert store.load()
    tool = make_wiki_tool(store)
    assert tool.name == "wiki"
    assert tool.parameters == {
        "type": "object",
        "properties": {"query": {"type": "string", "maxLength": 4096}},
        "required": ["query"],
        "additionalProperties": False,
    }
    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute(registry.prepare(ToolCall("w1", "wiki", '{"query":"Steel"}')))
    assert result.ok and result.observation is None
    data = result.value["data"]
    assert data["status"] == "found"
    assert data["snapshot"]["game_version"] == "1.6"
    assert len(data["snapshot"]["sha256"]) == 64
    assert data["applicability"]["current_game_match_verified"] is False
    assert "不是当前存档观察" in data["notice"]
    assert "observation_id" not in data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{}, {"query": "Steel", "limit": 1}, {"query": None}, {"query": 1}, {"query": " "}, {"query": "a" * 4097}],
)
async def test_argument_validation(tmp_path, arguments):
    with pytest.raises(ToolExecutionError):
        await make_wiki_tool(WikiStore(tmp_path)).handler(arguments)


def test_exact_matches_rank_before_fuzzy_and_ambiguity_is_stable(tmp_path):
    write_snapshot(
        tmp_path,
        [
            entry("SteelWall", "steel wall"),
            entry("Steel", "steel"),
            entry("ModSteel", "steel"),
            entry("A", "steel plate"),
        ],
    )
    store = WikiStore(tmp_path)
    assert store.query("vErSe.ThingDef/sTEEL")["entries"][0]["def_name"] == "Steel"
    result = store.query("STEEL")
    assert result["status"] == "ambiguous"
    assert [e["def_name"] for e in result["entries"]] == ["ModSteel", "Steel"]
    assert store.query("wall steel")["entries"][0]["def_name"] == "SteelWall"
    result = store.query("ste")
    assert result["status"] == "ambiguous" and result["match_count"] == 4
    assert len(result["entries"]) == 3 and result["candidates_truncated"]
    assert store.query("unknown")["status"] == "not_found"


@pytest.mark.parametrize(
    "changes", [{"complete": False}, {"issues": ["one type failed"]}, {"entries": [entry(truncated=True)]}]
)
def test_partial_snapshot_and_entries_are_not_reported_as_complete(tmp_path, changes):
    write_snapshot(tmp_path, **changes)
    result = WikiStore(tmp_path).query("Steel")
    assert result["status"] == "partial" and result["resolution"] == "found"


def test_incomplete_not_found_is_not_an_absence_claim(tmp_path):
    write_snapshot(tmp_path, complete=False)
    result = WikiStore(tmp_path).query("not there")
    assert result["status"] == "partial" and result["resolution"] == "not_found"


def test_store_pins_snapshot_until_next_run(tmp_path):
    path = write_snapshot(tmp_path)
    store = WikiStore(tmp_path)
    before = store.query("Steel")
    replacement = tmp_path / "next"
    write_snapshot(replacement, [entry("Gold", "gold")])
    (replacement / "current.json").replace(path)
    assert store.query("Steel") == before
    fresh = WikiStore(tmp_path)
    assert fresh.query("Steel")["status"] == "not_found"
    assert fresh.query("Gold")["snapshot"]["sha256"] != before["snapshot"]["sha256"]


@pytest.mark.parametrize("content", ["broken", "[]", '{"format":"future"}', "\ufeff{}"])
def test_corrupt_snapshots_are_controlled_unavailable(tmp_path, content):
    (tmp_path / "current.json").write_text(content, encoding="utf-8")
    store = WikiStore(tmp_path)
    assert store.load() is False
    assert store.query("Steel")["status"] == "unavailable"


def test_missing_and_oversized_snapshot(tmp_path, monkeypatch):
    assert WikiStore(tmp_path).query("Steel")["status"] == "unavailable"
    write_snapshot(tmp_path)
    monkeypatch.setattr(knowledge_module, "MAX_SNAPSHOT_BYTES", 10)
    assert not WikiStore(tmp_path).load()


def test_changed_in_place_file_is_rejected(tmp_path, monkeypatch):
    write_snapshot(tmp_path)
    actual = knowledge_module.signature
    calls = []

    def changing(value):
        calls.append(1)
        return (*actual(value), len(calls))

    monkeypatch.setattr(knowledge_module, "signature", changing)
    assert not WikiStore(tmp_path).load()


@pytest.mark.parametrize("text", ["长" * 2000, "\x00" * 2000, "a" * 2000])
def test_result_and_entry_details_are_bounded(tmp_path, text):
    facts = [{"name": text, "value": text} for _ in range(50)]
    related = [{"key": text, "label": text} for _ in range(50)]
    write_snapshot(
        tmp_path,
        [entry(str(i), "steel", description=text, facts=facts, related=related) for i in range(5)],
        mods=[{"package_id": text, "name": text}] * 100,
    )
    result = WikiStore(tmp_path).query("steel")
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 8192
    assert result["status"] == "ambiguous"
    assert 1 <= len(result["entries"]) <= 3
    assert all(e["candidate"] and e["not_read"] and e["facts"] == [] for e in result["entries"])


@pytest.mark.parametrize(
    "diagnostics", [None, {}, "bad", [None], [{}], [{"path": "x", "kind": "incomplete", "reason": 1}]]
)
def test_optional_diagnostics_are_validated(tmp_path, diagnostics):
    write_snapshot(tmp_path, [entry(diagnostics=diagnostics)])
    assert WikiStore(tmp_path).query("Steel")["status"] == "unavailable"


@pytest.mark.parametrize(
    "changes",
    [
        {"warnings": None},
        {"warnings": [1]},
        {"provenance_complete": "false"},
        {"entries": [entry(facts=[{"name": "x", "value": 1}])]},
    ],
)
def test_extended_v1_contract_rejects_wrong_types(tmp_path, changes):
    write_snapshot(tmp_path, **changes)
    assert not WikiStore(tmp_path).load()


def test_old_v1_works_without_optional_metadata(tmp_path):
    write_snapshot(tmp_path)
    result = WikiStore(tmp_path).query("Steel")
    assert result["status"] == "found"
    assert result["snapshot"]["format"] == "neko.defs.v1"
    assert result["snapshot"]["provenance_complete"] is True
    assert result["warnings"] == []
    assert result["entries"][0]["diagnostics"] == []
    assert not result["entries"][0]["diagnostics_truncated"]


@pytest.mark.parametrize("query,status", [("Steel", "found"), ("missing", "not_found")])
def test_provenance_warnings_do_not_make_complete_definitions_partial(tmp_path, query, status):
    write_snapshot(tmp_path, warnings=["unknown package"], provenance_complete=False)
    result = WikiStore(tmp_path).query(query)
    assert result["status"] == status and "resolution" not in result
    assert result["snapshot"]["complete"] is True
    assert result["snapshot"]["provenance_complete"] is False
    assert result["warnings"] == ["unknown package"]
    write_snapshot(tmp_path, warnings=["unknown package"], issues=["type load failed"])
    result = WikiStore(tmp_path).query(query)
    assert result["status"] == "partial" and result["resolution"] == status


def test_incomplete_diagnostics_are_prioritized_and_do_not_mutate_raw_snapshot(tmp_path):
    notes = [{"path": f"field{i}", "kind": "excluded", "reason": "runtime state"} for i in range(10)]
    failure = {"path": "degreeDatas[2].label", "kind": "incomplete", "reason": "field read failed"}
    notes.append(failure)
    write_snapshot(tmp_path, [entry(diagnostics=notes)])
    store = WikiStore(tmp_path)
    result = store.query("Steel")
    assert result["status"] == "partial" and result["resolution"] == "found"
    detail = result["entries"][0]
    assert detail["diagnostics"][0] == failure
    assert len(detail["diagnostics"]) == 4 and detail["diagnostics_truncated"]
    assert detail["truncated"]
    assert store.read_entry("Verse.ThingDef/Steel")["diagnostics"] == notes


def test_exclusion_diagnostics_are_not_collection_failures(tmp_path):
    notes = [{"path": f"field{i}", "kind": "excluded", "reason": "runtime state"} for i in range(10)]
    write_snapshot(tmp_path, [entry(diagnostics=notes)])
    result = WikiStore(tmp_path).query("Steel")
    assert result["status"] == "found"
    assert result["entries"][0]["diagnostics_truncated"]
    assert not result["entries"][0]["truncated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [257, 1024, 4096])
async def test_exact_keys_round_trip_through_tool_schema_and_handler(tmp_path, length):
    key = "Verse.ThingDef/" + "K" * (length - len("Verse.ThingDef/"))
    write_snapshot(tmp_path, [entry(key.split("/", 1)[1])])
    tool = make_wiki_tool(WikiStore(tmp_path))
    assert len(key) <= tool.parameters["properties"]["query"]["maxLength"]
    registry = ToolRegistry()
    registry.register(tool)
    first = await registry.execute(registry.prepare(ToolCall("w1", "wiki", json.dumps({"query": key}))))
    returned = first.value["data"]["entries"][0]["key"]
    assert returned == key
    second = await tool.handler({"query": returned})
    assert second["entries"][0]["key"] == key
    assert len(json.dumps(second, ensure_ascii=False).encode("utf-8")) <= 8192


@pytest.mark.parametrize("name", ["长" * 300, "K" * 500 + "  "])
def test_unicode_and_whitespace_keys_are_not_normalized(tmp_path, name):
    source = entry(name)
    write_snapshot(tmp_path, [source])
    store = WikiStore(tmp_path)
    result = store.query(source["key"])
    assert result["entries"][0]["key"] == source["key"]
    assert store.query(result["entries"][0]["key"])["match_count"] == 1


def test_unaddressable_key_is_explicit_and_never_clipped(tmp_path):
    source = entry("K" * 4097, related=[{"key": "K" * 4097, "label": "too long"}])
    write_snapshot(tmp_path, [source])
    result = WikiStore(tmp_path).query("steel")
    detail = result["entries"][0]
    assert result["status"] == "found" and detail["response_truncated"]
    assert detail["key"] is None and detail["key_unavailable"] == "exceeds_query_limit"
    assert detail["related"][0]["key"] is None
    assert detail["related"][0]["key_unavailable"] == "exceeds_query_limit"


def test_long_related_key_round_trips_without_clipping(tmp_path):
    target = entry("Target" + "K" * 1000, "target")
    source = entry(related=[{"key": target["key"], "label": "target"}])
    write_snapshot(tmp_path, [source, target])
    store = WikiStore(tmp_path)
    key = store.query("Steel")["entries"][0]["related"][0]["key"]
    assert key == target["key"]
    assert store.query(key)["entries"][0]["key"] == key


@pytest.mark.parametrize(
    "def_type,path,label,description,keyword",
    [
        ("RimWorld.TraitDef", "degreeDatas[0]", "Kind", "Helps others feel welcome", "welcome"),
        ("RimWorld.TraitDef", "degreeDatas[1]", "善良", "总会帮助其他殖民者", "帮助"),
        ("RimWorld.ThoughtDef", "stages[0]", "Ate fine meal", "Enjoyed delicious food", "delicious"),
        ("RimWorld.ThoughtDef", "stages[2]", "吃了精致餐食", "这顿饭非常美味", "美味"),
    ],
)
def test_collected_names_and_descriptions_are_searchable(tmp_path, def_type, path, label, description, keyword):
    # Search must see past the response's eight-fact limit.
    facts = [{"name": f"modCustom[{i}].arbitrary", "value": str(i)} for i in range(12)]
    label_fact = {"name": path + ".label", "value": label}
    description_fact = {"name": path + ".description", "value": description}
    facts.extend([label_fact, description_fact])
    source = entry("Unnamed", "", type=def_type, key=def_type + "/Unnamed", description="", facts=facts)
    write_snapshot(tmp_path, [source, entry("Distractor", "elsewhere", description=label)])
    store = WikiStore(tmp_path)
    result = store.query(label.swapcase())
    assert result["match_count"] == 1
    assert result["entries"][0]["key"] == source["key"]
    assert label_fact in result["entries"][0]["facts"]
    result = store.query(keyword)
    assert result["match_count"] == 1
    assert description_fact in result["entries"][0]["facts"]
    assert store.read_entry(source["key"]) == source


def test_root_description_keyword_tokens_and_arbitrary_facts(tmp_path):
    custom = {"name": "modField.notKnownToPlugin", "value": "opaque custom value"}
    write_snapshot(tmp_path, [entry(description="Improves work speed and comfort", facts=[custom])])
    result = WikiStore(tmp_path).query("comfort speed")
    assert result["status"] == "found"
    assert result["entries"][0]["facts"] == [custom]
    assert "keyword lookup" in wiki_tools.WIKI_GUIDANCE


@pytest.mark.parametrize("text", ["长" * 4096, "\x00" * 4096, "X" * 4096])
def test_diagnostics_warnings_and_keys_share_eight_kib_budget(tmp_path, text):
    diagnostics = [{"path": text, "kind": "excluded", "reason": text}] * 12
    diagnostics.append({"path": "failedField", "kind": "incomplete", "reason": text})
    entries = [
        entry(text[:4000] + str(i), "same", diagnostics=diagnostics, related=[{"key": text, "label": text}])
        for i in range(3)
    ]
    write_snapshot(
        tmp_path, entries, warnings=[text] * 20, issues=[text] * 20, mods=[{"package_id": text, "name": text}] * 20
    )
    result = WikiStore(tmp_path).query("same")
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 8192
    assert result["status"] == "partial" and result["resolution"] == "ambiguous"
    assert result["entries"]
    assert result["warnings_truncated"]
    original_keys = {e["key"] for e in entries}
    for detail in result["entries"]:
        assert detail["key"] in original_keys or (
            detail["key"] is None and detail["key_unavailable"] == "result_byte_limit"
        )
        assert detail["diagnostics"][0]["kind"] == "incomplete"
        assert detail["diagnostics_truncated"]


@pytest.mark.parametrize("fact_type", [None, 1, False, [], {}])
def test_optional_fact_type_must_be_string(tmp_path, fact_type):
    write_snapshot(tmp_path, [entry(facts=[{"name": "custom", "value": "75", "type": fact_type}])])
    assert WikiStore(tmp_path).query("Steel")["status"] == "unavailable"


def test_typed_facts_preserve_null_and_scalar_distinctions(tmp_path):
    facts = [
        {"name": "actualNull", "value": "null", "type": "null"},
        {"name": "stringNull", "value": "null", "type": "System.String"},
        {"name": "number", "value": "0", "type": "System.Int32"},
        {"name": "boolean", "value": "False", "type": "System.Boolean"},
        {"name": "legacy", "value": "old value"},
    ]
    write_snapshot(tmp_path, [entry(facts=facts)])
    result = WikiStore(tmp_path).query("Steel")
    assert result["status"] == "found"
    returned = {row["name"]: row for row in result["entries"][0]["facts"]}
    assert returned == {row["name"]: row for row in facts}
    assert result["entries"][0]["facts"][-1]["name"] == "actualNull"


def test_summary_prioritizes_valued_properties_over_nulls_and_metadata(tmp_path):
    facts = [
        {"name": "object.$type", "value": "Steel", "type": "System.String"},
        {"name": "cycle.$ref", "value": "Steel", "type": "System.String"},
        {"name": "nullField", "value": "null", "type": "null"},
        {"name": "legacyNull", "value": "null"},
        {"name": "empty", "value": "", "type": "System.String"},
    ] * 3
    valued = [{"name": f"customMod.property{i}", "value": str(i), "type": "System.Int32"} for i in range(8)]
    valued[0] = {"name": "literalNull", "value": "null", "type": "System.String"}
    valued[1] = {"name": "disabled", "value": "False", "type": "System.Boolean"}
    source = entry(facts=facts + valued)
    write_snapshot(tmp_path, [source])
    store = WikiStore(tmp_path)
    result = store.query("Steel")
    # A small record now retains every field, including null and metadata.
    returned = result["entries"][0]
    assert all(f in returned["facts"] for f in facts + valued)
    assert len(returned["facts"]) == len(facts + valued)
    assert result["status"] == "found" and returned["read_complete"]
    assert store.read_entry(source["key"]) == source


@pytest.mark.parametrize("type_text", ["长" * 4096, "\x00" * 4096, "Generic" * 4096])
def test_fact_type_is_bounded_and_truncation_is_explicit(tmp_path, type_text):
    facts = [{"name": "property", "value": "value", "type": type_text}] * 20
    write_snapshot(tmp_path, [entry(str(i), "same", facts=facts) for i in range(3)])
    store = WikiStore(tmp_path)
    result = store.query("same")
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 8192
    assert result["status"] == "ambiguous"
    assert all(e["candidate"] and e["facts"] == [] for e in result["entries"])
    detail = store.query("Verse.ThingDef/0 property")["entries"][0]
    assert detail["facts"] == []  # never shorten a type and call it a fact
    assert detail["not_read"] == ["property"]
    assert not detail["read_complete"] and detail["response_truncated"]
    assert store.read_entry("Verse.ThingDef/0")["facts"] == facts


def test_complete_source_stays_distinguishable_from_small_tool_summary(tmp_path):
    facts = [{"name": f"property{i}", "value": str(i), "type": "System.Int32"} for i in range(100)]
    write_snapshot(tmp_path, [entry(facts=facts)], complete=True)
    store = WikiStore(tmp_path)
    result = store.query("Steel")
    assert result["snapshot"]["complete"] is True
    assert result["entries"][0]["source_truncated"] is False
    assert result["entries"][0]["truncated"] is True
    assert store.read_entry("Verse.ThingDef/Steel")["facts"] == facts


def test_large_provenance_warnings_alone_never_lower_status(tmp_path):
    entries = [
        entry(str(i), "same", description="D" * 760, facts=[{"name": "field", "value": "V" * 380}], related=[])
        for i in range(3)
    ]
    write_snapshot(
        tmp_path,
        entries,
        mods=[{"package_id": "P" * 120, "name": "N" * 120}] * 10,
        warnings=["W" * 10000] * 100,
        provenance_complete=False,
    )
    result = WikiStore(tmp_path).query("same")
    assert result["status"] == "ambiguous"
    assert result["warnings_truncated"]
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 8192


def test_field_terms_do_not_outrank_explicit_object_name(tmp_path):
    facts = [
        {"name": "plant.growDays", "value": "3", "type": "System.Single"},
        {"name": "plant.harvestYield", "value": "6", "type": "System.Single"},
    ]
    write_snapshot(
        tmp_path,
        [
            entry("Plant_Rice", "水稻", type="Verse.ThingDef", key="Verse.ThingDef/Plant_Rice", facts=facts),
            entry(
                "PlantHarvestYield",
                "植物收获效率",
                type="RimWorld.StatDef",
                key="RimWorld.StatDef/PlantHarvestYield",
                facts=[],
            ),
            entry(
                "DrugHarvestYield",
                "成瘾品作物收获效率",
                type="RimWorld.StatDef",
                key="RimWorld.StatDef/DrugHarvestYield",
                facts=[],
            ),
        ],
    )
    result = WikiStore(tmp_path).query("水稻 growDays harvestYield")
    assert result["entries"][0]["key"] == "Verse.ThingDef/Plant_Rice"
    assert result["entries"][0]["match"] == "name_in_query"


def test_chinese_sentence_query_finds_the_labelled_entry(tmp_path):
    write_snapshot(tmp_path, [entry("Steel", "钢铁"), entry("SteelWall", "钢铁墙")])
    store = WikiStore(tmp_path)
    # Regression: `\w+` made the whole sentence one token, so no real question
    # could ever reach the collected Chinese label.
    result = store.query("钢铁的堆叠上限是多少")
    assert result["entries"][0]["def_name"] == "Steel"
    assert result["entries"][0]["label"] == "钢铁"


def test_a_latin_name_must_be_a_whole_query_word(tmp_path):
    write_snapshot(tmp_path, [entry("StatFactor", "stat factor"), entry("Kind", "kind")])
    store = WikiStore(tmp_path)
    assert store.query("Verse.ThingDef/Kind statFactors")["entries"][0]["def_name"] == "Kind"
    # Regression: `StatFactor` sits inside the longer word `statFactors`. Treating
    # that as the name used to outrank the intended definition.
    assert store.query("statFactors")["status"] == "not_found"


def test_field_names_list_reusable_valued_tokens(tmp_path):
    facts = [
        {"name": "gas", "value": "null", "type": "null"},
        {"name": "workAmount", "value": "1000", "type": "System.Int32"},
        {"name": "degreeDatas[0].statFactors[0].value", "value": "0.75", "type": "System.Single"},
    ]
    facts += [{"name": f"filler{index}", "value": "x", "type": "System.String"} for index in range(10)]
    write_snapshot(tmp_path, [entry("Steel", "钢铁", facts=facts)])
    store = WikiStore(tmp_path)
    detail = store.query("钢铁")["entries"][0]
    # Small records include their complete fields; no lossy token directory.
    assert detail["read_complete"] and detail["field_names"] == []
    assert all(fact in detail["facts"] for fact in facts)
    # A listed token is directly reusable: the documented follow-up workflow.
    follow = store.query("Verse.ThingDef/Steel workAmount")["entries"][0]
    assert {"name": "workAmount", "value": "1000", "type": "System.Int32"} in follow["facts"]


def test_a_question_with_no_collected_name_is_not_guessed(tmp_path):
    write_snapshot(tmp_path, [entry("Steel", "钢铁")])
    store = WikiStore(tmp_path)
    # Regression: description fragments used to fabricate an entry for any prose
    # sentence, which reads to the model as a confident definition answer.
    assert store.query("完全无关的一句话")["status"] == "not_found"
    assert store.query("今天天气怎么样")["status"] == "not_found"


def test_entries_report_how_they_matched(tmp_path):
    write_snapshot(tmp_path, [entry("Steel", "钢铁")])
    store = WikiStore(tmp_path)
    assert store.query("verse.thingdef/steel")["entries"][0]["match"] == "exact_key"
    assert store.query("钢铁")["entries"][0]["match"] == "exact_name"
    assert store.query("钢铁的堆叠上限是多少")["entries"][0]["match"] == "name_in_query"
