import pytest

from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger
from plugin.plugins.neko_rimworld.agent.model_context import RETIRED_DEFAULTS, validate_deployment
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession


def test_default_session_has_json_projection_without_environment(monkeypatch):
    for name in (*RETIRED_DEFAULTS, "NEKO_EVIDENCE_COMPACT", "NEKO_PUBLIC_INPUT_TOKEN_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    session = AgentRunSession.create(ledger=AgentAuditLedger("question"), system_prompt="system", question="question")
    assert session.context_pruner is not None
    assert not hasattr(session, "policy") and not hasattr(session, "input_budget")


@pytest.mark.parametrize("name,expected", RETIRED_DEFAULTS.items())
def test_retired_switch_cannot_select_an_old_variant(monkeypatch, name, expected):
    monkeypatch.setenv(name, "0" if expected else "1")
    with pytest.raises(ValueError, match="retired"):
        validate_deployment()


@pytest.mark.parametrize("name", ["NEKO_EVIDENCE_COMPACT", "NEKO_PUBLIC_INPUT_TOKEN_LIMIT"])
def test_retired_codec_and_production_input_cap_fail_explicitly(monkeypatch, name):
    monkeypatch.setenv(name, "1")
    with pytest.raises(ValueError, match="retired"):
        validate_deployment()


def test_matching_old_value_is_a_migration_notice_not_an_option(monkeypatch):
    monkeypatch.setenv("NEKO_CONTEXT_PRUNING", "1")
    with pytest.warns(DeprecationWarning, match="redundant"):
        validate_deployment()
