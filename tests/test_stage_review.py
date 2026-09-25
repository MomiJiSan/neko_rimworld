import json

import pytest

from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime, AssistantTurn, ToolCall, build_agent_model_settings, ModelClientError
from plugin.plugins.neko_rimworld.agent.stage_review import parse_review
from plugin.plugins.neko_rimworld.agent.tools import ToolSpec
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, validate_submission, count_handoff_tokens


class Logger:
    def info(self, *args): pass
    def warning(self, *args): pass
    def error(self, *args): pass


class Model:
    def __init__(self, turns):
        self.turns, self.requests = list(turns), []

    async def complete(self, messages, tools, *, timeout):
        self.requests.append(messages)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def reply(name, arguments):
    return AssistantTurn('r', 'test', 'tool_calls', '', (ToolCall(name, name, json.dumps(arguments)),),
                         usage={'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}, reasoning_content='PRIVATE_AUTHOR_OR_CRITIC')


def submit(text):
    return reply('submit_response', {'answer': text, 'detail': text})


async def run_review(review_turn, revisions=None):
    async def query(args): return {'value': 7}
    author = Model([reply('query_game', {}), submit('数量为8。'), *(revisions if revisions is not None else [submit('数量为7。')])])
    critic = Model([review_turn])
    registry = AgentAuditRegistry()
    runtime = AgentRuntime(observation_tools=[ToolSpec('query_game', 'Read value', {'type': 'object', 'properties': {}}, query)],
        audit_registry=registry, logger=Logger(), model_factory=lambda settings: author,
        review_model_factory=lambda settings: critic)
    settings = build_agent_model_settings(base_url='http://127.0.0.1:1234', model='test',
        review_before_delivery=True, run_budget_seconds=90)
    result = await runtime.run('数量是多少？', settings)
    return result, author, critic, registry


@pytest.mark.asyncio
async def test_one_review_reuses_author_history_and_does_not_rereview_revision():
    result, author, critic, _ = await run_review(reply('record_review', {'issues': [
        {'claim': '数量为8。', 'evidence_ids': [0], 'reason': '读取结果为7。'}]}))
    assert result['status'] == 'ok' and '7' in result['agent_text']
    assert len(critic.requests) == 1 and len(author.requests) == 3
    assert result['audit']['model_calls'] == 4
    assert result['review']['status'] == 'issues_raised'
    assert result['review']['valid_resubmitted'] is True
    assert author.requests[-1][0] == author.requests[0][0]
    assert any(m.get('reasoning_content') for m in author.requests[-1])
    assert any('evidence_bindings' in (m.get('content') or '') for m in author.requests[-1])
    assert 'PRIVATE_AUTHOR_OR_CRITIC' not in json.dumps(critic.requests)
    assert 'PRIVATE_AUTHOR_OR_CRITIC' not in json.dumps(result)


@pytest.mark.asyncio
async def test_no_issues_preserves_original_without_buying_an_author_rewrite():
    result, author, critic, _ = await run_review(reply('record_review', {'issues': []}))
    assert '8' in result['agent_text'] and len(author.requests) == 2 and len(critic.requests) == 1
    assert result['review']['status'] == 'no_issues_reported'
    assert result['audit']['model_calls'] == 3


@pytest.mark.asyncio
async def test_review_failure_keeps_valid_draft_and_reports_unreviewed_state():
    result, author, critic, registry = await run_review(ModelClientError('model_first_response_timeout', 'timed out', usage={'prompt_tokens': 5, 'completion_tokens': 0, 'total_tokens': 5}))
    assert '8' in result['agent_text'] and len(author.requests) == 2 and len(critic.requests) == 1
    assert result['review']['status'] == 'unavailable'
    snapshot = registry.snapshot()
    assert snapshot['latest_run']['usage']['total_tokens'] == 25
    assert snapshot['latest_run']['usage']['complete']


@pytest.mark.asyncio
async def test_rejected_revision_is_not_marked_valid_or_delivered_as_success():
    review = reply('record_review', {'issues': [{'claim': '数量为8。', 'evidence_ids': [0], 'reason': '数字应为7'}]})
    result, author, critic, _ = await run_review(review, [submit('7' * 13000), ModelClientError('invalid_request', 'stop')])
    assert result['status'] == 'degraded'
    assert result['review']['author_revision_attempted']
    assert result['review']['valid_resubmitted'] is False
    assert result['review']['fallback'] == 'pre_review_draft'
    assert '数量为8。' in result['answer'] and '7' * 1000 not in result['answer']
    assert len(critic.requests) == 1


def test_review_data_can_be_natural_json_but_not_invented_evidence():
    turn = AssistantTurn('r', 'test', 'stop', '{"issues":[]}')
    assert parse_review(turn, 0) == []
    with pytest.raises(ValueError):
        parse_review(reply('record_review', {'issues': [{'claim': 'x', 'reason': 'y', 'evidence_ids': [3]}]}), 1)


@pytest.mark.asyncio
async def test_reference_fact_share_survives_failed_revision_fallback():
    rendered = 'measured ' * 185
    fact_tokens = count_handoff_tokens(rendered)[0]
    assert 180 < fact_tokens <= 200
    accepted = validate_submission(rendered, rendered, fact_tokens=fact_tokens)

    class FailedRevisionRuntime(AgentRuntime):
        async def _run_loop(self, question, settings, ledger, session, reference):
            session.review = {'status': 'issues_raised', 'valid_resubmitted': False, 'author_revision_attempted': True}
            session.review_initial_draft = SubmissionDraft(full_text=rendered, agent_text=rendered)
            session.review_initial_delivery = accepted['delivery']
            session.submission.full_text = 'rejected revision'
            return self._finish_degraded(ledger, reason='revision failed', error_code='test', observations=[], fallback_answer='rejected revision')

    runtime = FailedRevisionRuntime(observation_tools=[], audit_registry=AgentAuditRegistry(), logger=Logger())
    result = await runtime.run('q', build_agent_model_settings(base_url='http://127.0.0.1:1234', model='test'))
    assert result['status'] == 'degraded'
    assert result['agent_text'] == rendered
    assert result['delivery']['fact_tokens'] == fact_tokens
