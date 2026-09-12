import copy

import pytest

from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, SubmissionPatchError, count_handoff_tokens
from plugin.plugins.neko_rimworld.agent.presentation import EvidenceError, render_evidence
from plugin.plugins.neko_rimworld.reports import ReportError, ReportStore


def observed(identity="one", *, exact=None, lower=12, complete=False):
    return {
        "observation_id": identity,
        "response": {
            "connection_epoch": "connection",
            "colony_id": "colony",
            "load_epoch": "load",
            "game_tick": 10,
            "snapshot_sequence": 2,
            "payload": {
                "status": "ok" if complete else "partial",
                "issues": [],
                "data": {"reading": {"units": exact, "known": lower, "complete": complete}, "count": 7},
                "execution": {
                    "evidence": {
                        "version": "observed_values.v1",
                        "nodes": {
                            "": {"kind": "object", "name": "Result"},
                            "/reading": {
                                "kind": "reading",
                                "name": "reading",
                                "context": {"subject": "selected objects", "scope": "declared collection"},
                                "reading": {
                                    "value": "/reading/units",
                                    "lower": "/reading/known",
                                    "state": "/reading/complete",
                                    "unit": "seconds",
                                },
                            },
                            "/reading/units": {"kind": "scalar", "bound_to": "/reading"},
                            "/reading/known": {"kind": "scalar", "bound_to": "/reading"},
                            "/count": {"kind": "scalar", "native": "Result.recordCount"},
                        },
                    }
                },
            },
        },
    }


def reference(identity="one", paths=None):
    return {"observation_id": identity, "brief_paths": paths or ["/reading/units"]}


def test_unknown_reading_keeps_bound_subject_unit_and_lower_bound():
    obs = observed()
    full, brief, _ = render_evidence([reference()], [obs], "zh")
    assert "精确值未知，至少12" in brief
    assert "seconds" in brief and "selected objects" in brief and "declared collection" in brief
    assert "load" in full and r"snapshot\_sequence" in full
    assert count_handoff_tokens(brief)[0] < 180
    assert obs["response"]["payload"]["data"]["reading"]["units"] is None


def test_zero_remains_exact_and_unaliased_count_keeps_native_name():
    _, brief, _ = render_evidence(
        [reference(paths=["/reading", "/count"])], [observed(exact=0, lower=0, complete=True)], "en"
    )
    assert "0 seconds" in brief and "unknown" not in brief
    assert "Result.recordCount=7" in brief


@pytest.mark.parametrize("path", ["/missing", "/reading/0", "/~2/units", "/reading~3", "../secret"])
def test_references_only_locate_returned_data(path):
    with pytest.raises(EvidenceError):
        render_evidence([reference(paths=[path])], [observed()], "zh")


def test_repeated_paths_and_bound_status_are_one_displayed_fact():
    _, brief, _ = render_evidence([reference(paths=["/count", "/count"])], [observed()], "zh")
    assert brief.count("recordCount=7") == 1
    _, brief, _ = render_evidence([reference(paths=["/reading/units", "/reading/known"])], [observed()], "zh")
    assert brief.count("至少12") == 1
    _, brief, _ = render_evidence([reference(paths=["", "/count"])], [observed()], "zh")
    assert brief.count("recordCount=7") == 1


def test_wrong_run_failed_and_obsolete_load_cannot_be_cited():
    obs = observed()
    with pytest.raises(EvidenceError):
        render_evidence([reference("foreign")], [obs], "zh")
    failed = copy.deepcopy(obs)
    failed["response"]["payload"]["status"] = "failed"
    with pytest.raises(EvidenceError):
        render_evidence([reference()], [failed], "zh")
    later = observed("later")
    later["response"]["load_epoch"] = "new-load"
    with pytest.raises(EvidenceError):
        render_evidence([reference()], [obs, later], "zh")


def part(identity="one", paths=None):
    return {"observation_id": identity, "paths": paths or ["/reading/units"]}


def test_replacing_answer_keeps_analysis_and_facts_cohesive():
    one, two = observed(), observed("two", exact=30, lower=30, complete=True)
    draft = SubmissionDraft()
    draft.apply(
        {"answer": [{"text": "A conclusion."}, part()], "detail": "Supporting explanation."}, observations=[one, two]
    )
    with pytest.raises(SubmissionPatchError) as caught:
        draft.apply({"answer": [part("two")]}, observations=[one, two])
    assert caught.value.details["required_fields"] == ["detail"]
    assert "A conclusion" not in draft.full_text and "Supporting explanation" not in draft.full_text
    result = draft.apply({"detail": None}, observations=[one, two])
    assert "30" in result["full_text"] and "30" in result["agent_text"]
    assert "至少12" not in result["full_text"]
    assert result["provenance"]["sources"][0]["observation_id"] == "two"


def test_over_budget_preserves_answer_and_reports_available_text_space():
    draft = SubmissionDraft()
    with pytest.raises(SubmissionPatchError) as caught:
        draft.apply({"answer": [{"text": "长结论。" * 150}, part()]}, observations=[observed()])
    assert caught.value.code == "handoff_over_budget"
    assert draft.answer[-1] == part()
    assert caught.value.details["fact_tokens"] > 0
    assert caught.value.details["text_tokens_available"] < 180
    result = draft.apply({"answer": [part()]}, observations=[observed()])
    assert "至少12" in result["agent_text"]


def test_report_size_prefers_referenced_observations_and_refuses_lost_proof(monkeypatch):
    from plugin.plugins.neko_rimworld import reports

    monkeypatch.setattr(reports, "MAX_REPORT_BYTES", 2000)
    store = ReportStore()
    record = {
        "report_id": "a" * 32,
        "state": "complete",
        "full_text": "Facts",
        "provenance": {"sources": [{"observation_id": "kept"}]},
        "observations": [
            {"observation_id": "kept", "response": {"game_tick": 1}},
            {"observation_id": "other", "payload": "x" * 5000},
        ],
    }
    result = store.read(store.save(record)["report_id"])
    assert result["observations"] == record["observations"][:1]
    assert result["omitted_observations"] == ["other"]
    record["report_id"] = "b" * 32
    record["observations"][0]["large"] = "x" * 5000
    with pytest.raises(ReportError):
        store.save(record)


def test_empty_collections_keep_distinct_context_and_paging():
    obs = observed()
    payload = obs["response"]["payload"]
    payload["data"] = {"a": [], "b": []}
    payload["execution"]["evidence"]["nodes"] = {
        "/a": {
            "kind": "collection",
            "native": "Page.nodes",
            "context": {"map": "first", "window": {"offset": 12, "total": 12}},
        },
        "/b": {
            "kind": "collection",
            "native": "Page.nodes",
            "context": {"map": "second", "window": {"offset": 0, "total": 0}},
        },
    }
    full, brief, _ = render_evidence([reference(paths=["/a", "/b"])], [obs], "en")
    for text in (full, brief):
        assert "first" in text and "second" in text and '"offset":12' in text


def test_labels_are_literal_markdown_data():
    from plugin.plugins.neko_rimworld.agent.presentation import _text

    escaped = _text("![image](https://example.invalid/x) **claim** <script>")
    assert "![image]" not in escaped and "**claim**" not in escaped and "<script>" not in escaped


def test_pending_judgments_do_not_enter_degraded_projection():
    a, b = observed(), observed("two", exact=0, lower=0, complete=True)
    draft = SubmissionDraft()
    draft.apply({"answer": [part()], "detail": "There is enough; use the surplus."}, observations=[a, b])
    with pytest.raises(SubmissionPatchError):
        draft.apply({"answer": [part("two")]}, observations=[a, b])
    assert draft.detail == "There is enough; use the surplus."
    assert "enough" not in (draft.full_text or "") and "surplus" not in (draft.full_text or "")
    assert "enough" not in (draft.agent_text or "")


def test_report_roundtrip_preserves_long_distinct_descriptor_paths():
    prefix = "/" + "a" * 130
    paths = [prefix + "/one", prefix + "/two"]
    observation = observed()
    observation["response"]["payload"]["execution"]["evidence"]["nodes"] = {
        p: {"kind": "scalar", "native": "Declared.value"} for p in paths
    }
    store = ReportStore()
    record = {
        "report_id": "c" * 32,
        "state": "complete",
        "full_text": "Facts",
        "observations": [observation],
        "provenance": {"sources": [{"observation_id": "one", "paths": paths}]},
    }
    saved = store.read(store.save(record)["report_id"])
    assert set(saved["observations"][0]["response"]["payload"]["execution"]["evidence"]["nodes"]) == set(paths)
    assert saved["evidence_status"] == "retained"


def test_equivalent_parent_value_and_separated_reference_parts_are_one_fact():
    draft = SubmissionDraft()
    result = draft.apply(
        {
            "answer": [
                part(paths=["reading", "/reading/units"]),
                {"text": "This is an interpretation."},
                part(paths=["/reading/known"]),
            ]
        },
        observations=[observed()],
    )
    assert result["agent_text"].count("至少12") == 1
    assert result["agent_text"].endswith("This is an interpretation.")


def test_compact_observation_ids_preserve_full_envelope_identity():
    from plugin.plugins.neko_rimworld.observation import Observation

    from .test_catalog_tools import response

    frame = response()
    observation = Observation.from_response("graphql", {}, frame).to_dict()
    assert len(observation["observation_id"]) == 12
    assert observation["response"] == frame.to_dict()


def test_quantity_and_record_count_are_distinct_readings_despite_path_ancestry():
    obs = observed(exact=200, lower=200, complete=True)
    obs["response"]["payload"]["data"]["reading"]["item_count"] = 2
    obs["response"]["payload"]["execution"]["evidence"]["nodes"]["/reading/item_count"] = {
        "kind": "reading",
        "context": {"subject": "selected objects", "scope": "declared collection"},
        "reading": {"value": "/reading/item_count", "state": "/reading/complete", "unit": "item_records"},
    }
    _, brief, _ = render_evidence([reference(paths=["/reading/units", "/reading/item_count"])], [obs], "en")
    assert "200 seconds" in brief and r"2 item\_records" in brief
