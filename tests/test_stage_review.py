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


def assessment(verdict='contradiction', index=0, path='/data/value'):
    return {'coverage': 'complete', 'remaining': '', 'checks': [{
        'claim': '数量为8。', 'verdict': verdict, 'evidence': [{'evidence_id': index, 'path': path}],
        'reason': '读取结果为7。', 'impact': '' if verdict == 'supported' else '应将数量改为7。'}]}


def submit(text):
    return reply('submit_response', {'answer': text, 'detail': text})


async def run_review(review_turn, revisions=None, *, initial=None, allow_text_completion=False,
                     interaction_factory=None):
    async def query(args): return {'value': 7}
    author = Model([reply('query_game', {}), initial or submit('数量为8。'), *(revisions if revisions is not None else [submit('数量为7。')])])
    critic = Model([review_turn])
    registry = AgentAuditRegistry()
    runtime = AgentRuntime(observation_tools=[ToolSpec('query_game', 'Read value', {'type': 'object', 'properties': {}}, query)],
        audit_registry=registry, logger=Logger(), model_factory=lambda settings: author,
        review_model_factory=lambda settings: critic, allow_text_completion=allow_text_completion,
        interaction_factory=interaction_factory)
    settings = build_agent_model_settings(base_url='http://127.0.0.1:1234', model='test',
        review_before_delivery=True, run_budget_seconds=90)
    result = await runtime.run('数量是多少？', settings)
    return result, author, critic, registry


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', ['supported', 'contradiction'])
async def test_natural_completion_obeys_review_switch_and_revision_tracking(verdict):
    first = AssistantTurn('r', 'test', 'stop', '数量为8。')
    revised = AssistantTurn('r', 'test', 'stop', '数量为7。')
    result, author, critic, _ = await run_review(reply('record_review', assessment(verdict)),
        initial=first, revisions=[revised], allow_text_completion=True)

    assert len(critic.requests) == 1
    assert result['status'] == 'ok'
    if verdict == 'contradiction':
        assert result['agent_text'] == '数量为7。' and len(author.requests) == 3
        assert result['review']['author_revision_attempted'] is True
        assert result['review']['valid_resubmitted'] is True
    else:
        assert result['agent_text'] == '数量为8。' and len(author.requests) == 2
        assert result['review']['status'] == 'no_issues_reported'
    assert result['review']['final_rechecked'] is False


class NaturalInteraction:
    """An adapter whose accepted output is returned independently of saved draft fields."""
    def __init__(self, setup, session, update):
        self.setup, self.session, self.update = setup, session, update

    def prompt(self, cycles): return 'Read the value and answer.'
    def request_messages(self, messages): return messages
    def snapshot(self): return {}
    def tools(self):
        from plugin.plugins.neko_rimworld.agent.handoff import make_submission_tool
        return (*self.setup.tools, make_submission_tool(self.session.submission, self.session.observations, self.update))
    def accept_text(self, text, finish_reason):
        from plugin.plugins.neko_rimworld.agent.handoff import HandoffValidationError
        if finish_reason != 'stop':
            raise HandoffValidationError('incomplete_answer', 'Incomplete text')
        return validate_submission(text, text)
    def attach_result(self, result):
        from plugin.plugins.neko_rimworld.agent.handoff import attach_agent_handoff
        return attach_agent_handoff(result)
    async def aclose(self): pass


@pytest.mark.asyncio
async def test_adapter_text_is_reviewed_using_its_actual_accepted_output():
    first = AssistantTurn('r', 'test', 'stop', '数量为8。')
    revised = AssistantTurn('r', 'test', 'stop', '数量为7。')
    result, author, critic, _ = await run_review(reply('record_review', assessment()),
        initial=first, revisions=[revised], interaction_factory=NaturalInteraction)
    assert len(critic.requests) == 1 and len(author.requests) == 3
    payload = json.loads(critic.requests[0][1]['content'])
    assert payload['draft'] == {'answer': '数量为8。', 'full_report': '数量为8。'}
    assert result['agent_text'] == '数量为7。'
    assert result['review']['valid_resubmitted'] is True
    assert result['draft']['full_text'] == '数量为7。'


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter', [False, True])
async def test_failed_natural_revision_restores_the_reviewed_candidate(adapter):
    result, _, critic, _ = await run_review(reply('record_review', assessment()),
        initial=AssistantTurn('r', 'test', 'stop', '数量为8。'),
        revisions=[AssistantTurn('r', 'test', 'stop', '7' * 13000), ModelClientError('invalid_request', 'stop')],
        allow_text_completion=not adapter, interaction_factory=NaturalInteraction if adapter else None)
    assert len(critic.requests) == 1
    assert result['status'] == 'degraded' and result['agent_text'] == '数量为8。'
    assert result['review']['author_revision_attempted'] is True
    assert result['review']['valid_resubmitted'] is False
    assert result['review']['fallback'] == 'pre_review_draft'


@pytest.mark.asyncio
async def test_one_review_reuses_author_history_and_does_not_rereview_revision():
    result, author, critic, _ = await run_review(reply('record_review', assessment()))
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
    result, author, critic, _ = await run_review(reply('record_review', assessment('supported')))
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
async def test_oversize_review_is_explicit_and_never_sends_truncated_evidence(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import stage_review
    monkeypatch.setattr(stage_review, 'MAX_REVIEW_CHARS', 100)
    result, author, critic, _ = await run_review(reply('record_review', assessment('supported')))
    assert len(critic.requests) == 0 and len(author.requests) == 2
    assert result['agent_text'] == '数量为8。'
    assert result['review']['status'] == 'skipped_context_limit'
    assert result['review']['input_chars'] > result['review']['input_limit_chars'] == 100
    assert result['review']['valid_resubmitted'] is False
    assert any('not a reviewed approval' in c for c in result['caveats'])


@pytest.mark.asyncio
async def test_rejected_revision_is_not_marked_valid_or_delivered_as_success():
    review = reply('record_review', assessment())
    result, author, critic, _ = await run_review(review, [submit('7' * 13000), ModelClientError('invalid_request', 'stop')])
    assert result['status'] == 'degraded'
    assert result['review']['author_revision_attempted']
    assert result['review']['valid_resubmitted'] is False
    assert result['review']['fallback'] == 'pre_review_draft'
    assert '数量为8。' in result['answer'] and '7' * 1000 not in result['answer']
    assert len(critic.requests) == 1


def test_review_data_can_be_natural_json_but_not_invented_evidence():
    evidence = [{'result': {'data': {'value': 7}}}]
    turn = AssistantTurn('r', 'test', 'stop', json.dumps(assessment('supported')))
    assert parse_review(turn, evidence)['checks'][0]['verdict'] == 'supported'
    for data in [{'issues': []}, assessment(index=-1)]:
        with pytest.raises(ValueError):
            parse_review(reply('record_review', data), evidence)


@pytest.mark.asyncio
async def test_partial_review_is_not_a_silent_pass():
    data = assessment('supported')
    data.update(coverage='partial', remaining='关键比较未检查')
    result, author, critic, _ = await run_review(reply('record_review', data))
    assert len(author.requests) == 3 and len(critic.requests) == 1
    assert result['review']['assessment']['coverage'] == 'partial'
    assert result['review']['issues'][0]['kind'] == 'review_incomplete'


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


def test_bad_citations_are_retained_as_unverified_advice_not_silent_approval():
    from plugin.plugins.neko_rimworld.agent.stage_review import review_issues
    for data in [assessment(index=3), assessment(path='/data/imaginary'), assessment('supported', path='/wrong')]:
        result = parse_review(reply('record_review', data), [{'result': {'data': {'value': 7}}}])
        assert result['checks'] == data['checks']
        assert result['citation_errors'][0]['status'] == 'unresolved'
        issues = review_issues(result)
        assert any(i['kind'] == 'citation_unverified' for i in issues)
        assert '数量为8' in issues[-1]['reason']


def test_bad_path_keeps_valid_evidence_identity_in_author_feedback():
    from plugin.plugins.neko_rimworld.agent.stage_review import author_feedback, review_issues

    evidence = [{'evidence_id': 0, 'tool_call_id': 'route-batch', 'native_result_index': 7,
                 'call': {'name': 'inspect_route'}, 'result': {'data': {'value': 7}}}]
    data = assessment('supported', path='/wrong_path')
    data['checks'].append(assessment('supported', index=8)['checks'][0])
    review = parse_review(reply('record_review', data), evidence)
    feedback = json.loads(author_feedback(review_issues(review), evidence).split('\n')[-1])

    assert feedback['evidence_bindings'] == [
        {'evidence_id': 0, 'tool_call_id': 'route-batch', 'tool': 'inspect_route', 'native_result_index': 7}]
    assert len(review['citation_errors']) == 2
    assert all(error['status'] == 'unresolved' for error in review['citation_errors'])
    assert review['checks'] == data['checks']  # Restoring identity does not validate the path or the claim.


def test_review_schema_matches_cross_field_parser_requirements():
    import jsonschema
    from plugin.plugins.neko_rimworld.agent.stage_review import REVIEW_TOOL
    validator = jsonschema.Draft202012Validator(REVIEW_TOOL['function']['parameters'])
    for value in [assessment('supported'), assessment(), {**assessment(), 'coverage': 'partial', 'remaining': 'x'}]:
        assert validator.is_valid(value)
        parse_review(reply('record_review', value), [{'result': {'data': {'value': 7}}}])
    invalid = assessment('supported'); invalid['checks'][0]['evidence'] = []
    for value in [invalid, {**assessment(), 'coverage': 'partial'}]:
        assert not validator.is_valid(value)
        with pytest.raises(ValueError): parse_review(reply('record_review', value), [])
