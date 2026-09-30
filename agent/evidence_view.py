"""Compact public evidence without changing observations or trusting derivations."""
from __future__ import annotations

import copy
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def deduplicate_program_result(response: dict[str, Any], available_indices: set[int] | None = None) -> dict[str, Any]:
    """Share exact emitted copies only with full results present in this message.

    Runs after size fitting, so a reference never points to an omitted record.
    Calls/epochs/unknowns remain inside each full value. No cross-read merging.
    """
    known = {canonical(r['value']): r['index'] for r in response.get('full_results', [])
             if available_indices is None or r['index'] in available_indices}
    if not known:
        return response
    changes = 0

    def visit(value):
        nonlocal changes
        if isinstance(value, (dict, list)):
            found = known.get(canonical(value))
            if found is not None:
                reference = {'same_as_full_result_index': found}
                if len(canonical(reference)) < len(canonical(value)):
                    changes += 1
                    return reference
        if isinstance(value, dict):
            return {key: visit(child) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child) for child in value]
        return value

    emitted = visit(response.get('derived_output', []))
    if not changes:
        return response
    result = copy.deepcopy(response)
    result['derived_output'] = emitted
    return result


def review_evidence(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use every visible observation, not a handpicked supporting subset.

    Tool payloads become JSON objects instead of multiply escaped strings.
    Author code remains beside its tool observations for derived-value provenance.
    Submission feedback is not new game evidence and is excluded.
    """
    calls = {}
    evidence = []
    read_calls = {}
    for message in messages:
        if message.get('role') == 'assistant':
            for call in message.get('tool_calls', []):
                calls[call['id']] = call['function']
        if message.get('role') != 'tool':
            continue
        call = calls.get(message.get('tool_call_id'), {})
        if call.get('name', message.get('name')) == 'submit_response':
            continue
        content = message.get('content')
        try:
            payload = json.loads(content) if isinstance(content, str) else copy.deepcopy(content)
        except (ValueError, TypeError):
            payload = content
        if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
            payload['data'] = deduplicate_program_result(payload['data'])
        # Bind each native record directly to its business call. Keep the execution
        # receipt (errors, omissions and derivations) separately; no evidence selection.
        if call.get('name') == 'execute_python' and isinstance(payload, dict) and isinstance(payload.get('data'), dict):
            data = payload['data']
            read_calls.update({r['index']: r for r in data.get('reads', [])})
            for record in data.pop('full_results', []):
                source = read_calls.get(record['index'], {})
                evidence.append({'evidence_id': len(evidence), 'tool_call_id': message.get('tool_call_id'),
                    'native_result_index': record['index'],
                    'call': {'name': source.get('tool', 'read_result'), 'arguments': source.get('arguments', {'index': record['index']})},
                    'result': record['value']})
        evidence.append({'evidence_id': len(evidence), 'tool_call_id': message.get('tool_call_id'),
                         'call': copy.deepcopy(call), 'result': payload})
    return evidence
