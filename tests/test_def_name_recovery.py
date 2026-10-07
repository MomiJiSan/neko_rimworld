"""Deterministic Def-name recovery: narrow lexical mechanism, native stays the arbiter.

Ports the controls verified by rimworld-bench experiments/feasibility_20261001
(catalog token normalization, collision refusal, incomplete refusal) onto the
production resolver, and pins the intervention surface: opt-in flag, untouched
exact-known terms, names-only discovery, and honest disclosure on the result.
"""

import copy
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent.def_names import (
    DefNameResolver,
    tokens,
)
from plugin.plugins.neko_rimworld.agent.semantic_tools import SemanticInteraction


class RecordingSurface:
    def __init__(self):
        self.argument_schemas = {"thing_defs": {"properties": {"exact_names": {}}}}

    def compile(self, plan):
        return copy.deepcopy(plan)


class FakeView:
    """Indexed-view shape: casefolded published names, case-retaining keys."""

    def __init__(self, rows, *, complete=True, issues=()):
        self._rows = rows
        self.metadata = {
            "complete": complete,
            "issues": list(issues),
            "sha256": "ab" * 32,
            "generated_at": "2026-10-01T00:00:00Z",
        }

    def search_rows(self):
        return self._rows


CATALOG = [
    {"key": "Verse.ThingDef/ComponentIndustrial", "names": ["componentindustrial", "零部件"], "description": ""},
    {"key": "Verse.ThingDef/AdvancedComponent", "names": ["advancedcomponent", "高级零部件"], "description": ""},
    {"key": "Verse.ThingDef/Silver", "names": ["silver", "白银"], "description": ""},
    {"key": "Verse.StatDef/MedicalSurgerySpeed", "names": ["medicalsurgeryspeed"], "description": ""},
]


def resolver(rows=CATALOG, **kwargs):
    return DefNameResolver.from_knowledge_view(FakeView(rows, **kwargs))


def test_tokens_are_word_order_and_case_insensitive_but_narrow():
    assert tokens("IndustrialComponent") == tokens("ComponentIndustrial") == ("component", "industrial")
    assert tokens("component_industrial") == ("component", "industrial")
    # CJK has no camel humps; the run stays one token and never fuses with labels.
    assert tokens("工业零部件") == ("工业零部件",)
    assert tokens("零部件") != tokens("工业零部件")




def test_word_order_recovery_is_unique_and_canonical_names_roundtrip():
    r = resolver()
    advice = r.normalize("IndustrialComponent")
    assert advice["state"] == "unique" and advice["auto_selection"] == "ComponentIndustrial"
    for row in CATALOG:
        if row["key"].startswith("Verse.ThingDef/"):
            canonical = row["key"].split("/", 1)[1]
            assert r.normalize(canonical)["state"] == "known"


def test_exact_known_labels_are_left_to_the_native_lookup():
    # A published label resolves natively; rewriting it would touch a working path.
    assert resolver().normalize("零部件")["state"] == "known"


def test_injected_collision_refuses_auto_selection():
    rows = CATALOG + [
        {"key": "Verse.ThingDef/Industrial_Component", "names": ["industrial_component"], "description": ""}
    ]
    advice = resolver(rows).normalize("IndustrialComponent")
    assert advice["state"] == "ambiguous"
    assert advice["auto_selection"] is None
    assert advice["candidates"] == ["ComponentIndustrial", "Industrial_Component"]


def test_incomplete_catalog_refuses_auto_selection():
    assert resolver(complete=False).normalize("IndustrialComponent")["state"] == "incomplete"
    assert resolver(issues=["snapshot truncated"]).normalize("IndustrialComponent")["state"] == "incomplete"


def test_non_thingdef_rows_never_enter_the_catalog():
    # A StatDef sharing the tokens must not become a ThingDef candidate.
    rows = CATALOG + [{"key": "Verse.StatDef/ComponentIndustrial", "names": ["componentindustrial"], "description": ""}]
    assert resolver(rows).normalize("IndustrialComponent")["auto_selection"] == "ComponentIndustrial"


def test_discovery_is_names_only_and_covers_localized_containment():
    d = resolver().discovery("工业零部件")
    assert d is not None
    assert [m["definition"] for m in d["matches"]] == ["ComponentIndustrial"]
    assert d["definition_kind"] == "ThingDef" and d["truncated"] is False
    assert "names only, not computed values or automatic replacements" in d["meaning"]
    assert d["source"]["kind"] == "def_knowledge_index"
    # Word-order miss also surfaces its token candidates.
    assert [m["definition"] for m in resolver().discovery("IndustrialComponent")["matches"]] == ["ComponentIndustrial"]


def test_discovery_caps_and_reports_truncation():
    rows = [
        {"key": f"Verse.ThingDef/Part{i}", "names": [f"part{i}", f"共用零部件{i}"], "description": ""}
        for i in range(12)
    ]
    d = resolver(rows).discovery("零部件")
    assert d["truncated"] is True and len(d["matches"]) == 8


def test_discovery_without_any_lexical_hit_is_none():
    assert resolver().discovery("NonexistentThing_Audit") is None


def native_payload(rows, exact_names):
    return {
        "status": "ok",
        "observation_id": "obs-1",
        "data": {
            "results": [
                {
                    "from": "thing_defs",
                    "rows": rows,
                    "scope": {"args": {"exact_names": exact_names}},
                    "state": "complete",
                    "fields": "complete",
                    "listing": "complete",
                    "matched": len(rows),
                    "known_matches": len(rows),
                }
            ]
        },
    }


def interaction(view, responses):
    calls = []

    async def execute_native(request, *, tool_name="query"):
        calls.append(copy.deepcopy(request))
        return responses[len(calls) - 1]

    stub = SemanticInteraction.__new__(SemanticInteraction)
    stub.setup = SimpleNamespace(knowledge_view=view)
    stub.session = SimpleNamespace(observations=[])
    stub.surface = RecordingSurface()
    stub._def_name_resolver = None
    stub.execute_native = execute_native
    return stub, calls


RESOLVED_ROW = {"def_name": "ComponentIndustrial", "label": "零部件"}




@pytest.mark.asyncio
async def test_unique_recovery_rewrites_once_and_discloses(monkeypatch):
    monkeypatch.setenv("NEKO_DEF_NAME_RECOVERY", "1")
    stub, calls = interaction(FakeView(CATALOG), [native_payload([RESOLVED_ROW], ["ComponentIndustrial"])])
    obj, resolution, _ = await SemanticInteraction._resolve(
        stub, {"kind": "things", "definition": "IndustrialComponent"}
    )
    assert calls[0]["blocks"][0]["args"] == {"exact_names": ["ComponentIndustrial"]}
    assert len(calls) == 1  # recovery rides the same production call
    assert resolution["status"] == "resolved"
    assert obj["definition"] == "ComponentIndustrial"
    assert resolution["name_resolution"]["requested"] == "IndustrialComponent"
    assert resolution["name_resolution"]["resolved_as"] == "ComponentIndustrial"
    assert resolution["name_resolution"]["method"] == "token_normalization"
    assert resolution["name_resolution"]["catalog"]["sha256"] == "ab" * 32
    assert resolution["name_resolution"]["catalog"]["complete"] is True


@pytest.mark.asyncio
async def test_stale_index_degrades_to_honest_missing_with_candidates(monkeypatch):
    monkeypatch.setenv("NEKO_DEF_NAME_RECOVERY", "1")
    # Native is the arbiter: the rewritten canonical name may itself be missing.
    stub, calls = interaction(FakeView(CATALOG), [native_payload([], ["ComponentIndustrial"])])
    obj, resolution, _ = await SemanticInteraction._resolve(
        stub, {"kind": "thing_def", "definition": "IndustrialComponent"}
    )
    assert obj is None and resolution["status"] == "missing_in_scope"
    assert resolution["name_resolution"]["resolved_as"] == "ComponentIndustrial"
    assert [m["definition"] for m in resolution["discovery"]["matches"]] == ["ComponentIndustrial"]


@pytest.mark.asyncio
async def test_localized_missing_gets_discovery_without_auto_selection(monkeypatch):
    monkeypatch.setenv("NEKO_DEF_NAME_RECOVERY", "1")
    stub, calls = interaction(FakeView(CATALOG), [native_payload([], ["工业零部件"])])
    obj, resolution, _ = await SemanticInteraction._resolve(stub, {"kind": "things", "definition": "工业零部件"})
    assert obj is None and resolution["status"] == "missing_in_scope"
    assert calls[0]["blocks"][0]["args"] == {"exact_names": ["工业零部件"]}  # never rewritten
    assert "name_resolution" not in resolution
    assert [m["definition"] for m in resolution["discovery"]["matches"]] == ["ComponentIndustrial"]


@pytest.mark.asyncio
async def test_broken_view_degrades_to_unchanged_lookups(monkeypatch):
    monkeypatch.setenv("NEKO_DEF_NAME_RECOVERY", "1")

    class Broken:
        metadata = {"complete": True, "issues": []}

        def search_rows(self):
            raise ValueError("knowledge payload changed")

    stub, calls = interaction(Broken(), [native_payload([], ["IndustrialComponent"])])
    obj, resolution, _ = await SemanticInteraction._resolve(
        stub, {"kind": "things", "definition": "IndustrialComponent"}
    )
    assert obj is None and resolution["status"] == "missing_in_scope"
    assert calls[0]["blocks"][0]["args"] == {"exact_names": ["IndustrialComponent"]}
    assert "discovery" not in resolution


@pytest.mark.asyncio
async def test_identity_name_lookups_are_out_of_scope(monkeypatch):
    monkeypatch.setenv("NEKO_DEF_NAME_RECOVERY", "1")
    stub, calls = interaction(
        FakeView(CATALOG),
        [
            {
                "status": "ok",
                "observation_id": "o",
                "data": {
                    "results": [
                        {
                            "from": "things",
                            "rows": [],
                            "scope": {},
                            "state": "complete",
                            "fields": "complete",
                            "listing": "complete",
                            "matched": 0,
                            "known_matches": 0,
                        }
                    ]
                },
            }
        ],
    )
    await SemanticInteraction._resolve(stub, {"kind": "things", "names": ["IndustrialComponent"], "map": "current"})
    assert calls[0]["blocks"][0]["where"] == {"label": {"in": ["IndustrialComponent"]}}
