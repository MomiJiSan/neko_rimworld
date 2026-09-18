"""提交时断言检查的端到端接线。

bench 的 `tests/test_claims.py::test_submission_path_is_wired_to_the_claim_check`
用源码断言守接线，并在注释里指向本文件做端到端覆盖。此前**本文件不存在**，
所以"接线"只有字符串断言、没有行为断言。这里补上：

  1. 拒绝的提交不会留下可交付文本（`SubmissionDraft` 里 full_text/agent_text 不被写）。
  2. 降级路径不会把被拒的句子交出去（`AgentRunSession.fallback_answer`）。
  3. 检查失败后模型修正措辞可以成功提交。
  4. `claims_supported=False` 可以关掉（对照臂需要），且只在这个显式开关下生效。
"""
from __future__ import annotations

import json

import pytest

from plugin.plugins.neko_rimworld.agent.handoff import (
    SubmissionDraft,
    SubmissionPatchError,
    make_submission_tool,
)
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession


def observation_without_identity():
    """一次真实形状的读取：没有任何身份/派系字段。"""
    return {
        "observation_id": "obs-1",
        "response": {
            "connection_epoch": "connection",
            "colony_id": "colony",
            "load_epoch": "load",
            "payload": {"status": "ok", "data": {"rows": [{"id": "Pawn1", "label": "Stin"}]}},
        },
    }


def test_refused_submission_does_not_commit_text():
    draft = SubmissionDraft()
    with pytest.raises(SubmissionPatchError) as caught:
        draft.apply({"answer": [{"text": "他是我们的殖民者。"}]},
                    observations=[observation_without_identity()])
    assert caught.value.code == "identity_claim"
    assert draft.claims_blocked is True
    # 关键：被拒的句子没有落进任何可交付字段。
    assert draft.full_text is None
    assert draft.agent_text is None
    assert draft.snapshot()["complete"] is False


def test_resubmitting_a_supported_claim_succeeds_and_lifts_the_block():
    draft = SubmissionDraft()
    with pytest.raises(SubmissionPatchError):
        draft.apply({"answer": [{"text": "他是我们的殖民者。"}]},
                    observations=[observation_without_identity()])
    result = draft.apply({"answer": [{"text": "本次读取没有取得该对象的派系字段。"}]},
                         observations=[observation_without_identity()])
    assert draft.claims_blocked is False
    assert "派系字段" in result["agent_text"]
    assert draft.snapshot()["complete"] is True


def test_fallback_never_re_emits_a_refused_sentence():
    """实测缺陷：检查原本跑在文本赋值之后，被拒的句子会经降级路径原样交付。"""
    session = AgentRunSession.create(ledger=_ledger(), system_prompt="s", question="q")
    session.observations.append(observation_without_identity())
    with pytest.raises(SubmissionPatchError):
        session.submission.apply({"answer": [{"text": "他是我们的殖民者。"}]},
                                 observations=session.observations)
    answer = session.fallback_answer
    assert "殖民者" not in answer
    assert "refused for unsupported claims" in answer


def test_claims_can_be_switched_off_for_a_control_arm():
    draft = SubmissionDraft()
    result = draft.apply({"answer": [{"text": "他是我们的殖民者。"}]},
                         observations=[observation_without_identity()],
                         claims_supported=False)
    assert "殖民者" in result["agent_text"]


@pytest.mark.asyncio
async def test_submission_tool_surfaces_the_rejection_to_the_model():
    from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError

    draft = SubmissionDraft()
    tool = make_submission_tool(draft, [observation_without_identity()])
    with pytest.raises(ToolExecutionError) as caught:
        await tool.handler({"answer": [{"text": "他是我们的殖民者。"}]})
    assert caught.value.code == "identity_claim"
    assert caught.value.details["required_fields"] == ["answer"]
    assert caught.value.details["rejected_claims"][0]["quote"] == "殖民者"


def test_a_clean_text_submission_still_goes_through_unchanged():
    draft = SubmissionDraft()
    result = draft.apply({"answer": [{"text": "地面范围为 200 单位。"}]},
                         observations=[observation_without_identity()])
    assert result["agent_text"] == "地面范围为 200 单位。"


def _ledger():
    from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger

    return AgentAuditLedger("test question")
