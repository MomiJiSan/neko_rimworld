import pytest

from plugin.plugins.neko_rimworld.agent.claims import check_claims
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft


def readings():
    return [
        {
            "response": {
                "payload": {
                    "status": "ok",
                    "data": {
                        "objects": [
                            {
                                "target": {"id": "Pawn1", "name": "Near"},
                                "facets": {
                                    "pawn": {
                                        "status": "observed",
                                        "conditions": [{"definition": "Gunshot"}],
                                        "conditions_scope": "visible native hediffs; not all hidden conditions",
                                    }
                                },
                            }
                        ]
                    },
                }
            }
        }
    ]


@pytest.mark.parametrize("text", ["技能最高且自身完全健康。", "他完全健康。", "完全健康。"])
def test_unbound_prose_is_not_a_mechanical_proof_of_missing_evidence(text):
    assert check_claims(text, observations=readings()) == []
    assert SubmissionDraft().apply({"answer": [{"text": text}]}, observations=readings())["agent_text"] == text


@pytest.mark.parametrize(
    "text", ["Near完全健康。", "Near是完全健康。", "| Near | 疼痛0.1 | 无伤情 |", "Doctor完全健康。"]
)
def test_explicit_subject_or_table_row_keeps_health_evidence_check(text):
    assert "health_completeness_claim" in [x["code"] for x in check_claims(text, observations=readings())]


def test_explicit_complete_condition_listing_licenses_health_claim():
    complete = readings()
    pawn = complete[0]["response"]["payload"]["data"]["objects"][0]["facets"]["pawn"]
    pawn["conditions_complete"] = True
    assert check_claims("Near完全健康。", observations=complete) == []
