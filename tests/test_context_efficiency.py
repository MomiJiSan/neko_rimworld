import copy
import json

from plugin.plugins.neko_rimworld.agent.audit import AgentAuditLedger
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession
from plugin.plugins.neko_rimworld.agent.evidence_view import deduplicate_program_result, review_evidence
from plugin.plugins.neko_rimworld.agent.contracts import AssistantTurn, ToolCall


def test_advisory_does_not_invalidate_existing_conversation_prefix():
    session = AgentRunSession.create(ledger=AgentAuditLedger('q'), system_prompt='stable policy', question='q')
    session.append_assistant(AssistantTurn('r', 'test', 'tool_calls', '',
        (ToolCall('r', 'look', '{}'),), reasoning_content='private continuation'))
    session.messages.append({'role': 'tool', 'tool_call_id': 'r', 'content': 'large evidence'})
    saved = copy.deepcopy(session.messages)
    first = session.request_messages('cycle=1')
    second = session.request_messages('cycle=2')
    assert first[:-1] == second[:-1] == saved
    assert first[-1] == {'role': 'system', 'content': 'cycle=1'}
    assert session.messages == saved


def test_dedup_keeps_source_scope_and_derived_labels_without_mutation():
    value = {'ok': True, 'data': {'n': 0, 'scope': 'partial', 'sample': 4}}
    original = {'full_results': [{'index': 0, 'value': value}],
                'derived_output': [{'chosen': value, 'computed': 5}], 'printed': 'unaltered'}
    saved = copy.deepcopy(original)
    reduced = deduplicate_program_result(original)
    assert reduced['derived_output'] == [{'chosen': {'same_as_full_result_index': 0}, 'computed': 5}]
    assert reduced['full_results'] == original['full_results']
    assert original == saved
    assert deduplicate_program_result({'derived_output': [value]}) == {'derived_output': [value]}


def test_review_gets_visible_failures_and_derivations_not_private_reasoning():
    messages = [
        {'role': 'assistant', 'reasoning_content': 'PRIVATE', 'tool_calls': [
            {'id': 'r', 'function': {'name': 'look', 'arguments': '{}'}},
            {'id': 's', 'function': {'name': 'submit_response', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'r', 'content': '{"ok":false,"error":{"code":"unavailable"}}'},
        {'role': 'tool', 'tool_call_id': 's', 'content': '{"full_text":"not evidence"}'},
    ]
    view = review_evidence(messages)
    assert len(view) == 1 and view[0]['result']['ok'] is False
    assert 'PRIVATE' not in json.dumps(view)
