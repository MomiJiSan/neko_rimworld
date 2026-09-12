from __future__ import annotations

import pytest

from plugin.plugins.neko_rimworld.agent.handoff import (
    HANDOFF_TOKEN_BUDGET,
    HandoffValidationError,
    attach_agent_handoff,
    count_handoff_tokens,
    validate_submission,
)


def test_submission_preserves_both_agent_authored_texts() -> None:
    full_text = "完整报告。\n包含模型认为与问题相关的全部细节。"
    agent_text = "殖民地目前稳定；详细报告已由插件保存。"

    submission = validate_submission(full_text, agent_text)

    assert submission["full_text"] == full_text
    assert submission["agent_text"] == agent_text
    assert submission["delivery"]["format"] == "natural_text"
    assert submission["delivery"]["handoff_tokens"] <= HANDOFF_TOKEN_BUDGET


def test_over_budget_agent_text_is_rejected_without_truncation() -> None:
    agent_text = "这是由内部Agent自己撰写的自然语言结果。" * 100

    with pytest.raises(HandoffValidationError) as caught:
        validate_submission("完整回答", agent_text)

    assert caught.value.code == "handoff_over_budget"
    assert agent_text.endswith("结果。")


def test_outer_agent_projection_contains_only_agent_text() -> None:
    from utils.result_parser import parse_plugin_result

    agent_text = "殖民地目前安全，没有发现需要立即处理的问题。"
    result = attach_agent_handoff(
        {
            "status": "ok",
            "answer": "这里是面板中保留的完整详细报告。",
            "agent_text": agent_text,
            "submission_mode": "submit_response",
            "game_evidence": {"private": "not projected"},
            "audit": {"run_id": "private-run-id"},
        }
    )

    projected = parse_plugin_result(result, llm_result_fields=["summary"], plugin_message="done")
    assert projected == agent_text
    assert "private" not in projected
    assert "run-id" not in projected
    assert "RWA" not in projected
    assert result["answer"] == "这里是面板中保留的完整详细报告。"
    assert result["delivery"]["submission_mode"] == "submit_response"


def test_short_direct_model_text_remains_a_compatible_fallback() -> None:
    answer = "现在有3名殖民者。"
    result = attach_agent_handoff({"status": "ok", "answer": answer})

    assert result["summary"] == answer
    assert result["reply"] == answer
    assert result["delivery"]["submission_mode"] == "text_fallback"


def test_missing_valid_handoff_returns_only_an_operational_failure() -> None:
    long_answer = "完整模型回答。" * 500
    result = attach_agent_handoff({"status": "degraded", "answer": long_answer})

    assert result["answer"] == long_answer
    assert "callback-safe response" in result["summary"]
    assert "RWA" not in result["summary"]
    assert result["delivery"]["submission_mode"] == "operational_fallback"
    assert result["delivery"]["host_truncation_expected"] is False


def test_counter_reports_a_safe_budget_metric() -> None:
    tokens, tokenizer = count_handoff_tokens("殖民地 stable 123")

    assert tokens > 0
    assert tokenizer in {"o200k_base", "utf8_bytes_upper_bound"}


@pytest.mark.asyncio
@pytest.mark.parametrize("references_supported", [False, True])
async def test_text_length_repair_preserves_report_and_requires_reconfirmation(references_supported):
    from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, make_submission_tool
    from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError

    draft = SubmissionDraft()
    tool = make_submission_tool(draft, [], references_supported=references_supported)
    detail = "地面钢铁200单位；背包未能读取，联合精确总量未知。"
    long_answer = [{"text": "地面有钢铁，背包数量尚不确定。" * 100}]
    with pytest.raises(ToolExecutionError) as rejected:
        await tool.handler({"answer": long_answer, "detail": detail})
    assert rejected.value.code == "handoff_over_budget"
    assert "Reference parts" not in str(rejected.value)
    assert "resubmit matching saved detail" in str(rejected.value)
    assert draft.detail == detail and draft.answer == long_answer
    assert detail in draft.full_text and draft.agent_text is None

    answer = [{"text": "地面钢铁200单位；背包未知，精确合计未知。"}]
    with pytest.raises(ToolExecutionError) as unconfirmed:
        await tool.handler({"answer": answer})
    assert unconfirmed.value.code == "submission_requires_confirmation"
    result = await tool.handler({"detail": detail})
    assert draft.snapshot()["complete"] is True
    assert result["full_text"] == answer[0]["text"] + "\n\n" + detail
    assert result["agent_text"] == answer[0]["text"]


@pytest.mark.asyncio
async def test_text_tool_declares_only_text_parts_and_rejects_references():
    import json

    from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, make_submission_tool
    from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError

    tool = make_submission_tool(SubmissionDraft(), [], references_supported=False)
    schema = tool.schema()
    assert "reference" not in json.dumps(schema).lower()
    fields = schema["function"]["parameters"]["properties"]
    assert "180 tokens" in fields["answer"]["description"]
    assert "Complete factual report" in fields["detail"]["description"]
    with pytest.raises(ToolExecutionError) as rejected:
        await tool.handler({"answer": [{"observation_id": "missing"}]})
    assert rejected.value.code == "unsupported_reference"

    compatible = make_submission_tool(SubmissionDraft(), [], references_supported=True)
    assert "observation_id" in json.dumps(compatible.schema())
