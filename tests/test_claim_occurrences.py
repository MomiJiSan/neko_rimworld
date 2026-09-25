"""R4: evidence and grammar, not an ever-growing game-scenario phrase list."""
import copy

import pytest

from plugin.plugins.neko_rimworld.agent.claims import check_claims, describe


def mixed():
    return [{'rows':[{'id':'PawnA','name':'Alice','faction':{'is_player':True}},
                     {'id':'PawnB','name':'Bob'}]}]


def completed_state(*, status='ok', scope=None, empty=False, matched=True):
    data={'status':status,'sample':{'load_epoch':'load','started_tick':6,'finished_tick':6},
          'objects':[] if empty else [{'target':{'id':'Thing_Stove1' if matched else 'Thing_Other2'},
             'facets':{'condition':{'status':'observed'}},
             'coverage':{'registered_facets_read_successfully':True}}]}
    if scope is not None:data['scope']=scope
    return {'request':{'operation':'inspect_state','arguments':{
        'targets':[{'id':'Thing_Stove1'}],'include_fogged':True}},
        'response':{'payload':{'status':status,'data':data}}}


def test_parenthetical_identity_keeps_its_subject_across_annotation_comma():
    readings=[{'response':{'payload':{'status':'ok','data':{'objects':[{
        'target':{'id':'Thing_Human10378','name':'Rescue_Middle',
                  'pawn_membership':{'is_free_colonist':True}},
        'facets':{'work':{'status':'observed'}}}]}}}}]
    assert check_claims('Rescue_Middle（ID Thing_Human10378，Map_0 的殖民者）。',observations=readings)==[]
    assert check_claims('Alice（ID PawnA，备注），Bob是殖民者。',observations=mixed())


def test_completed_old_state_reply_can_attest_its_executed_fog_parameter():
    reading=completed_state();original=copy.deepcopy(reading)
    assert check_claims('本次炉灶查询包含迷雾。',observations=[reading])==[]
    assert reading==original


@pytest.mark.parametrize('reading',[
    completed_state(scope={'include_fogged':False}),
    completed_state(status='partial'),completed_state(status='unavailable'),
    completed_state(empty=True),completed_state(matched=False),
    {'request':{'arguments':{'include_fogged':True}}},
    {'response':{'payload':{'status':'error','data':{'scope':{'include_fogged':True}}}}},
])
def test_requested_fog_or_failed_unmatched_reply_never_authorizes_scope(reading):
    assert any(r['code']=='fog_inclusive_claim'for r in check_claims('本次包含迷雾。',observations=[reading]))


def test_returned_scope_is_authoritative_for_new_native_reply():
    reading=completed_state(scope={'include_fogged':True})
    reading['request']['arguments']['include_fogged']=False
    assert check_claims('本次包含迷雾。',observations=[reading])==[]


@pytest.mark.parametrize('text',[
    '已读伤病为空但不等同完全健康。','已读伤病为空但不等同于完全健康。',
    '若只有 Rescue_Near 在场，需要手动下达治疗指令。',
    'If only Rescue_Near is present, arrange help.',
])
def test_negated_equivalence_and_conditional_premise_are_not_assertions(text):
    readings=[{'from':'pawns','matched_count':31,'rows':[{'id':'Thing_Near1','name':'Rescue_Near'}],
               'page':{'offset':0,'has_more':True}}]
    assert check_claims(text,observations=readings)==[]


@pytest.mark.parametrize('text',[
    'Alice是殖民者。Bob是殖民者。','Alice和Bob都是殖民者。',
    'Alice是殖民者，但Bob是殖民者。','如果Alice是殖民者，Bob是殖民者。',
])
def test_supported_first_occurrence_or_subject_cannot_license_later_subject(text):
    rejected=check_claims(text,observations=mixed())
    assert any(x['code']=='identity_claim'for x in rejected)
    assert all('Bob'in x['clause']for x in rejected)


def test_every_unsupported_occurrence_is_located_in_its_own_field():
    detail='Bob是殖民者。Bob是玩家派系成员。'
    rejected=check_claims('Alice是殖民者。',detail,observations=mixed())
    assert len(rejected)==2
    assert all(r['field']=='detail'and r['path']=='detail'for r in rejected)
    assert all(detail[r['start']:r['end']]==r['quote']for r in rejected)
    assert 'detail['in describe(rejected)


def test_answer_item_location_and_quote_do_not_hide_next_occurrence():
    parts=[{'text':'“Bob是殖民者”尚未证实。'},{'text':'Bob是殖民者。'}]
    rejected=check_claims(parts,observations=mixed())
    assert len(rejected)==1 and rejected[0]['path']=='answer[1].text'


def test_colonist_root_membership_does_not_license_another_subject():
    readings=[{'from':'spawned_free_colonists','rows':[{'id':'PawnA','name':'Alice'}]},
              {'rows':[{'id':'PawnB','name':'Bob'}]}]
    assert check_claims('Alice是殖民者。',observations=readings)==[]
    assert check_claims('Alice和Bob都是殖民者。',observations=readings)


def test_observed_name_starting_with_conditional_word_is_still_a_subject():
    readings=[{'rows':[{'id':'PawnR','name':'若风'}]}]
    assert check_claims('若风是殖民者。',observations=readings)


def test_attribute_labels_do_not_become_entity_aliases():
    readings=[{'rows':[{'id':'PawnA','name':'Alice','faction':{'is_player':True}},
                      {'id':'PawnB','name':'Bob','skills':[{'label':'医疗','level':1}]}]}]
    assert check_claims('Alice是殖民者且医疗工作已安排。',observations=readings)==[]


@pytest.mark.parametrize('text', [
    'Alice是帮助Bob的殖民者。',
    'Alice是照顾Bob的殖民者。',
    'Alice是给Bob讲故事的殖民者。',
])
def test_relative_clause_object_is_not_treated_as_identity_subject(text):
    assert check_claims(text, observations=mixed()) == []


@pytest.mark.parametrize('text', [
    'Alice和Charlie都是殖民者。',
    'Alice与Charlie同为殖民者。',
    'Alice是殖民者而Charlie是殖民者。',
    'Alice是殖民者且Charlie是殖民者。',
    'Alice and Charlie are colonists.',
])
def test_unobserved_explicit_subject_cannot_borrow_known_subject_evidence(text):
    rejected = check_claims(text, observations=mixed())
    assert any(item['code'] == 'identity_claim' for item in rejected)


def test_supported_subject_does_not_require_evidence_for_later_object():
    assert check_claims('Alice是殖民者并帮助Bob。', observations=mixed()) == []


def test_ascii_alias_must_match_whole_subject_not_substring():
    assert check_claims('AliceClone是殖民者。', observations=mixed())


def test_joint_subjects_with_identity_evidence_are_accepted():
    readings = [{'rows': [
        {'id': 'PawnA', 'name': 'Alice', 'faction': {'is_player': True}},
        {'id': 'PawnB', 'name': 'Bob', 'faction': {'is_player': True}},
    ]}]
    assert check_claims('Alice和Bob都是殖民者。', observations=readings) == []


def test_non_identity_parenthetical_clause_does_not_bind_to_outer_subject():
    assert check_claims('Bob（Alice是殖民者）仍在场。', observations=mixed()) == []


def test_parenthetical_id_before_predicate_does_not_hide_joint_subject():
    assert check_claims('Alice（ID PawnA）和Charlie都是殖民者。', observations=mixed())


def test_ambiguous_duplicate_name_is_not_treated_as_unique_subject():
    readings = [{'rows': [
        {'id': 'PawnA', 'name': 'Alice', 'faction': {'is_player': True}},
        {'id': 'PawnB', 'name': 'Alice'},
    ]}]
    assert check_claims('Alice是殖民者。', observations=readings) == []


def test_quantified_unnamed_group_cannot_borrow_one_named_pawn_identity():
    readings=[{'rows':[{'id':'Pawn1','faction':{'is_player':True}}]}]
    assert any(x['code']=='identity_claim'for x in check_claims('这 5 名殖民者全部属于玩家派系。',observations=readings))
