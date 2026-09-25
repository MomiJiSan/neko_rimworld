"""v1.10 公开提交契约：完整聚合不等于完整明细；否定包含不等于包含。"""
from __future__ import annotations

import pytest

from plugin.plugins.neko_rimworld.agent.claims import check_claims
from plugin.plugins.neko_rimworld.agent.handoff import SubmissionDraft, SubmissionPatchError

AGGREGATE_TEXT = "总量来自对全部20个匹配实例的完整聚合，明细只展示1条，不影响总量。"


def steel_result(*, total_key="matched_count", quantity_complete=True,
                 offset=0, has_more=True):
    return {
        "id": "steel-detail", "from": "things",
        "scope": {"args": {"map_id": "Map_0", "include_held": False}},
        "where": {"definition.def_name": {"eq": "Steel"}},
        "scan_complete": True, "fields_complete": True,
        total_key: 20,
        "aggregates": {
            "records": {"op": "count", "value": 20, "complete": True},
            "quantity": {"op": "sum", "field": "stack_count", "unit": "stack_units",
                         "value": 1170 if quantity_complete else None,
                         "complete": quantity_complete},
        },
        "rows": [{"id": "Thing_Steel123", "label": "钢铁", "stack_count": 75}],
        "page": {"offset": offset, "limit": 1, "has_more": has_more},
    }


def observations(*results):
    return [{"observation_id": "query-1", "response": {
        "connection_epoch": "connection", "colony_id": "colony", "load_epoch": "load",
        "payload": {"status": "ok", "data": {"results": list(results)}},
    }}]


def assert_contract(entry, text, readings, code=None):
    """Both public entry points must agree; rejected drafts remain undeliverable."""
    if entry == "check_claims":
        rejected = check_claims(text, observations=readings)
        if code:
            assert code in [item["code"] for item in rejected]
        else:
            assert rejected == []
        return
    draft = SubmissionDraft()
    if code:
        with pytest.raises(SubmissionPatchError) as caught:
            draft.apply({"answer": [{"text": text}]}, observations=readings)
        assert caught.value.code == code
        assert draft.claims_blocked is True
        assert draft.full_text is None and draft.agent_text is None
    else:
        result = draft.apply({"answer": [{"text": text}]}, observations=readings)
        assert result["agent_text"] == text
        assert draft.snapshot()["complete"] is True
        assert draft.claims_blocked is False


def mixed_subject_readings():
    """Alice 有派系证据，Bob 没有。"""
    return [{"rows": [{"id": "PawnA", "name": "Alice", "faction": {"is_player": True}},
                      {"id": "PawnB", "name": "Bob"}]}]


def population_predicate_readings():
    """明细行带 id，但“按阵营筛选”的证据在**已执行的解析范围**里。"""
    return [{"rows": [{"id": "Thing_Wall1", "label": "墙"}]},
            {"scope": {"where": {"faction": {"is_player": {"eq": True}}}}, "rows": []}]


@pytest.mark.parametrize("text", [
    "Bob是殖民者。",
    "Alice的身份已读取。Bob是殖民者。",
    "Bob是殖民者，Alice是玩家派系成员。",
    "PawnB是殖民者。",
])
def test_mixed_sentence_cannot_borrow_another_subjects_faction_evidence(entry, text):
    """实测缺陷（跨主体串证）：同一篇里只要**任意**被提及对象有证据，整篇就放行。

    `_evidence_present` 用整篇文本算“被提及的主体”，于是一句里提到 Alice 的
    已读记录，就能为 Bob 的身份断言授权。断言必须绑定到**它所在子句**的主体。
    """
    assert_contract(entry, text, mixed_subject_readings(), "identity_claim")


def test_population_statement_binds_to_its_own_clause_not_listing_ids(entry):
    """反向误报：整篇里出现明细行 id，不等于断言主体就是那些对象。

    实测（v1.10）：建筑题的正文/明细里有大量 Thing_* 行 id，而“按阵营筛选属于
    玩家阵营的实例为 0”这句的**子句**里没有任何对象名，证据来自已执行的
    faction 筛选范围；用整篇算主体会把它误判成无证据。
    """
    assert_contract(entry, "按阵营筛选属于玩家阵营的实例为 0。", population_predicate_readings())


@pytest.fixture(params=["check_claims", "submission"])
def entry(request):
    return request.param


@pytest.mark.parametrize("total_key", ["matched_count", "known_matches"])
@pytest.mark.parametrize("text", [
    AGGREGATE_TEXT,
    "钢铁总量来自对所有20个匹配实例的完整聚合，明细只展示1条。",
])
def test_complete_aggregate_with_partial_detail_passes(entry, total_key, text):
    assert_contract(entry, text, observations(steel_result(total_key=total_key)))


@pytest.mark.parametrize("text", [
    "include_held=未包含持有物（false）。",
    "include_held=未包含被持有物（false）。",
    "本次不包含持有物。",
    "本次没有包含持有物。",
])
def test_negated_held_inclusion_passes(entry, text):
    assert_contract(entry, text, observations(steel_result()))


@pytest.mark.parametrize("text", [
    "include_held=包含持有物（false）。",
    "本次含被持有物。",
    "未包含持有物；本次包含持有物。",
    "未包含持有物，但包括随身物品。",
])
def test_positive_held_inclusion_still_rejected(entry, text):
    assert_contract(entry, text, observations(steel_result()), "held_inclusive_claim")


@pytest.mark.parametrize("text", [
    "所有20实例都已逐项检查。",
    "总量来自完整聚合；所有20实例都已逐项检查。",
    "总量来自完整聚合且所有20实例都已逐项检查。",
    "总量来自对全部20个匹配实例的完整聚合且所有20实例都已逐项检查。",
    "总量来自对全部20个匹配实例的完整聚合，所有20实例都已逐项检查。",
    "全部20个匹配实例的完整聚合说明所有20实例都已逐项检查。",
    "钢铁总量来自完整聚合；钢铁是唯一物资。",
])
def test_aggregate_narration_does_not_license_row_claims(entry, text):
    assert_contract(entry, text, observations(steel_result()), "incomplete_quantifier_claim")


def test_aggregate_does_not_license_another_collection(entry):
    body = {"matched_count": 64, "rows": [{"label": "右眼"}],
            "page": {"offset": 0, "has_more": True}}
    text = "总量来自对全部20个匹配实例的完整聚合；全部64部位均满血。"
    assert_contract(entry, text, observations(steel_result(), body), "incomplete_quantifier_claim")


@pytest.mark.parametrize("change", ["incomplete_quantity", "missing_complete", "no_aggregates",
                                        "incomplete_scan", "request_only"])
def test_only_corresponding_complete_aggregates_license_the_narration(entry, change):
    result = steel_result()
    if change == "incomplete_quantity":
        result = steel_result(quantity_complete=False)
    elif change == "missing_complete":
        result["aggregates"]["quantity"].pop("complete")
    elif change == "no_aggregates":
        result.pop("aggregates")
    elif change == "incomplete_scan":
        result["scan_complete"] = False
    elif change == "request_only":
        result["request"] = {"aggregates": result.pop("aggregates")}
    assert_contract(entry, AGGREGATE_TEXT, observations(result), "incomplete_quantifier_claim")


def test_another_results_complete_aggregate_cannot_license_this_one(entry):
    gold = steel_result()
    gold.update(id="gold-detail", rows=[{"label": "黄金"}], matched_count=10)
    text = "黄金总量来自对全部10个匹配实例的完整聚合；钢铁总量来自对全部20个匹配实例的完整聚合。"
    assert_contract(entry, text, observations(gold, steel_result(quantity_complete=False)),
                    "incomplete_quantifier_claim")


@pytest.mark.parametrize("text,code", [
    (AGGREGATE_TEXT, None),
    ("所有20实例都已逐项检查。", "incomplete_quantifier_claim"),
])
def test_last_page_is_not_full_detail_but_aggregate_can_be_complete(entry, text, code):
    assert_contract(entry, text, observations(steel_result(offset=19, has_more=False)), code)


@pytest.mark.parametrize("status", ["ok", "partial"])
def test_world_scope_flags_license_held_but_not_fog_claim(entry, status):
    reading = {"response": {"payload": {"status": status, "data": {
        "objects_summary": {"records": 2, "scan_complete": status == "ok", "scope": {
            "selection": {"capabilities": ["medicine"]},
            "include_held": True, "visible_only": True,
            "counts_apply_to": "matching_objects_in_observed_footprint",
        }}, "objects": [],
    }}}}
    assert_contract(entry, "本次范围包含被持有物。", [reading])
    assert_contract(entry, "本次包含迷雾。", [reading], "fog_inclusive_claim")
    reading["response"]["payload"] = {"status": "error", "data": None}
    reading["request"] = {"arguments": {"include_held": True}}
    assert_contract(entry, "本次范围包含被持有物。", [reading], "held_inclusive_claim")


def test_partial_attribute_label_is_not_an_alias_for_its_owner(entry):
    data = {"objects":[{"target":{"id":"Thing_Doctor","name":"Doctor"},"facets":{"pawn":{
        "status":"observed","skills":[{"definition":"Medicine","label":"医疗","level":16}],
        "skills_total":12,"skills_complete":False,
    }}}]}
    readings=[{"response":{"payload":{"status":"ok","data":data}}}]
    assert_contract(entry, "3张床全部医疗标记。441格全部混凝土。", readings)
    assert_contract(entry, "Doctor的全部12项技能已读取。", readings, "incomplete_quantifier_claim")


@pytest.mark.parametrize('text',[
    '不可断言完全健康。',
    '不可认定 Doctor 完全健康。',
    '可见伤情为空，未含隐藏伤情，不能据此认定完全健康；不可断言完全健康。',
])
def test_epistemic_negation_is_not_a_health_assertion(entry,text):
    assert_contract(entry,text,observations())


@pytest.mark.parametrize('text',[
    '不可否认 Doctor 完全健康。',
    '不可断言 Near 完全健康，但 Doctor 完全健康。',
    '不可认定 Near 完全健康。Doctor 完全健康。',
])
def test_epistemic_negation_does_not_license_an_affirmative_other_claim(entry,text):
    assert_contract(entry,text,observations(),'health_completeness_claim')
