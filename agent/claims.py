"""提交时的确定性断言检查：把"没读到却断言"的句子挡回去。

背景（实测）：跨五轮实验，反复出现的错误不是数字算错，而是**用读到的少数事实去断言
它没读到的东西**——

  * 把普通 Pawn 说成殖民者（身份未读）
  * 把某人库存里的黄金说成"我们的物资"（归属/派系未读）
  * 只读了几个健康汇总值就说"完全健康"（conditions/capacities 未读）
  * 把只返回了一页的集合说成"全部/唯一"（listing partial）

这不是"再加一个裁判模型"，而是**确定性检查**：运行时知道这一轮到底读过哪些事实，
所以可以在提交时拒绝那些缺少对应证据的高风险断言，并明确告诉模型缺什么、怎么改。

设计边界（必须诚实）：
  * 这是**缓解**，不是证明。它只覆盖有限的高风险断言类别，靠词表匹配，会有漏报；
    中文措辞多样，也可能误报。因此它只在该类别**确实缺少证据**时才拒绝，并且
    把"要读什么"或"要如何限定措辞"写进错误信息，让模型一次可改。
  * 它不评判数字是否正确（那是评分器的事），也不判断语义是否恰当。
  * 因果（"因为…所以…"）**有意不覆盖**：字段证据无法判定机制解释，写成任何模式
    都只是假装覆盖。见 CLAIM_CLASSES 的说明。

证据模型（v2，2026-09-18 重写）：
  旧版把整轮的读取压成一个扁平布尔袋，于是一处读过就为全篇授权：
    * 读了 Pawn1 的 faction，"这 5 名殖民者全部属于玩家派系"通过
    * 读了 Pawn9 的完整 health，"Pawn1 完全健康"通过
    * 一个失败的请求里写了 include_held=true，"含被持有"通过
    * 任何一行带有 def_name（物品定义名，如 Gold），归属断言永久通过
  这些都是同一个结构性缺陷：**证据没有绑定到主语**。v2 按主语建索引，并增加
  "全称量词必须绑定完整集合"的检查。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# 高风险断言类别 → (触发词, 需要的证据键, 缺失时的提示)
# ---------------------------------------------------------------------------

CLAIM_CLASSES = (
    dict(
        code="held_inclusive_claim",
        label="声称含被持有物",
        # 实测失败：两场咨询把 held=false（排除持有物）的结果描述成“含被持有容器内容”，
        # 把真实的 74 单位报成 0。这一条是**确定性**的：观察里就带着范围标志，
        # 只要本轮没有任何一次读取是 include_held=true，这个声称就与证据矛盾。
        patterns=(r"含被持有", r"包含被持有", r"含持有", r"包括(?:随身|身上|库存)", r"含被持有物",
                  r"including held", r"including (?:carried|contained)"),
        evidence=("include_held_true",),
        hint="本轮没有任何一次读取包含持有物（include_held=true）。请读 include_held=true 后重报，"
             "或把措辞改成“仅地图上生成的物体”。",
    ),
    dict(
        code="fog_inclusive_claim",
        label="声称含迷雾",
        patterns=(r"含迷雾", r"包含迷雾", r"包括迷雾", r"含迷雾中", r"including fog", r"including fogged"),
        evidence=("include_fogged_true",),
        hint="本轮没有任何一次读取包含迷雾（include_fogged=true）。请读 include_fogged=true 后重报，"
             "或把措辞改成“仅当前可见”。",
    ),
    dict(
        code="identity_claim",
        label="身份断言",
        # 必须要求**归属形式**（系词 + 名词），不能只匹配名词。
        #
        # 在 105 份已复核答案上实测：裸名词版命中 18 条，其中 11 条是复核 pass 且无
        # issue 的误报——因为“殖民者”在这些答案里是**人口名词或 root 名**：
        #   “3 名自由殖民者中无人胜任”、“kind=colonists”、“未读取阵营/殖民者身份”
        #   “不在当前地图自由殖民者名单”、“两者都不是殖民者范围内匹配到的对象”
        # 这些都是在命名查询人口或明确拒绘，不是在断言某个对象是殖民者。
        # 改用系词前缀后，命中数降到 7，其中只有 1 条误报。
        # 用固定宽度回顾（Python re 不支持变长）：命中点落在名词上，
        # 前面的否定词才能被否定检查看到。
        patterns=(r"(?<=是)殖民者|(?<=为)殖民者|(?<=算)殖民者|(?<=的)殖民者|(?<=属于)殖民者"
                  r"|(?<=都是)殖民者|(?<=同为)殖民者|(?<=不是)殖民者|(?<=并非)殖民者",
                  r"我方人员", r"我们的(?:人|成员)",
                  # 派系归属也要求同一类证据：“属于玩家派系”是在断言身份事实。
                  r"(?:属于|是|为)\s*(?:玩家|我方|我们的)?\s*(?:派系|阵营)",
                  r"(?:are|is|were)\s+(?:a\s+|the\s+|our\s+)?colonists?\b",
                  r"\bour (?:member|colonist)s?\b"),
        evidence=("identity", "faction", "colonists_root"),
        hint="先读该对象的 identity/faction（例如 is_free_colonist、faction.is_player），"
             "或把措辞限定为“该对象属于哪个派系未读取”。",
    ),
    dict(
        code="ownership_claim",
        label="归属断言",
        # 只授权“在某玩家派系成员手里”——held_by_faction 能建立这一步。
        patterns=(r"我们的(?:物资|资源|库存|财产)", r"我方(?:物资|资源|财产)",
                  r"\bour (?:supplies|resources|property)\b"),
        evidence=("held_by_faction",),
        hint="先读 held_by_faction.is_player（谁持有它、那个持有者是否属于玩家派系），"
             "或明确写成“归属未确认”。",
    ),
    dict(
        code="usability_claim",
        label="可用性/可交易断言",
        # 实测：把“在某玩家成员手里”直接推成“可以直接交易/使用”。
        # held_by_faction 只说明**持有关系**，不建立可取用、可交易或可使用。
        # 当前只读 schema **没有**任何字段能建立可用性，因此这一类没有可授权的证据：
        # 要么改措辞，要么先说明未知——这是有意为之，不是漏实现。
        # 允许中间带修饰词：实测“可以**随时**调配”漏过。
        # 另实测：“已重新组织为可直接**使用**的简短回答”曾被误拒——
        # 那是在说答复本身可直接使用，不是在说游戏物品可取用。所以“使用”
        # 必须带领域宾语（物资/资源/…），“交易/调配/动用/拿取/取用”本身
        # 就足够领域特定。
        patterns=(r"(?:物资|资源|物品|库存|货物|装备|武器|药品|食物|黄金|白银|零件|材料)[^。；\n]{0,8}?可以[^。；\n]{0,4}?(?:交易|使用|调配|动用|拿取|取用)",
                  r"可以[^。；\n]{0,4}?(?:交易|调配|动用|拿取|取用)",
                  r"\bcan be (?:traded|used|taken)\b", r"\bavailable for (?:trade|use)\b"),
        evidence=(),
        # 显式声明：没有任何字段能授权这一类，所以总是拒绝。旧版靠“空 tuple 为假”
        # 顺带得到这个行为，重构时极易被“修掉”。
        always_reject=True,
        hint="单凭持有关系不能断言可取用/可交易：这些条件在只读 schema 里没有字段。"
             "请改成“由某玩家派系成员持有”，并明确写成“可取用/可交易条件未确认”。",
    ),
    # 因果（"因为…所以…"）**有意不在此列**：字段证据无法判定一个机制解释是否成立，
    # 写成任何模式都只是假装覆盖。因果由复核/评分处理，不由提交时机械检查处理。

    dict(
        code="health_completeness_claim",
        label="健康完整性断言",
        patterns=(r"完全健康", r"没有(?:任何)?(?:伤|病|问题)", r"无(?:伤|病)情", r"\bfully healthy\b",
                  r"\bno injuries\b", r"\bcompletely healthy\b"),
        evidence=("health_conditions", "health_body_parts", "health_capacities"),
        hint="先读 health 的 conditions/body_parts/capacities（完整集合），"
             "或写“已读的汇总值正常，但不能据此认定完全健康”。",
    ),
)

# 全称 / 排他量词：它们对**整个集合**下断言，因此要求该集合被完整列举。
#
# 实测教训（在 105 份已复核答案上测量）：把“全部/所有/唯一”当裸词匹配会大量误报：
#   * `所有` 命中“所**有权**”这类复合名词
#   * “**唯一**返回的分组”是在描述本次返回，不是在声称全局唯一
#   * “12 项技能**全部**已知”说的是字段完整，不是列举完整
# 所以量词必须与一个**数字紧邻**才算在数集合。
QUANTIFIER_TOTAL_PATTERNS = (
    r"全部\s*\d+", r"所有\s*\d+",
    r"\d+\s*(?:个|项|条|种|名|处|件|组|类)?\s*[，,]?\s*全部",
    r"\d+\s*(?:个|项|条|种|名|处|件|组|类)?\s*[，,]?\s*均(?:为|是|已|无)",
    r"\ball\s+\d+", r"\d+\s+of\s+\d+\s+are\s+all\b",
)
# 排他性断言：只有当子句点名了某个**部分集合里的行标签**时才算越界，
# 因为“唯一”本身也常用于描述本次返回。
QUANTIFIER_LABEL_PATTERNS = (r"唯一", r"仅有", r"只有", r"全部", r"所有", r"\bonly\b")

# 聚合叙述 vs 明细列举：返回契约允许“完整聚合 + 部分明细页”，所以
# “总量来自对全部 N 个匹配实例的完整聚合”是**正确**表述，不应被当成对部分列举的全称断言。
# 但它不能授权“所有 N 个实例都已逐项检查”——那是明细层面的断言，需要列举完整。
#
# 判据是**子句**层面的：出现聚合名词，且没有任何明细列举措辞。
# 实测（v1.10）：这条豁免只应用于“聚合本身被断言完整”的场合；
# 后一句、另一个集合、聚合不完整（measure 无 complete / 扫描未完成 / 只有 request）都不适用。
AGGREGATE_NARRATION_WORDS = ("聚合", "统计", "总计", "总量", "合计", "求和", "汇总", "总数")
ROW_LEVEL_MARKERS = ("逐项", "逐条", "逐行", "逐个", "逐一", "每一项", "每一行", "每一件",
                     "每个实例", "每个对象", "明细都", "完整明细", "完整清单", "完整列表",
                     "全部读完", "全部读取", "均已读取", "都已读取", "均已检查", "都已检查",
                     "均已核对", "都已核对", "均已确认", "都已确认")


def _licenses_aggregate_narration(clause: str, item: Mapping[str, Any], *, require_noun: bool = True) -> bool:
    """该子句是否只是在说“这个读数/聚合覆盖了全部 N 项”。

    `require_noun=True`（标签型量词）：子句必须点名聚合，否则“唯一/只有”
    这种排他断言会被聚合完整性放行。
    `require_noun=False`（数字型量词）：“基于全部 20 个匹配项计算”这类正确说法
    不一定含“聚合”二字，靠**逐行列举措辞**反过来排除行级断言。
    """
    if not item.get("aggregate_complete"):
        return False
    if any(marker in clause for marker in ROW_LEVEL_MARKERS):
        return False
    return any(word in clause for word in AGGREGATE_NARRATION_WORDS) if require_noun else True

# ---------------------------------------------------------------------------
# 否定 / 引号
# ---------------------------------------------------------------------------

# 宽窗口标记：出现在**同一子句**内的触发词之前，说明这句说的是"没有/不能断定"。
NEGATION = ("不能", "无法", "不足以", "并不", "并非", "不等于", "未读", "没读", "没有读",
            "不能据此", "不足以说明", "未确认", "无法确认", "无从", "不能直接", "未取", "未获得",
            # 实测（离线复演 v1.3 真实答案）：模型写的是“**也不代表**只有两件实物”，
            # 是一句正确的否定，却被量词检查误判成断言。这类“不代表/不意味着/而非”
            # 的否定形式必须认。
            "不代表", "不意味着", "不构成", "而非", "不是", "不能说明", "不应当", "不在",
            # 疑问语境不是断言：实测标题“## 能否直接算作“我们的物资””曾被误拒。
            "能否", "是否", "可否", "是不是", "算不算", "有没有",
            "cannot", "can not", "can't", "not ", "no evidence", "does not", "doesn't", "insufficient",
            "whether", "is it", "does not support", "not support", "no support", "fails to")

# 紧邻标记：中文否定常常直接贴在名词前（"没有殖民者"、"无殖民者"、"未见异常"）。
# 只在命中点前 8 个字符内检查，避免"无"这种单字在长句里过度抑制。
IMMEDIATE_NEGATION = ("没有", "无", "没", "未见", "未发现", "不存在")
# 单字否定前缀：**只在紧贴命中点时才**算否定。
# 实测（离线复演 v1.3 真实答案）：“地面范围含储存格中的物品、**不含**被持有内容”
# 里的“不含被持有”命中了“含被持有”的模式，是一句正确的否定。
# 只取紧贴一个字是为了不把“不少殖民者”这类反向表述误判成否定。
LEADING_NEGATORS = ("不", "没", "无", "非", "未", "别")

# 动词级否定：否定词与“包含/包/含”之间可能隔着一个“包”字，单字前缀看不见它。
# 实测（v1.10 真实答案）：`include_held=未包含持有物（false）` 里“含持有”命中触发词，
# 前面的“未包”却不被任何现有规则认作否定，正确的范围声明被误拒（1 次）。
# 只匹配“否定词 + 可选 包含/包/含”且**紧贴命中点**，不扩大其它类别的抑制窗口。
NEGATED_VERB_PREFIX = re.compile(r"(?:没有|不|未|没|无|非|别)(?:包含|包|含)?$")

# 子句分隔：句末标点 + 对比/转折 + 分号 + 逗号。切到逗号是有意的——
# 实测“是否属实无关紧要，这些黄金就是我们的物资。”曾靠逗号前的疑问词洗白整句。
CLAUSE_BREAK = "。！？!?\n，,、；;：:"
CONTRAST = ("但", "然而", "而是", "不过", "只是", "却")

# 引号内的文字往往是标题/引用，不是叙述断言。
QUOTE_SPANS = (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'))


def _inside_quotes(text: str, start: int, end: int) -> bool:
    for opening, closing in QUOTE_SPANS:
        position = text.find(opening)
        while position != -1 and position < start:
            close = text.find(closing, position + 1)
            if close == -1:
                break
            if position < start and end <= close:
                return True
            position = text.find(opening, close + 1)
    return False


def _clause_span(text: str, position: int) -> tuple[int, int]:
    """包含 position 的子句范围（句末标点、转折词、分号、逗号都算边界）。"""
    start = 0
    for index in range(position - 1, -1, -1):
        if text[index] in CLAUSE_BREAK or text[index] in CONTRAST:
            start = index + 1
            break
    end = len(text)
    for index in range(position, len(text)):
        if text[index] in CLAUSE_BREAK or text[index] in CONTRAST:
            end = index
            break
    return start, end


def _hit_is_negated(text: str, hit: re.Match[str]) -> bool:
    """**这一处**命中是否处在否定/引号语境里。

    必须逐处判定。旧版对整个模式判定（`_all_negated`），于是同一模式在一篇里出现
    两次、只否定一次时，**被否定的那一处也会被当成断言报出来**——实测真实答案
    “但这是部分列举，不能外推到全部 64 个部位”就是这样被误拒的。
    """
    if _inside_quotes(text, hit.start(), hit.end()):
        return True
    start, _ = _clause_span(text, hit.start())
    before = text[start:hit.start()]
    if any(marker in before for marker in NEGATION):
        return True
    if any(marker in before[-8:] for marker in IMMEDIATE_NEGATION):
        return True
    stripped = before.rstrip()
    if stripped and stripped[-1] in LEADING_NEGATORS:
        return True
    # 动词级否定（“未包含”“不包含”）：否定词后面还可以跟一个“包/包含/含”字。
    return bool(NEGATED_VERB_PREFIX.search(stripped))


def _all_negated(text: str, pattern: str) -> bool:
    """该模式的每一处出现都在否定/疑问语境里 → 视为没有做这个断言。

    只要求“存在一处未被否定的出现”就判定为断言，因此偏向“宁漏不误杀”——
    误杀会挡掉正确回答，代价更高。
    """
    hits = list(re.finditer(pattern, text))
    if not hits:
        return False
    return all(_hit_is_negated(text, hit) for hit in hits)


# ---------------------------------------------------------------------------
# 证据索引（v2：绑定到主语与集合，而不是整轮一个布尔袋）
# ---------------------------------------------------------------------------

_SUBJECT_KEYS = ("id", "pawn_id", "thing_id", "owner_id", "subject_id", "holder_id")
_LABEL_KEYS = ("name", "label", "full_name", "def_name", "title", "key")
_ROW_KEYS = ("rows", "values", "items", "records")
_MAX_NODES = 40_000
_MAX_DEPTH = 14


def _is_available(value: Any) -> bool:
    """读取失败/未捕获的字段不算"读到"。"""
    if isinstance(value, Mapping) and "state" in value:
        return value.get("state") not in ("unavailable", "not_read", "failed")
    return True


def _new_subject() -> dict[str, Any]:
    return {"fields": set(), "labels": set()}


def _population_signature(node: Mapping[str, Any], total: int | None) -> str | None:
    r"""一个结果节点的“总体”身份：同一个 from + 解析后 args + where + 总体规模。

    用于把**完整聚合**与**部分明细页**关联起来——实测（v1.10）两者是不同结果节点：
    模型先查一个只算聚合的请求，再查一个 limit=1 的明细页，两个节点 scope.args 完全相同。
    规模必须一致：不同 def / 不同范围的两次查询即使声明了同样的 args 也不是同一批对象，
    不能互相授权（回归 `test_another_results_complete_aggregate_cannot_license_this_one`）。
    签名缺失时不关联，退回逐节点判定。
    """
    scope = node.get("scope")
    if not isinstance(scope, Mapping):
        return None
    args = scope.get("args")
    if not isinstance(args, Mapping):
        return None
    try:
        return json.dumps([node.get("from"), dict(args), scope.get("where"), total],
                          sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def build_read_index(observations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """把这一轮的观察压成**按主语绑定**的证据索引。

    returns::

        {
          "global":  {"fields": set(), "labels": set()},   # 无主语的顶层事实
          "subjects": {subject_id: {"fields": set(), "labels": set()}},
          "labels":  {label: subject_id},
          "collections": [{"total", "returned", "complete", "labels"}],
          "flags": {"include_held_true": bool, "include_fogged_true": bool},
        }

    只认真正出现在工具结果里的事实：读取失败/`unavailable` 的字段不计入。
    `request` 子树被**整体跳过**——旧版会遍历 `request.arguments`，于是一个失败的
    请求只要写了 `include_held: true` 就能为"含被持有"授权（实测漏过）。
    """
    index: dict[str, Any] = {
        "global": _new_subject(),
        "subjects": {},
        "labels": {},
        "collections": [],
        # 这一轮读过哪些 root。用于 `identity_claim`：把“殖民者”当作**人口名词**
        # 使用时是合法的，但只有当本轮真的读过 `spawned_free_colonists` 才成立。
        # 实测：105 份已复核答案里 24 条被误拒，全是因为只看名词、不看读过什么 root。
        "roots": set(),
        "flags": {"include_held_true": False, "include_fogged_true": False},
    }
    budget = [_MAX_NODES]

    def entry(subject: str | None) -> dict[str, Any]:
        if subject is None:
            return index["global"]
        return index["subjects"].setdefault(subject, _new_subject())

    def note(subject: str | None, key: str, value: Any) -> None:
        bucket = entry(subject)
        if _is_available(value):
            bucket["fields"].add(key)
        if key == "from" and isinstance(value, str) and value:
            index["roots"].add(value)
        if key == "include_held" and value is True:
            index["flags"]["include_held_true"] = True
        if key == "include_fogged" and value is True:
            index["flags"]["include_fogged_true"] = True
        if key == "visible_only" and value is False:
            index["flags"]["include_fogged_true"] = True

    def note_label(subject: str | None, value: Any) -> None:
        if isinstance(value, str) and len(value) >= 2:
            entry(subject)["labels"].add(value)
            if subject is not None:
                index["labels"].setdefault(value, subject)

    def collection(node: Mapping[str, Any], subject: str | None) -> None:
        rows = None
        for key in _ROW_KEYS:
            value = node.get(key)
            if isinstance(value, list):
                rows = value
                break
        aggregates = node.get("aggregates")
        if rows is None and not isinstance(aggregates, Mapping):
            return
        total = node.get("matched")
        if total is None:
            total = node.get("matched_count")
        if total is None:
            total = node.get("known_matches")
        total = total if isinstance(total, int) else None
        measures = aggregates if isinstance(aggregates, Mapping) else {}
        # 聚合覆盖是**另一个问题**：发布过的返回契约说明，扫描与声明的度量
        # 完整时，聚合就是精确值，即使明细页只展示了一部分。
        aggregate_complete = (
            bool(measures) and node.get("scan_complete") is True
            and all(isinstance(measure, Mapping) and measure.get("complete") is True
                    for measure in measures.values())
        )
        signature = _population_signature(node, total)
        if rows is None:
            # 只算聚合的结果节点没有明细键（limit 0）。它不构成“部分列举”，
            # 但必须记录，否则它的聚合完整性就丢了——实测（v1.10）就是这种节点
            # 与明细页分属两个结果，导致完整聚合被当成不完整明细。
            index["collections"].append({
                "total": total,
                "returned": 0,
                "complete": True,
                "aggregate_complete": bool(aggregate_complete),
                "signature": signature,
                "labels": set(),
                "subject": subject,
            })
            return
        page = node.get("page") if isinstance(node.get("page"), Mapping) else {}
        has_more = page.get("has_more")
        if has_more is None:
            has_more = node.get("has_more")
        offset = page.get("offset", 0)
        if type(offset) is not int or offset < 0:
            offset = 0
        listing = node.get("listing")
        # 明细“读到哪儿”：只有**首页且没有下一页**才是整份列举。
        # 实测漏过：非零 offset 的末页（has_more=false）被当成完整列举，
        # 于是“所有 20 个实例都已逐项检查”在只看到最后一页时被放行。
        complete = offset == 0 and ((has_more is False) or (listing == "complete"))
        if has_more is True or listing in ("partial", "incomplete"):
            complete = False
        labels: set[str] = set()
        for row in rows:
            if isinstance(row, Mapping):
                for label_key in _LABEL_KEYS:
                    value = row.get(label_key)
                    if isinstance(value, str) and len(value) >= 2:
                        labels.add(value)
            elif isinstance(row, str) and len(row) >= 2:
                labels.add(row)
        index["collections"].append({
            "total": total,
            "returned": len(rows),
            "complete": bool(complete),
            "aggregate_complete": bool(aggregate_complete),
            "signature": signature,
            "labels": labels,
            "subject": subject,
        })

    def walk(node: Any, subject: str | None, depth: int) -> None:
        if budget[0] <= 0 or depth > _MAX_DEPTH:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, subject, depth + 1)
            return
        if not isinstance(node, Mapping):
            return
        budget[0] -= 1
        local = subject
        for key in _SUBJECT_KEYS:
            value = node.get(key)
            if isinstance(value, str) and len(value) >= 3:
                local = value
                entry(value)
                break
        collection(node, local)
        for key, value in node.items():
            if key == "request":
                # 请求参数不是观察结果：失败的请求不构成证据。
                continue
            note(local, str(key), value)
            if key in _LABEL_KEYS:
                note_label(local, value)
            walk(value, local, depth + 1)

    walk(list(observations), None, 0)
    # 聚合完整性与明细完整性可能来自**不同的结果节点**（先查聚合，再查明细页）。
    # 返回契约按“同一总体”判定，所以把同一 from/args/where 的完整聚合关联到
    # 该总体的所有集合条目上。不同 def/范围 → 不同签名 → 不会互相授权。
    complete_populations = {
        item["signature"] for item in index["collections"]
        if item.get("aggregate_complete") and item.get("signature") is not None
    }
    if complete_populations:
        for item in index["collections"]:
            if item.get("signature") in complete_populations:
                item["aggregate_complete"] = True
    index["flags"]["colonists_root"] = bool(
        index["roots"] & {"spawned_free_colonists", "colonists", "colonist"})
    return index


def observed_categories(observations: Iterable[Mapping[str, Any]],
                        index: Mapping[str, Any] | None = None) -> dict[str, bool]:
    """扁平证据视图，保留旧接口。

    新代码应使用 :func:`build_read_index`：这个视图会丢掉主语绑定，只适合
    兼容调用方与结构自检。
    """
    idx = dict(index) if index is not None else build_read_index(observations)
    found = dict(identity=False, faction=False, held_by_faction=False,
                 health_conditions=False, health_body_parts=False, health_capacities=False,
                 include_held_true=bool(idx["flags"]["include_held_true"]),
                 include_fogged_true=bool(idx["flags"]["include_fogged_true"]),
                 colonists_root=bool(idx["flags"]["colonists_root"]))
    buckets = [idx["global"], *idx["subjects"].values()]
    for bucket in buckets:
        fields = bucket["fields"]
        if fields & {"is_player", "is_free_colonist", "is_colonist"}:
            found["identity"] = True
        if fields & {"is_player", "faction"}:
            found["faction"] = True
        if fields & {"held_by_faction", "held_by"}:
            found["held_by_faction"] = True
        for name in ("conditions", "body_parts", "capacities"):
            if name in fields:
                found["health_" + name] = True
    return found


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------

# 证据键 → 索引里实际记录的字段名。索引记的是工具结果里的原始键，
# 而 CLAIM_CLASSES 用的是领域词汇（例如 health_conditions 对应嵌套的 `conditions`）。
_EVIDENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "identity": ("is_player", "is_free_colonist", "is_colonist"),
    "faction": ("faction", "is_player"),
    "held_by_faction": ("held_by_faction", "held_by"),
    "health_conditions": ("conditions",),
    "health_body_parts": ("body_parts",),
    "health_capacities": ("capacities",),
}


def _subjects_for(index: Mapping[str, Any], label: str) -> set[str | None]:
    """满足某条证据键的主语集合；`None` 代表无主语的顶层事实。"""
    hits: set[str | None] = set()
    if index["flags"].get(label):
        hits.add(None)
    wanted = _EVIDENCE_FIELDS.get(label, (label,))
    buckets = [(None, index["global"]), *index["subjects"].items()]
    for name, bucket in buckets:
        if any(field in bucket["fields"] for field in wanted):
            hits.add(name)
    return hits


def _mentioned_subjects(text: str, index: Mapping[str, Any]) -> set[str]:
    mentioned = set()
    for key in index["subjects"]:
        if len(key) >= 3 and key in text:
            mentioned.add(key)
    for label, key in index["labels"].items():
        if label in text:
            mentioned.add(key)
    return mentioned


def _evidence_present(index: Mapping[str, Any], evidence: Sequence[str], text: str) -> bool:
    """断言的主体是否真的被读过对应证据。

    规则（v2）：证据必须绑定到**这句话的主语**。
      * 句子点名了某个对象 → 只认该对象的读取记录（实测：读 Pawn9 的健康
        不能授权“Pawn1 完全健康”）。
      * 句子没有点名 → 只认无主语的顶层事实（实测：读 Pawn1 的 faction
        不能授权“这 5 名殖民者全部属于玩家派系”）。

    例外：**范围标志是整轮的，不是主语的**。`include_held`/`include_fogged` 描述的
    是这一轮读取的覆盖范围；实测把“含被持有”（这是模型对本次口径的正确声明）
    因为句子里提到了某个对象而误拒，就是因为把全局标志也当成了主语证据。
    """
    if not evidence:
        return False
    for name in evidence:
        if index["flags"].get(name):
            return True
    # 范围标志与 root 名称是整轮的，不绑定主语。
    mentioned = _mentioned_subjects(text, index)
    if mentioned:
        for name in evidence:
            hits = _subjects_for(index, name)
            if any(subject in hits for subject in mentioned):
                return True
        return False
    return any(None in _subjects_for(index, name) for name in evidence)


def _cited_numbers(clause: str) -> set[int]:
    r"""子句里真正作为测量被写出来的数字。

    排除标识符里的数字：`Thing_Human465` 的 465 不是被引用的读数（实测
    “目前唯一受损器官是右眼” 的判定期里混进了 465）。

    边界只排除 **ASCII 标识符字符**，不能用 `\w`：中文汉字在 `\w` 里也算单词字符，
    于是“全部20个”的数字根本没被提取。实测漏过：`所有20实例都已逐项检查`
    因此完全没有进入量词检查。
    """
    return {int(value) for value in re.findall(r"(?<![A-Za-z0-9_])\d+", clause)}


def _quantifier_rejections(text: str, index: Mapping[str, Any]) -> list[dict]:
    """全称/排他量词必须能拿出“整个集合都被读过”的依据。

    这是**确定性**检查，不是语义判断：Mod 的返回信封里已经带着
    `page.has_more` / `matched` / `listing`。两条都很窄：

      * 量词与一个数字**紧邻**，且该数字等于某个 partial 集合的 total；或
      * 子句点名了某个 partial 集合里**已返回的行标签**

    唯一豁免：子句只是在陈述“聚合覆盖了全部 N 项”，且该集合的聚合确实完整
    （见 `_licenses_aggregate_narration`）。

    这个宽度是实测调出来的：第一版用裸词（“全部/所有/唯一”）加“存在 partial 即拒”，
    在 105 份已复核答案上误报 49 条（其中 0 条复核有 issue）。
    """
    partial = [item for item in index["collections"] if not item["complete"]]
    if not partial:
        return []
    rejected: list[dict] = []
    reported: set[tuple[int, int]] = set()

    def record(hit: re.Match[str], item: Mapping[str, Any], reason: str) -> None:
        start, end = _clause_span(text, hit.start())
        if (start, end) in reported:
            return
        reported.add((start, end))
        total = item["total"]
        shape = (f"matched={total}, returned={item['returned']}"
                 if total is not None else f"returned={item['returned']}")
        rejected.append(dict(
            code="incomplete_quantifier_claim",
            label="对不完整集合使用全称/排他量词",
            quote=hit.group(0),
            hint=(f"本轮有一次读取只返回了部分结果（{shape}，listing partial）：{reason}。"
                  f"请先读完，或把措辞改成“已返回的 {item['returned']} 项中……”并说明总数未完整列举。"),
        ))

    def first_citing(cited: set[int], clause: str) -> Mapping[str, Any] | None:
        for item in partial:
            if isinstance(item["total"], int) and item["total"] in cited:
                return item
        for item in partial:
            if any(label in clause for label in item["labels"]):
                return item
        return None

    for pattern in QUANTIFIER_TOTAL_PATTERNS:
        for hit in re.finditer(pattern, text):
            if _hit_is_negated(text, hit):
                continue
            start, end = _clause_span(text, hit.start())
            clause = text[start:end]
            # 只看量词附近的数字：远处出现同一个数字不算数这个集合。
            local = _cited_numbers(hit.group(0))
            item = next((c for c in partial if isinstance(c["total"], int) and c["total"] in local), None)
            if item is None:
                continue
            if _licenses_aggregate_narration(clause, item, require_noun=False):
                continue
            record(hit, item, f"这句话用“{hit.group(0)}”数了一个只返回了 {item['returned']} 项的集合")

    for pattern in QUANTIFIER_LABEL_PATTERNS:
        for hit in re.finditer(pattern, text):
            if _hit_is_negated(text, hit):
                continue
            start, end = _clause_span(text, hit.start())
            clause = text[start:end]
            item = next((c for c in partial if any(label in clause for label in c["labels"])), None)
            if item is None:
                continue
            if _licenses_aggregate_narration(clause, item):
                continue
            record(hit, item, f"这句话把只返回了 {item['returned']} 项的集合当成已知全体")
    return rejected


def check_claims(answer, detail=None, *, observations=(), categories=None,
                 index: Mapping[str, Any] | None = None) -> list[dict]:
    """返回被拒绝的断言列表；空列表表示通过。

    只在**证据确实缺失**时拒绝；证据存在时一律放行（不做语义判断）。
    """
    text = _text(answer, detail)
    if not text.strip():
        return []
    idx = dict(index) if index is not None else build_read_index(observations)
    if categories is not None:
        # 兼容旧调用方：把扁平视图折回索引的 global 桶。
        flat = {name: bool(value) for name, value in categories.items()}
        boot = dict(idx["global"])
        boot["fields"] = set(boot["fields"]) | {
            name for name in ("identity", "faction", "held_by_faction",
                              "health_conditions", "health_body_parts", "health_capacities")
            if flat.get(name)
        }
        idx["global"] = boot
        idx["flags"]["include_held_true"] = flat.get("include_held_true", idx["flags"]["include_held_true"])
        idx["flags"]["include_fogged_true"] = flat.get("include_fogged_true", idx["flags"]["include_fogged_true"])
        for name in ("identity", "faction"):
            if flat.get(name) and not idx["global"]["fields"]:
                idx["global"]["fields"].add(name)

    rejected = _quantifier_rejections(text, idx)
    for claim in CLAIM_CLASSES:
        hit = None
        for pattern in claim["patterns"]:
            # 必须找**第一处未被否定的**出现。旧版只在“全部出现都被否定”时才跳过，
            # 然后用 `re.search` 报出**第一处**（可能正是被否定的那处）。实测真实答案
            # “两者都不是殖民者范围内匹配到的对象” + 另一处未否定的“殖民者”
            # 因此被误拒。
            hit = next((h for h in re.finditer(pattern, text) if not _hit_is_negated(text, h)), None)
            if hit is not None:
                break
        if hit is None:
            continue
        # 证据必须绑定到**命中所在的子句**，不是整篇。用整篇算“被提及的主体”时，
        # 只要同篇里提过某个已读对象（或者只是列了一堆行 id），就能为另一句里
        # 另一个对象的断言授权——实测跨主体串证：`Alice 的身份已读取。Bob 是殖民者。`
        # 被放行。反向也会误伤：建筑题的明细里到处都是 Thing_* 行 id，
        # 而“按阵营筛选属于玩家阵营的实例为 0”的子句里并没有对象名。
        start, end = _clause_span(text, hit.start())
        if not claim.get("always_reject") and _evidence_present(idx, claim["evidence"] or (), text[start:end]):
            continue
        rejected.append(dict(code=claim["code"], label=claim["label"], quote=hit.group(0), hint=claim["hint"]))
    return rejected


def _text(answer: Sequence[Mapping[str, Any]] | str, detail: str | None) -> str:
    if isinstance(answer, str):
        return answer + ("\n" + (detail or ""))
    parts = []
    for part in answer or ():
        if isinstance(part, Mapping) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts) + ("\n" + (detail or ""))


def describe(rejected: Iterable[Mapping[str, Any]]) -> str:
    """把拒绝原因写成模型可一次修正的说明。"""
    lines = []
    for item in rejected:
        lines.append(f"{item['label']}（“{item['quote']}”）缺少本轮证据。{item['hint']}")
    return " ".join(lines)


__all__ = ["CLAIM_CLASSES", "build_read_index", "check_claims",
           "describe", "observed_categories"]
