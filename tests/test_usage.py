from __future__ import annotations

from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger, AgentAuditRegistry, AgentRunState
from plugin.plugins.neko_rimworld.agent.model_client import OpenAICompatibleModelClient, build_agent_model_settings
from plugin.plugins.neko_rimworld.agent.usage import summarize_usage


def test_missing_usage_is_unknown_not_zero_and_partial_counts_are_subtotals():
    empty = summarize_usage(1, [{}])
    assert empty["total_tokens"] is None and empty["unreported_requests"] == 1
    assert not empty["complete"]
    partial = summarize_usage(2, [{"prompt_tokens": 100, "completion_tokens": 30}, {}])
    assert partial["total_tokens"] == 130
    assert partial["coverage"]["total_tokens"] == 1
    assert partial["reported_requests"] == partial["unreported_requests"] == 1
    assert not partial["complete"]
    assert summarize_usage(0, [])["total_tokens"] == 0


def test_usage_filters_invalid_values_and_keeps_reported_zero():
    usage = summarize_usage(
        1, [{"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": True, "reasoning_tokens": -1}]
    )
    assert usage["total_tokens"] == 0 and usage["complete"]
    assert usage["cached_tokens"] is None and usage["reasoning_tokens"] is None


def test_cached_and_reasoning_tokens_are_details_not_added_to_total():
    client = OpenAICompatibleModelClient(build_agent_model_settings(base_url="http://localhost:11434/v1", model="test"))
    turn = client._parse_response(
        {
            "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "total_tokens": 130,
                "prompt_tokens_details": {"cached_tokens": 80},
                "completion_tokens_details": {"reasoning_tokens": 20},
            },
        }
    )
    assert turn.usage["cached_tokens"] == 80 and turn.usage["reasoning_tokens"] == 20
    assert summarize_usage(1, [turn.usage])["total_tokens"] == 130


def test_ledger_counts_attempts_responses_and_known_tokens_separately():
    ledger = AgentAuditLedger("test")
    ledger.record("model_request_started")
    ledger.record(
        "model_response_received", {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
    )
    ledger.record("tool_call_requested")
    ledger.record("model_request_started")
    ledger.record("model_request_failed", {"error_code": "model_timeout"})
    summary = ledger.summary()
    assert summary["model_calls"] == 2 and summary["model_responses"] == 1 and summary["model_failures"] == 1
    assert summary["tool_calls"] == 1 and summary["usage"]["total_tokens"] == 110
    assert not summary["usage"]["complete"]


def test_retained_history_totals_do_not_double_count_or_claim_lifetime_usage():
    registry = AgentAuditRegistry(max_recent_runs=1)
    old = AgentAuditLedger("old")
    old.record("model_request_started")
    old.record("model_response_received", {"usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    registry.start(old)
    registry.finish(old)
    registry.finish(old)
    assert registry.snapshot()["totals"]["model_calls"] == 1
    active = AgentAuditLedger("active")
    registry.start(active)
    active.record("model_request_started")
    totals = registry.snapshot()["totals"]
    assert totals["runs"] == 2 and totals["usage"]["total_tokens"] == 12
    assert not totals["usage"]["complete"]
    registry.finish(active)
    snapshot = registry.snapshot()
    assert snapshot["retained_run_limit"] == 1 and snapshot["totals"]["runs"] == 1
    assert snapshot["totals"]["usage"]["total_tokens"] is None


def test_completed_duration_does_not_grow_while_panel_is_open(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import audit

    now = [100.0]
    from types import SimpleNamespace

    monkeypatch.setattr(audit, "time", SimpleNamespace(monotonic=lambda: now[0]))
    ledger = AgentAuditLedger("test")
    ledger.transition(AgentRunState.MODEL_REQUESTING, "model_request_started")
    now[0] = 105.0
    ledger.transition(AgentRunState.MODEL_RESPONDED, "model_response_received")
    ledger.transition(AgentRunState.FINALIZING, "finalizing")
    ledger.transition(AgentRunState.COMPLETED, "run_completed")
    now[0] = 999.0
    assert ledger.summary()["duration_ms"] == 5000.0
