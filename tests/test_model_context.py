import pytest

from plugin.plugins.neko_rimworld.agent import model_context


def test_one_opt_in_and_retired_modes_fail_explicitly(monkeypatch):
    monkeypatch.delenv('NEKO_EVIDENCE_COMPACT', raising=False)
    monkeypatch.delenv('NEKO_CONTEXT_PRUNING', raising=False)
    assert not model_context.enabled()
    monkeypatch.setenv('NEKO_CONTEXT_PRUNING', '1')
    assert model_context.enabled()
    monkeypatch.setenv('NEKO_CONTEXT_PRUNING', '3')
    with pytest.raises(ValueError, match='boolean'):
        model_context.enabled()
    monkeypatch.setenv('NEKO_CONTEXT_PRUNING', '1')
    for mode in ['1', '2', '3', 'aggressive', 'pruned']:
        monkeypatch.setenv('NEKO_EVIDENCE_COMPACT', mode)
        with pytest.raises(ValueError, match='retired'):
            model_context.enabled()
