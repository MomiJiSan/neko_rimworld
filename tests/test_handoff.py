from __future__ import annotations

import pytest

from plugin.plugins.neko_rimworld.agent.handoff import (
    HANDOFF_FACTS_TOKEN_BUDGET,
    HANDOFF_TOKEN_BUDGET,
    HandoffValidationError,
    attach_agent_handoff,
    count_handoff_tokens,
    validate_agent_text,
    validate_submission,
    rendered_authored_text,
)


def test_tool_parameter_tail_is_rendered_without_mutating_original_text():
    raw='合理分工。 </answer>\n<parameter name="language">zh'
    assert rendered_authored_text(raw,'zh')=='合理分工。'
    assert raw.endswith('>zh')
    quoted='角色名字是“</answer><parameter name="language">zh”。'
    assert rendered_authored_text(quoted,'zh')==quoted
    assert rendered_authored_text(raw,'en')==raw


def test_submission_preserves_both_agent_authored_texts() -> None:
    full_text = "完整报告。\n包含模型认为与问题相关的全部细节。"
    agent_text = "殖民地目前稳定；详细报告已由插件保存。"

    submission = validate_submission(full_text, agent_text)

    assert submission["full_text"] == full_text
    assert submission["agent_text"] == agent_text
    assert submission["delivery"]["format"] == "natural_text"
    assert submission["delivery"]["handoff_tokens"] <= HANDOFF_TOKEN_BUDGET


def test_over_budget_agent_text_is_rejected_without_truncation() -> None:
    """模型写的字超过 180 就被拒，且**不截断**。

    模型文字与渲染事实分别记账，但合计仍受现有宿主 200-token 传输上限约束。
    不能通过声明事实份额来绕过传输预算，也不能在最后一跳静默截断。
    """
    agent_text = "这是由内部Agent自己撰写的自然语言结果。" * 100

    with pytest.raises(HandoffValidationError) as caught:
        validate_agent_text(agent_text, fact_tokens=0)

    assert caught.value.code == "handoff_over_budget"
    assert agent_text.endswith("结果。")
    # fact_tokens 是**显式**传入的，不从缺失推断：不传就等于“整份都是模型写的”。
    # 早期版本让最后一跳在缺少事实份额时放宽天花板，结果分不清“模型写多了”和
    # “事实很大”，静默放过了超长的模型文本（bench 的 staged 交互用例就是因此变红的）。
    with pytest.raises(HandoffValidationError) as strict:
        validate_submission("完整回答", agent_text)
    assert strict.value.code == "handoff_over_budget"
    # 声明事实份额能消除模型文字超额，但不能让宿主接收 1300-token 的摘要。
    facts = count_handoff_tokens(agent_text)[0] - HANDOFF_TOKEN_BUDGET + 10
    with pytest.raises(HandoffValidationError) as transport:
        validate_submission("完整回答", agent_text, fact_tokens=facts)
    assert transport.value.code == "handoff_transport_over_budget"


def test_model_text_is_not_charged_for_rendered_facts() -> None:
    """v1.3 的 7/7：事实与模型文本记在同一条 180 额度上。"""
    model_text = "结论：" + "判断。" * 60
    assert count_handoff_tokens(model_text)[0] <= HANDOFF_TOKEN_BUDGET
    facts = HANDOFF_FACTS_TOKEN_BUDGET - 10
    assert validate_agent_text(model_text, fact_tokens=facts) == model_text
    with pytest.raises(HandoffValidationError) as caught:
        validate_agent_text(model_text, fact_tokens=HANDOFF_FACTS_TOKEN_BUDGET + 1)
    assert caught.value.code == "facts_over_budget"


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
async def test_text_length_repair_preserves_report_without_retransmission(references_supported):
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
    assert "Omitted detail remains saved" in str(rejected.value)
    assert draft.detail == detail and draft.answer == long_answer
    assert detail in draft.full_text and draft.agent_text is None

    answer = [{"text": "地面钢铁200单位；背包未知，精确合计未知。"}]
    result = await tool.handler({"answer": answer})
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


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["language", "detail", "unexpected"])
async def test_text_mode_bad_parts_use_current_contract_in_real_registry(extra):
    import json

    from plugin.plugins.neko_rimworld.agent.contracts import ToolCall
    from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, make_submission_tool
    from plugin.plugins.neko_rimworld.agent.tools import ToolRegistry

    registry = ToolRegistry()
    registry.register(make_submission_tool(SubmissionDraft(), [], references_supported=False))
    prepared = registry.prepare(
        ToolCall(
            call_id="bad-part",
            name="submit_response",
            raw_arguments=json.dumps({"answer": [{"text": "结论", extra: "zh"}]}),
        )
    )
    result = await registry.execute(prepared)
    error = result.value["error"]
    assert result.ok is False and error["code"] == "invalid_submission"
    assert "answer[0]" in error["message"] and extra in error["message"]
    assert "observation_id" not in error["message"]
    assert "fact_tokens" not in error["details"]
    assert "text_tokens_available" not in error["details"]
    if extra in {"language", "detail"}:
        assert "top level" in error["message"]


def test_text_fallback_applies_same_capability_and_token_feedback():
    from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, SubmissionPatchError

    draft = SubmissionDraft()
    with pytest.raises(SubmissionPatchError) as rejected:
        draft.apply({"answer": [{"text": "长结论。" * 200}], "detail": "保留完整报告"}, references_supported=False)
    details = rejected.value.details
    assert details["handoff_tokens"] > details["token_budget"] == HANDOFF_TOKEN_BUDGET
    assert "fact_tokens" not in details
    with pytest.raises(SubmissionPatchError) as rejected:
        draft.apply({"answer": [{"text": "结论", "language": "zh"}]}, references_supported=False)
    assert "observation_id" not in str(rejected.value)
    assert draft.detail == "保留完整报告"


@pytest.mark.asyncio
async def test_text_submission_string_and_legacy_shapes_share_validation():
    from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, make_submission_tool
    from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError

    conclusion = "可达，150步；终点是可触及位置，并非站入目标格。"
    outputs = []
    for value in (conclusion, {"text": conclusion}, [{"text": conclusion}]):
        draft = SubmissionDraft()
        tool = make_submission_tool(draft, [], references_supported=False)
        assert tool.parameters["properties"]["answer"]["type"] == "string"
        await tool.handler({"answer": value, "detail": "完整路径及范围。"})
        outputs.append((draft.agent_text, draft.full_text))
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0][0] == conclusion
    with pytest.raises(ToolExecutionError) as error:
        await make_submission_tool(SubmissionDraft(), [], references_supported=False).handler(
            {"answer": "这是很长的结论。" * 300}
        )
    assert error.value.code == "handoff_over_budget"


def test_handoff_guidance_separates_main_conclusion_and_complete_report():
    from plugin.plugins.neko_rimworld.agent.prompt import build_finalization_prompt, build_system_prompt

    for prompt in (
        build_system_prompt(4, references_supported=False),
        build_finalization_prompt(4, references_supported=False),
    ):
        assert "cover the question" in prompt and "standalone conclusion" in prompt
        assert "one concise text string" in prompt
        assert "180 tokens" in prompt and "12000 characters" in prompt


@pytest.mark.asyncio
async def test_invalid_text_submission_feedback_matches_public_string_schema():
    from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, make_submission_tool
    from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError

    tool = make_submission_tool(SubmissionDraft(), [], references_supported=False)
    with pytest.raises(ToolExecutionError) as error:
        await tool.handler({"answer": 42})
    assert "nonempty text string" in str(error.value)
    assert "ordered parts" not in str(error.value)


@pytest.mark.asyncio
async def test_legacy_single_object_does_not_drop_extra_fields():
    from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, make_submission_tool
    from plugin.plugins.neko_rimworld.agent.tools import ToolExecutionError

    with pytest.raises(ToolExecutionError):
        await make_submission_tool(SubmissionDraft(), [], references_supported=False).handler(
            {"answer": {"text": "结论", "language": "zh"}}
        )
