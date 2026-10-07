import pytest

from plugin.plugins.neko_rimworld.agent.handoff import HANDOFF_TOKEN_BUDGET, SubmissionDraft, SubmissionPatchError

from .test_presentation import observed


def test_text_revision_keeps_saved_detail_without_reconfirmation():
    draft = SubmissionDraft()
    draft.apply({"answer": [{"text": "结论。"}], "detail": "完整报告与条件。"})
    updated = draft.apply({"answer": [{"text": "补充条件后的结论。"}]})
    assert updated["agent_text"] == "补充条件后的结论。"
    assert draft.detail == "完整报告与条件。"
    assert draft.snapshot()["complete"]
    assert HANDOFF_TOKEN_BUDGET == 180


def test_changed_explicit_reference_still_requires_matching_detail():
    readings = [observed("one"), observed("two")]
    draft = SubmissionDraft()
    draft.apply(
        {"answer": [{"observation_id": "one", "paths": ["/count"]}], "detail": "第一份观察的说明。"},
        observations=readings,
    )
    with pytest.raises(SubmissionPatchError) as caught:
        draft.apply({"answer": [{"observation_id": "two", "paths": ["/count"]}]}, observations=readings)
    assert caught.value.code == "submission_requires_confirmation"
