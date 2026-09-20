"""v1.9 语义工具表面：模型只传封闭枚举，程序生成 blocks 并复用 Surface.compile。

模型可见 2 个工具：

    look(object, aspects, detail?)      # 读对象事实
    count(object, metric, by?, detail?) # 计量

object 的选择器（kind/name/names/ids/definition/category/map/include_held/include_fogged）全部是
封闭枚举或精确值。aspects→原生字段包、metric→aggregate、by→group_by、kind→原生 root 的
映射只存在于本模块；模型无法传字段名、路径、原生参数键或不透明 handle。

结果全部明文内联：scope、coverage、not_read、语义说明。不使用 _meaning/_measure/
_unavailable/provenance 这类代号，也不把 unknown 落成 0/null。
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field

from .query_surface import SurfaceError

# --- 封闭枚举 -----------------------------------------------------------------

ROOTS = {
    'game': 'game',
    'maps': 'maps',
    'map': 'map',
    'colony': 'colony',
    'pawn': 'pawn',
    'pawns': 'pawns',
    'colonists': 'spawned_free_colonists',
    'thing': 'thing',
    'things': 'things',
    'thing_def': 'thing_def',
    'thing_defs': 'thing_defs',
}
KINDS = tuple(ROOTS)
ASPECTS = ('identity', 'health', 'skills', 'gear', 'work', 'mood', 'profile',
           'resources', 'quantity', 'distribution')
METRICS = ('units', 'records')
GROUPINGS = ('definition', 'category', 'holder', 'map')

# object 是两工具共用的封闭结构；每个 kind 只接受其适用的选择器。
OBJECT_SELECTORS = {
    'game': (),
    'maps': ('ids', 'names'),
    'map': ('ids', 'names'),
    'colony': (),
    'pawn': ('ids', 'names'),
    'pawns': ('ids', 'names', 'map'),
    'colonists': ('ids', 'names', 'map'),
    'thing': ('ids', 'names', 'map'),
    'things': ('names', 'definition', 'category', 'map', 'include_held', 'include_fogged'),
    'thing_def': ('definition',),
    'thing_defs': ('definition', 'category'),
}
# 主语寻址：不管模型要哪个 aspect，事实都必须能说清楚"这是谁/这是哪个"。
# 少了这一层，就会出现「数字读对了、但没说清主语」的典型失败。
SUBJECT_FIELDS = {
    'game': (),
    'colony': (),
    'maps': ('id', 'name'),
    'map': ('id', 'name'),
    'pawn': ('id', 'name', 'map_id'),
    'pawns': ('id', 'name', 'map_id'),
    'colonists': ('id', 'name', 'map_id'),
    'thing': ('id', 'definition.def_name', 'definition.label', 'map_held.id'),
    'things': ('id', 'definition.def_name', 'definition.label', 'map_held.id'),
    'thing_def': ('def_name', 'label'),
    'thing_defs': ('def_name', 'label'),
}

POPULATIONS = ('maps', 'pawns', 'colonists', 'things', 'thing_defs')
PAWN_KINDS = ('pawn', 'pawns', 'colonists')
THING_KINDS = ('thing', 'things')
DEF_KINDS = ('thing_def', 'thing_defs')
MAP_KINDS = ('map', 'maps')
# 多候选会落入一个**总体**根并逐个返回的 kind：pawn → pawns，thing → things。
# 只有 `map` 没有总体根，路由会直接取 ids[0]，所以它必须由歧义守卫拦住。
SINGLE_ONLY_KINDS = ('map',)

# 程序侧字段包：模型永远看不到这些原生路径。
# 身份事实：原生派系成员关系与殖民者状态。这是"这是谁的人"的直接答案，
# 不从种类、名字或容器关系推断。
FACTION_FIELDS = ['faction.def_name', 'faction.name', 'faction.is_player', 'faction.player_relation']
# 物品归属链：谁持有它、那个持有者的派系。沿真实持有关系合成，不是所有权推断。
HOLDER_FIELDS = ['held_by.id', 'held_by.runtime_type',
                 'held_by_faction.def_name', 'held_by_faction.is_player', 'held_by_faction.player_relation']
PAWN_IDENTITY_FIELDS = ['is_free_colonist', 'is_colonist', 'is_prisoner_of_colony', 'is_slave_of_colony',
                        'guest_status']
IDENTITY_FIELDS = {
    'game': ['paused', 'current_map_id'],
    'maps': ['id', 'name', 'is_current', 'is_player_home', 'colonist_count', 'has_visible_threat'],
    'map': ['id', 'name', 'is_current', 'is_player_home', 'colonist_count', 'has_visible_threat'],
    'pawn': ['id', 'name', 'map_id'] + PAWN_IDENTITY_FIELDS + FACTION_FIELDS,
    'pawns': ['id', 'name', 'map_id'] + PAWN_IDENTITY_FIELDS + FACTION_FIELDS,
    'colonists': ['id', 'name', 'map_id'] + PAWN_IDENTITY_FIELDS + FACTION_FIELDS,
    'thing': ['id', 'runtime_type', 'label', 'spawned', 'destroyed', 'definition.def_name',
              'definition.label', 'map_held.id'] + FACTION_FIELDS + HOLDER_FIELDS,
    'things': ['id', 'runtime_type', 'label', 'spawned', 'destroyed', 'definition.def_name',
               'definition.label', 'map_held.id'] + FACTION_FIELDS + HOLDER_FIELDS,
    'thing_def': ['def_name', 'label', 'thing_class', 'category'],
    'thing_defs': ['def_name', 'label', 'thing_class', 'category'],
}
# count/look 明细行的最小身份列（仅为把读数对齐到对象）。
LISTING_FIELDS = {
    'game': ['paused'],
    'maps': ['id', 'name'],
    'map': ['id', 'name'],
    'colonists': ['id', 'name', 'map_id'],
    # v1.10 capability repair: native item durability belongs to generic detail,
    # not a task-specific extra read. Null/unavailable still use the shared pack.
    # 实测（v1.10）：只给不透明的 `parent_holder.ref`，模型把“共享同一个父引用”读成
    # “在同一个容器里”，而真相是两堆白银都由**地图**持有（spawned）。父对象的原生类型
    # 才能区分 地图 / 人物库存 / 容器 / 建筑；`spawned` 直接回答“在不在图上”。
    'things': ['id', 'spawned', 'stack_count', 'hit_points', 'max_hit_points',
               'definition.def_name', 'definition.label', 'map_held.id',
               'parent_holder.runtime_type'],
    'thing_defs': ['def_name', 'label', 'category'],
}
# by → 原生分组路径（按 root 类型区分）。
GROUP_PATHS = {
    'things': {'definition': 'definition.def_name', 'category': 'definition.category',
               'holder': 'parent_holder.ref', 'map': 'map_held.id'},
    'colonists': {'map': 'map_id'},
    # `pawns` 的 map 分组已在 BY_KINDS 里公开允许，映射表却漏了这一项，
    # 合法调用会直接抛 KeyError。
    'pawns': {'map': 'map_id'},
    'thing_defs': {'category': 'category'},
    'maps': {},
}
BY_KINDS = {
    'things': ('definition', 'category', 'holder', 'map'),
    'pawns': ('map',),
    'colonists': ('map',),
    'maps': (),
    'thing_defs': ('category',),
}
# look 的 distribution 默认分组。
DEFAULT_GROUP = {'things': ('definition',), 'colonists': ('map',), 'thing_defs': ('category',)}

# 必须明文内联的语义句子（来自 Mod scope/字段说明；不能被压成代号）。
SEMANTICS = {
    'containment': 'Holder/container membership selects current containment; it is not faction or property '
                   'ownership, and it does not establish usable, tradable or colony property.',
    'pawn': 'Pawn includes humans, animals and mechanoids regardless of faction; Pawn is not a human, colonist '
            'or ownership identity.',
    'health': 'health_percent is "Game summaryHealth.SummaryHealthPercent: an overall game-computed health '
              'indicator. Report it separately from conditions and body-part HP; this read is not a '
              'blood-volume measurement. A value of 1 does not establish a cause or a general rule about '
              'which conditions affect it." A value of 1 is therefore not "fully healthy".',
    'holder': 'held_by is the nearest containing Pawn (containment, not ownership); null means no Pawn holds this '
              'instance. held_by_faction is that Pawn\'s faction: null means the holder has no faction (or there is no '
              'holder) - it does NOT mean the read failed. Use held_by_faction.is_player instead of inferring '
              '"ours" from containment.',
    'unknown': 'Unknown is not zero and not empty: an unavailable read, an incomplete scan and a legitimate zero '
               'or empty collection are different facts.',
    'lower_bound': 'An incomplete scan yields a lower bound (or unavailable), never an exact total.',
    'units': 'records counts matching objects; units sums stack_count of those objects. A record is not '
             'necessarily an item or a human.',
    # 父持有者必须能区分“地图”与“容器”，否则一个共享的不透明引用会被读成“同一个箱子”。
    'parent_holder': 'parent_holder is this instance\'s immediate container, named by its native type: '
                     'Verse.Map means the instance is spawned on that map rather than inside any container; '
                     'a tracker/owner type (for example Verse.Pawn_InventoryTracker or Verse.ThingOwner) or a '
                     'building type means it is inside that holder. Two instances sharing a parent_holder have '
                     'the same immediate holder - containment, not ownership, and it never proves a location '
                     'beyond what that type states.',
    'conditions_scope': 'health.conditions is the hediff record: injuries, diseases, scars, implants and '
                        'addictions. It is read with hidden records included, not only the entries the health '
                        'interface lists, so a short or empty list here is the whole record and not a visible '
                        'subset. Body-part HP and capacity values are not this record.',
    'durability': 'hit_points/max_hit_points are the native Thing getters for this instance. A null with no '
                  'issue at its path is the game returning no value for this member (some definitions carry no '
                  'durability); that is neither zero nor a failed read. An issue at the path means the read failed.',
}

# 接口说明：只说工具怎么用，不包含语义提醒。
# study 臂在共享的 Architecture prompt 前缀之后只接这一段——语义提醒由工具结果内联给出，
# 不靠额外提示词奖励，否则就不是接口对比而是提示词对比了。
TOOL_GUIDE = (
    'Read-only game evidence through two semantic tools. You never write field names, paths or native '
    'arguments; the program routes kind/aspects/metric/by to the native query.\n'
    'look(object, aspects, detail?) reads facts about one object or a population; aspects are a closed set: '
    'identity, health, skills, gear, work, mood, profile, resources, quantity, distribution.\n'
    'count(object, metric, by?, detail?) measures a population: metric units sums stack_count, records counts '
    'objects; by groups the result.\n'
    'object selectors are closed: kind, exact name/names or ids, exact definition, category, and scope map '
    '(default current), include_held (default true = also count things inside holders), include_fogged (default true = also '
    'include things under fog). Both flags are the same polarity: true means more inclusive, false means narrower. An exact display name that '
    'matches more than one object returns candidates; never assume one.\n'
    'detail is the number of detail rows: 0 means counts only. With by given, detail caps the listed groups '
    '(0 = all groups up to the display bound).\n'
    'Every result states scope, coverage and not_read plainly. Read them before interpreting facts.'
)

# 独立使用（不挂 Architecture）时的语义提醒。
SEMANTIC_REMINDERS = (
    'Unknown is not zero or empty; containment is not ownership; Pawn includes humans, animals and '
    'mechanoids. health_percent cannot be read as "fully healthy". An incomplete scan is a lower bound, '
    'not a total.'
)

INTRO = TOOL_GUIDE + '\n' + SEMANTIC_REMINDERS + '\nFinish with submit_response. Do not invent facts the tools did not return.'

FINISH = ('Finish with submit_response: answer:[{text:brief conclusion}], detail for required full findings. '
          'Answer is at most 180 tokens, combined text at most 12000 characters. Preserve scope and unknowns; '
          'fix only rejected fields.')


# --- schema -------------------------------------------------------------------

def _object_schema():
    return {
        'type': 'object',
        'description': 'Closed object selector. No field names, paths or native argument keys are accepted.',
        'properties': {
            'kind': {'type': 'string', 'enum': list(KINDS)},
            'name': {'type': 'string', 'description': 'Exact display name; ambiguity returns candidates, never a pick.'},
            'names': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Exact display names.'},
            'ids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Exact native object IDs.'},
            'definition': {'type': 'string', 'description': 'Exact def_name or localized definition label.'},
            'category': {'type': 'string', 'description': 'Definition category, e.g. Building.'},
            'map': {'type': 'string', 'default': 'current'},
            'include_held': {'type': 'boolean', 'default': True,
                             'description': 'true = also count/count things inside holders (inventory, containers, equipment). '
                                            'false = only things spawned on the map. The question "including what people carry" '
                                            'requires true; the default is true, so only pass false when the caller explicitly '
                                            'asks for ground/map objects alone.'},
            'include_fogged': {'type': 'boolean', 'default': True,
                               'description': 'true = also include things under fog of war. false = only currently visible things. '
                                              'The default is true, so only pass false when the caller explicitly asks for what is visible.'},
        },
        'required': ['kind'],
        'additionalProperties': False,
    }


def tool_schemas():
    """模型可见工具集：只有 look 与 count（submit_response 由 handoff 提供）。"""
    return [
        dict(name='look',
             description=('Read one object or a population through closed aspects: '
                          'identity health skills gear work mood profile resources quantity distribution. '
                          'No field names or native arguments; the program routes them. '
                          'detail is the number of detail rows (0 = counts only). '
                          'Results inline scope, coverage, not_read and meanings; unknown is not zero.'),
             parameters={'type': 'object',
                         'properties': {'object': _object_schema(),
                                        'aspects': {'type': 'array', 'minItems': 1,
                                                    'items': {'type': 'string', 'enum': list(ASPECTS)}},
                                        'detail': {'type': 'integer', 'minimum': 0, 'maximum': 100, 'default': 0}},
                         'required': ['object', 'aspects'], 'additionalProperties': False}),
        dict(name='count',
             description=('Measure a population. metric units sums stack_count; records counts objects. '
                          'by groups the measurement by definition, category, holder or map. '
                          'detail is the number of raw rows (0 = aggregate only); with by it caps listed groups. '
                          'For kind things each detail row gives the instance id, spawned, stack_count, '
                          'current/max hit_points and the immediate parent_holder type. '
                          'The aggregate is complete or explicitly a lower bound/unavailable, never silently zero.'),
             parameters={'type': 'object',
                         'properties': {'object': _object_schema(),
                                        'metric': {'type': 'string', 'enum': list(METRICS)},
                                        'by': {'type': 'string', 'enum': list(GROUPINGS)},
                                        'detail': {'type': 'integer', 'minimum': 0, 'maximum': 100, 'default': 0}},
                         'required': ['object', 'metric'], 'additionalProperties': False}),
    ]


# --- 校验 ---------------------------------------------------------------------

def _text(value, name):
    if not isinstance(value, str) or not value:
        raise SurfaceError(name + ' must be a nonempty string')
    return value


def _texts(value, name):
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise SurfaceError(name + ' must be a nonempty string array')
    return list(value)


def normalize_object(raw):
    """把模型输入变成规范化 object；未知 kind/选择器/类型一律拒绝。"""
    if not isinstance(raw, dict):
        raise SurfaceError('object must be an object')
    unknown = sorted(set(raw) - {'kind', 'name', 'names', 'ids', 'definition', 'category',
                                 'map', 'include_held', 'include_fogged'})
    if unknown:
        raise SurfaceError('object does not accept: ' + ', '.join(unknown))
    kind = raw.get('kind')
    if kind not in ROOTS:
        raise SurfaceError('kind must be one of: ' + ', '.join(KINDS))
    if 'name' in raw and 'names' in raw:
        raise SurfaceError('use either name or names, not both')
    if 'ids' in raw and ('name' in raw or 'names' in raw):
        raise SurfaceError('use exact ids or an exact display name, not both')
    allowed = OBJECT_SELECTORS[kind]
    for key in ('name', 'names', 'ids', 'definition', 'category', 'map', 'include_held', 'include_fogged'):
        if key in raw and key not in allowed and not (key == 'name' and 'names' in allowed):
            raise SurfaceError('kind ' + kind + ' does not accept ' + key)
    obj = {'kind': kind}
    if 'name' in raw:
        obj['names'] = [_text(raw['name'], 'name')]
    if 'names' in raw:
        obj['names'] = _texts(raw['names'], 'names')
    if 'ids' in raw:
        obj['ids'] = _texts(raw['ids'], 'ids')
    if 'definition' in raw:
        obj['definition'] = _text(raw['definition'], 'definition')
    if 'category' in raw:
        obj['category'] = _text(raw['category'], 'category')
    if 'map' in raw:
        obj['map'] = _text(raw['map'], 'map')
    # 两个范围标志同向：true = 包含更多。旧名 held/visible_only 极性相反、且未说明 false 的
    # 含义，实测导致模型写出 held=false 却把结果描述成"含被持有"（真实值 74，报 0）。
    for key in ('include_held', 'include_fogged'):
        if key in raw:
            if type(raw[key]) is not bool:
                raise SurfaceError(key + ' must be a boolean')
            obj[key] = raw[key]
    if 'definition' in obj and 'category' in obj:
        raise SurfaceError('use either definition or category, not both')
    if kind in ('colonists', 'things', 'pawns'):
        obj.setdefault('map', 'current')
    if kind == 'things':
        obj.setdefault('include_held', True)
        obj.setdefault('include_fogged', True)
    if 'ids' in obj:
        if kind in ('pawn', 'thing', 'map') and len(obj['ids']) != 1:
            raise SurfaceError('kind ' + kind + ' reads exactly one id')
    return obj


def validate_aspects(kind, aspects):
    if not isinstance(aspects, list) or not aspects or not all(isinstance(x, str) for x in aspects):
        raise SurfaceError('aspects must be a nonempty string array')
    supported = supported_aspects(kind)
    for aspect in aspects:
        if aspect not in ASPECTS:
            raise SurfaceError('unknown aspect: ' + aspect)
        if aspect not in supported:
            raise SurfaceError('kind ' + kind + ' supports: ' + ', '.join(supported))
    return tuple(dict.fromkeys(aspects))


def validate_metric(kind, metric):
    if metric not in METRICS:
        raise SurfaceError('metric must be one of: ' + ', '.join(METRICS))
    if kind not in POPULATIONS:
        raise SurfaceError('count measures a population: ' + ', '.join(POPULATIONS) + ' (got ' + kind + ')')
    if metric == 'units' and kind != 'things':
        raise SurfaceError('units sums stack_count and is only defined for kind things')
    return metric


def validate_by(kind, by):
    if by is None:
        return None
    if by not in GROUPINGS:
        raise SurfaceError('by must be one of: ' + ', '.join(GROUPINGS))
    if by not in BY_KINDS[kind]:
        allowed = BY_KINDS[kind] or ('nothing',)
        raise SurfaceError('kind ' + kind + ' cannot group by ' + by + '; allowed: ' + ', '.join(allowed))
    return by


def validate_detail(detail):
    if type(detail) is not int or not 0 <= detail <= 100:
        raise SurfaceError('detail must be an integer 0..100')
    return detail


def supported_aspects(kind):
    if kind in PAWN_KINDS:
        base = ['identity', 'health', 'skills', 'gear', 'work', 'mood', 'profile']
    elif kind in MAP_KINDS:
        base = ['identity', 'resources']
    else:
        base = ['identity']
    if kind in POPULATIONS:
        base = base + ['quantity']
        if kind in DEFAULT_GROUP:
            base = base + ['distribution']
    return tuple(base)


# --- 原生路由 -----------------------------------------------------------------

def route_object(obj):
    """kind + 精确选择器 → (native root, args, where)。"""
    kind = obj['kind']
    args, where = {}, None
    if kind in ('game', 'colony'):
        return ROOTS[kind], args, where
    if kind == 'maps':
        if 'ids' in obj:
            args['ids'] = list(obj['ids'])
        return ROOTS[kind], args, where
    if kind == 'map':
        args['id'] = obj['ids'][0] if 'ids' in obj else 'current'
        return ROOTS[kind], args, where
    if kind in ('pawn', 'pawns'):
        if kind == 'pawn' and 'ids' in obj and len(obj['ids']) == 1:
            args['id'] = obj['ids'][0]
            return 'pawn', args, where
        # pawns 覆盖人类/动物/机械族；pawn 多 id 时也落到同一根，避免伪造单 id。
        args['map_id'] = obj.get('map', 'current')
        if 'ids' in obj:
            where = {'id': {'in': list(obj['ids'])}}
        return 'pawns', args, where
    if kind == 'colonists':
        if 'ids' in obj:
            args['ids'] = list(obj['ids'])
        args['map_id'] = obj.get('map', 'current')
        return ROOTS[kind], args, where
    if kind in ('thing', 'things'):
        if kind == 'thing' and 'ids' in obj and len(obj['ids']) == 1 and 'names' not in obj:
            args['id'] = obj['ids'][0]
            if 'map' in obj:
                args['map_id'] = obj['map']
            return 'thing', args, where
        args['map_id'] = obj.get('map', 'current')
        args['include_held'] = bool(obj.get('include_held', True))
        # 原生参数是反向的 visible_only（true = 排除迷雾），由程序翻转，模型不必知道。
        args['visible_only'] = not bool(obj.get('include_fogged', True))
        if 'ids' in obj:
            where = {'id': {'in': list(obj['ids'])}}
        elif 'definition' in obj:
            # 解析后的精确定义名走 def_names，避免子串匹配。
            args['def_names'] = [obj['definition']]
        elif 'category' in obj:
            where = {'definition.category': {'eq': obj['category']}}
        return 'things', args, where
    if kind == 'thing_def':
        args['def_name'] = obj['definition']
        return 'thing_def', args, where
    if kind == 'thing_defs':
        if 'category' in obj:
            args['category_def_name'] = obj['category']
        if 'definition' in obj:
            where = {'def_name': {'eq': obj['definition']}}
        return 'thing_defs', args, where
    raise SurfaceError('unsupported kind: ' + kind)


# --- aspects → blocks ---------------------------------------------------------

# 配对实验开关：`off` 恢复修复前的口径——伤病名单只读健康界面列出的条目、不说明
# 口径、分组读数不报组数。它把“把这个事实说出来”这一项单独配对，而不是和别的
# 改动混在一起；默认 `on` 就是生产行为。用法见 planning/VERSION-1.10.md。
STATED_FACTS = os.environ.get('NEKO_SEMANTIC_STATED_FACTS', 'on').strip().lower() not in {
    '0', 'off', 'false', 'no', ''}


def _entry(aspect, at, key, fields=None, limit=None, listing=False, args=None):
    value = {'aspect': aspect, 'at': at, 'key': key}
    if fields is not None:
        value['fields'] = list(fields)
    if limit is not None:
        value['limit'] = limit
    if listing:
        value['listing'] = True
    if args:
        value['args'] = dict(args)
    return value


def _pawn_entries(surface, aspect, detail):
    if aspect == 'identity':
        return [_entry('identity', '', 'object', _supported(surface, 'pawn', IDENTITY_FIELDS['pawn']))]
    if aspect == 'health':
        return [
            _entry('health', 'health.summary', 'summary',
                   ['health_percent', 'pain', 'bleed_rate', 'in_pain_shock', 'downed']),
            # 读**含隐藏条目**的那一版：这份名单是伤病、疾病、疤、植入体、成瘾的记录，
            # 而“有没有人受伤”这个问题要的正是整份记录。默认的那版只列健康界面显示的
            # 条目，模型看不到自己被筛过（v1.10 实测因此得出“没人受伤”）。
            _entry('health', 'health.conditions', 'conditions',
                   ['id', 'def_name', 'label', 'severity', 'severity_label', 'visible'], detail,
                   args={'visible_only': False} if STATED_FACTS else None),
            _entry('health', 'health.body_parts', 'body_parts',
                   ['key', 'def_name', 'label', 'index', 'health', 'max_health'], detail),
            _entry('health', 'health.capacities', 'capacities',
                   ['def_name', 'label', 'level'], detail),
        ]
    if aspect == 'skills':
        return [_entry('skills', 'profile.skills', 'skills',
                       ['def_name', 'label', 'level', 'passion', 'aptitude', 'totally_disabled',
                        'permanently_disabled'], detail)]
    if aspect == 'gear':
        fields = ['id', 'stack_count', 'label', 'definition.def_name', 'definition.label']
        return [_entry('gear', rel, rel, fields, detail) for rel in
                ('equipment', 'apparel', 'inventory', 'carried')]
    if aspect == 'work':
        return [
            _entry('work', 'status.current_job', 'current_job', ['id', 'def_name', 'report']),
            _entry('work', 'status', 'status', ['drafted', 'in_mental_state', 'mental_state']),
        ]
    if aspect == 'mood':
        return [
            _entry('mood', 'mood', 'mood', ['level', 'instant']),
            _entry('mood', 'mood.break_thresholds', 'break_thresholds', ['minor', 'major', 'extreme']),
            _entry('mood', 'mood.thought_groups', 'thought_groups',
                   ['def_name', 'label', 'stage_index', 'mood_offset', 'member_count'], detail),
            _entry('mood', 'mood.memories', 'memories',
                   ['ref', 'def_name', 'label', 'mood_offset', 'age_ticks'], detail),
        ]
    if aspect == 'profile':
        return [
            _entry('profile', 'profile', 'profile', ['gender', 'race_def_name', 'kind_def_name']),
            _entry('profile', 'profile.age', 'age', ['biological_years', 'chronological_years', 'life_stage']),
            _entry('profile', 'profile.backstories', 'backstories', ['slot', 'def_name', 'title'], detail),
            _entry('profile', 'profile.traits', 'traits',
                   ['def_name', 'label', 'degree', 'suppressed'], detail),
        ]
    raise SurfaceError('unsupported aspect for this kind: ' + aspect)


def _resource_entries(kind, detail):
    return [
        _entry('resources', 'resource_counter.all_counted_amounts', 'all_counted_amounts',
               ['key.def_name', 'key.label', 'value'], detail),
        _entry('resources', 'resource_counter', 'resource_counter', ['total_human_edible_nutrition']),
    ]


def _supported(surface, kind, fields):
    """只保留当前 schema 真实存在的字段路径。

    插件与 Mod 版本可能不一致（例如旧 Mod 没有派系字段）。硬写字段名会直接
    报 Unknown field；正确行为是改用可用的那部分，并把缺失的部分交给 not_read 点名。
    """
    root = ROOTS[kind]
    kept = []
    for name in fields:
        try:
            surface.path_type(root, name)
        except (SurfaceError, KeyError, TypeError):
            continue
        kept.append(name)
    return kept


def _entries_for(surface, kind, aspect, detail):
    if aspect in ('quantity', 'distribution'):
        return [_entry(aspect, '', aspect, LISTING_FIELDS.get(kind), listing=True)]
    if kind in PAWN_KINDS:
        return _pawn_entries(surface, aspect, detail)
    if aspect == 'identity':
        if kind == 'colony':
            return [_entry('identity', 'game', 'game', ['paused', 'current_map_id']),
                    _entry('identity', 'maps', 'maps', _supported(surface, 'maps', ['id', 'name', 'is_current', 'colonist_count']), detail)]
        return [_entry('identity', '', 'object', _supported(surface, kind, IDENTITY_FIELDS[kind]))]
    if aspect == 'resources' and kind in MAP_KINDS:
        return _resource_entries(kind, detail)
    raise SurfaceError('unsupported aspect for kind ' + kind + ': ' + aspect)


def _identity_fields(surface, obj, entries):
    """实际会被选中的主语字段：SUBJECT_FIELDS 加上根级别 identity 条目，

    并且只保留当前 schema 真实存在的。未选中的字段不渲染，而不是渲染成 null。

    另外补上“寻址就该带上的身份事实”：对 Pawn 是无条件带上 faction；
    对单个 Thing 是带上持有者身份。原因是实测的失败场景——
    “看某人的装备 → 声称这是我们的物资”，“看一件东西 → 声称它属于我们”——
    模型读 gear 时拿不到 faction、读单件时拿不到 held_by，于是只能靠推。
    把这两项归到**寻址**而不是某个 aspect，才能让后面的事实有依据。
    """
    fields = list(SUBJECT_FIELDS.get(obj['kind'], ()))
    if obj['kind'] in PAWN_KINDS:
        fields += [f for f in FACTION_FIELDS if f not in fields]
    if obj['kind'] == 'thing':
        fields += [f for f in HOLDER_FIELDS if f not in fields]
    for entry in entries:
        if not entry['at'] and entry.get('fields') and not entry.get('listing'):
            fields += [f for f in entry['fields'] if f not in fields]
    return tuple(_supported(surface, obj['kind'], fields))


def _covers(entries):
    covered = set()
    for entry in entries:
        if entry['at']:
            covered.add(entry['at'])
        if entry.get('fields'):
            for name in entry['fields']:
                covered.add('.'.join(filter(None, [entry['at'], name])))
    return covered


# --- plan ---------------------------------------------------------------------

@dataclass
class Plan:
    tool: str
    kind: str
    root: str
    object: dict
    aspects: tuple
    detail: int
    structure: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    grouping: tuple = ()
    resolution: dict | None = None
    lookup: dict | None = None
    request: dict | None = None
    args: dict = field(default_factory=dict)
    where: dict | None = None
    root_limit: int = 0
    covers: set = field(default_factory=set)
    subject: tuple = ()
    blocked: dict | None = None
    surface: object = None


def _root_limit(kind, detail, grouping, needs_rows):
    if grouping:
        return detail if detail > 0 else 100
    if kind in POPULATIONS:
        if not needs_rows:
            return 0
        # detail 未指定时用默认根行上限；detail 已给出则尊重它。
        return detail if detail > 0 else DEFAULT_ROOT_ROWS
    return max(detail, 1) if needs_rows else 0


DEFAULT_ROOT_ROWS = 20


def plan_look(surface, obj, aspects, detail):
    """构造 look 的原生请求；未知组合已在 normalize/validate 阶段拒绝。"""
    entries = []
    for aspect in aspects:
        entries.extend(_entries_for(surface, obj['kind'], aspect, detail))
    metrics, grouping = {}, ()
    if 'quantity' in aspects or 'distribution' in aspects:
        metrics = _measures_for(obj['kind'])
    if 'distribution' in aspects:
        grouping = DEFAULT_GROUP[obj['kind']]
    # look 是"看这些东西"，不是"数这些东西"：只要请求里有任何非计量 aspect，
    # 就必须返回被寻址对象本身。否则模型问"殖民者是谁"只会得到"有 3 个"。
    # detail 只控制嵌套关系（conditions/body_parts/skills）的展开深度，
    # 不应该成为"能不能看到主语"的隐藏开关。
    rows_wanted = any(aspect not in ('quantity', 'distribution') for aspect in aspects)
    needs_rows = ((obj['kind'] not in POPULATIONS) or detail > 0 or rows_wanted
                  # 按定义寻址就是"读这个定义"，默认只回计数会让"定义存在吗"无法回答。
                  or (obj['kind'] in DEF_KINDS and 'definition' in obj))
    return _assemble(surface, 'look', obj, aspects, detail, entries, metrics, grouping, needs_rows)


def plan_count(surface, obj, metric, by, detail):
    aspects = ('quantity',) if by is None else ('distribution',)
    entries = [_entry('quantity', '', 'quantity', LISTING_FIELDS.get(obj['kind']), listing=True)]
    metrics = _measures_for(obj['kind'], metric)
    grouping = (by,) if by else ()
    # Group output is selected independently by _root_limit. An ungrouped
    # count must not read instance fields unless the caller requested detail.
    return _assemble(surface, 'count', obj, aspects, detail, entries, metrics, grouping, detail > 0)


def _measures_for(kind, metric='units'):
    measures = {'records': {'op': 'count'}}
    if kind == 'things' and metric == 'units':
        measures['units'] = {'op': 'sum', 'field': 'stack_count'}
    return measures


def _assemble(surface, tool, obj, aspects, detail, entries, metrics, grouping, needs_rows):
    root, args, where = route_object(obj)
    plan = Plan(tool=tool, kind=obj['kind'], root=root, object=dict(obj), aspects=tuple(aspects),
                detail=detail, structure=entries, metrics=metrics, grouping=grouping,
                args=args, where=where, surface=surface)
    root_limit = _root_limit(obj['kind'], detail, grouping, needs_rows)
    if needs_rows and obj.get('ids'):
        # 一次寻址到 N 个对象（含同名解析出的多个）时必须都能返回，
        # 否则明细/事实会静静丢掉其余候选。
        root_limit = max(root_limit, len(obj['ids']))
    blocks = []
    root_block = {'at': ''}
    if args:
        root_block['args'] = copy.deepcopy(args)
    if where:
        root_block['where'] = copy.deepcopy(where)
    fields = [e for e in entries if e['at'] == '' and e.get('fields') and not e.get('listing')]
    listing = [e for e in entries if e.get('fields') and e.get('listing')]
    selected = [f for e in fields for f in e['fields']]
    # 主语只算一次：同一个列表既用于**请求 select** 也用于**渲染**。
    # 两者分开算过一次错：渲染读了没被请求的字段，于是全是 null（“读了一个人的健康却不知
    # 道是谁”就是这类 bug）。这里把 subject 提前到请求构造之前，从结构上消掉分歧。
    plan.subject = _identity_fields(surface, obj, entries) if root_limit > 0 and not grouping else ()
    # 分组读取的行是**组行**（组键 + 度量），没有逐实例主语；把 id 之类加进
    # select 会让组内取值冲突，引擎正确地报 group_field_varies 并把整份读取标成
    # partial，而输出根本不用这些值（实测 v1.10：该题 21 条无谓 issue）。
    selected += [f for f in plan.subject if f not in selected]
    if listing and not grouping and root_limit > 0:
        selected += [f for e in listing for f in e['fields']]
    if selected:
        root_block['fields'] = list(dict.fromkeys(selected))
    if metrics:
        root_block['aggregate'] = copy.deepcopy(metrics)
    if grouping:
        root_block['group_by'] = [GROUP_PATHS[obj['kind']][g] for g in grouping]
    root_block['limit'] = root_limit
    blocks.append(root_block)
    for entry in entries:
        if not entry['at']:
            continue
        block = {'at': entry['at']}
        if entry.get('fields'):
            block['fields'] = list(entry['fields'])
        if entry.get('limit') is not None:
            block['limit'] = entry['limit']
        if entry.get('args'):
            block['args'] = copy.deepcopy(entry['args'])
        blocks.append(block)
    plan.covers = _covers(entries)
    plan.request = surface.compile({'source': root, 'blocks': blocks})
    plan.root_limit = root_block['limit']
    return plan


# --- 解析（名称/定义） --------------------------------------------------------

def lookup_spec(surface, obj):
    """需要二次原生解析时返回 lookup 描述，否则 None。"""
    kind = obj['kind']
    if 'names' in obj:
        names = list(obj['names'])
        if kind in ('pawn', 'pawns'):
            root, field, args = 'pawns', 'name', {}
        elif kind == 'colonists':
            root, field, args = 'spawned_free_colonists', 'name', {'map_id': obj.get('map', 'current')}
        elif kind in ('map', 'maps'):
            root, field, args = 'maps', 'name', {}
        elif kind in ('thing', 'things'):
            root, field = 'things', 'label'
            args = {'map_id': obj.get('map', 'current'), 'include_held': bool(obj.get('include_held', True)),
                    'visible_only': not bool(obj.get('include_fogged', True))}
        else:
            raise SurfaceError('kind ' + kind + ' has no display-name population to resolve')
        block = {'at': '', 'fields': list(dict.fromkeys(['id', field] + (['map_id'] if kind in PAWN_KINDS else [])))}
        if args:
            block['args'] = args
        block['where'] = {field: {'in': names}}
        block['limit'] = 100
        return {'kind': 'names', 'field': field, 'names': names, 'match_fields': (field,),
                'case_insensitive': False,
                'request': surface.compile({'source': root, 'blocks': [block]})}
    if 'definition' in obj and kind in DEF_KINDS + THING_KINDS:
        text = obj['definition']
        block = {'at': '', 'fields': ['def_name', 'label'], 'limit': 100,
                 'where': {'any': [{'def_name': {'eq': text, 'case_sensitive': False}},
                                   {'label': {'eq': text, 'case_sensitive': False}}]}}
        return {'kind': 'definition', 'field': 'def_name', 'names': [text],
                'match_fields': ('def_name', 'label'), 'case_insensitive': True,
                'request': surface.compile({'source': 'thing_defs', 'blocks': [block]})}
    return None


def interpret_lookup(lookup, packed):
    """把解析查询的结果翻成 resolution：绝不静默选一个。"""
    result = packed['results'][0]
    resolution = coverage_of(result)
    rows = result.get('rows') or []
    field = lookup['field']
    match_fields = lookup.get('match_fields') or (field,)
    complete = (resolution['scan'] == 'complete' and resolution['listing'] == 'complete'
                and resolution['fields'] == 'complete')
    wanted = lookup['names']
    folder = str.casefold if lookup.get('case_insensitive') else (lambda text: text)
    matches, missing, ambiguous = [], [], []

    def equal(row, name):
        return any(isinstance(row.get(key), str) and folder(row[key]) == folder(name)
                   for key in match_fields)

    if not complete:
        rows = [dict(row) for row in rows if any(equal(row, name) for name in wanted)]
    else:
        for name in wanted:
            found = [row for row in rows if equal(row, name)]
            if not found:
                missing.append(name)
            elif len(found) > 1:
                ambiguous.append(name)
            matches.extend(found)
    if not complete:
        status = 'incomplete'
    elif ambiguous:
        status = 'ambiguous'
    elif missing:
        status = 'missing_in_scope'
    else:
        status = 'resolved'
    return {'status': status, 'field': field, 'requested': wanted,
            'ids': [row.get('id') or row.get('def_name') for row in matches],
            'candidates': [_candidate(row) for row in rows],
            'ambiguous': ambiguous, 'missing': missing,
            'coverage': resolution,
            'note': ('Exact display names and definitions are resolved against a complete scan; an incomplete '
                     'scan or several exact matches never resolves to one object.')}


def _candidate(row):
    return {k: row.get(k) for k in ('id', 'name', 'label', 'def_name', 'map_id') if row.get(k) is not None}


def apply_lookup(obj, lookup, resolution):
    """把解析结果写回 object。

    对**总体**（can read many）：歧义（同名多个）不是失败——题目的要求往往正是
    “逐个列出”；回全部候选不等同“擅自选一个”。
    对**单对象**（map/pawn/thing）：歧义时不能选其中一个（`route_object` 只会取
    ids[0]，结果会同时声称“多义”又以 ok 回第一个），必须交回候选。
    只有缺失/扫描不完整才同样拒绝。
    """
    if resolution['status'] not in ('resolved', 'ambiguous'):
        return None
    resolved = dict(obj)
    resolved.pop('names', None)
    if lookup['field'] == 'def_name':
        if resolution['status'] == 'ambiguous':
            return None
        resolved['definition'] = resolution['ids'][0]
        return resolved
    if resolution['status'] == 'ambiguous' and obj['kind'] in SINGLE_ONLY_KINDS:
        # 实测（v1.10）：名称解析已正确报 ambiguous 并回两个 id，`map` 路由却取第一个，
        # 以 status=ok 只回一个的事实。公开承诺是“同名返回候选，绝不擅自选一个”。
        # pawn/thing 有总体根，多候选逐个返回是**有意保留**的行为，不走这里。
        return None
    resolved['ids'] = list(resolution['ids'])
    return resolved


# --- 渲染（不 intern） --------------------------------------------------------

def _measure_literal(value):
    if value.get('complete') is True and value.get('value') is not None:
        out = {'state': 'exact', 'value': value['value']}
    elif value.get('complete') is True:
        out = {'state': 'empty'}
    elif 'known_value' in value:
        out = {'state': 'lower_bound', 'minimum': value.get('known_value')}
    else:
        out = {'state': 'unavailable',
               'reason': 'the scan was incomplete and no partial value was reported'}
    out['operation'] = value.get('op')
    out['unit'] = value.get('unit')
    if value.get('field') is not None:
        out['field'] = value['field']
    return out


def _scope_literal(scope):
    out = {k: copy.deepcopy(v) for k, v in scope.items()
           if k not in ('description', 'argument_semantics')}
    notes = []
    if scope.get('description'):
        notes.append(scope['description'])
    for key, text in (scope.get('argument_semantics') or {}).items():
        if text:
            notes.append(key + ': ' + text)
    if notes:
        out['notes'] = notes
    return out


def _collection_literal(value):
    out = {'state': 'complete' if value.get('scan_complete') is True else 'partial'}
    if value.get('scan_complete') is not True:
        out['reason'] = 'the source scan was incomplete'
    out['fields'] = 'complete' if value.get('fields_complete') else 'partial'
    out['matched'] = value.get('matched_count')
    out['known_matches'] = value.get('known_matches')
    page = value.get('page')
    if page is None:
        out['listing'] = 'not_requested'
    else:
        out['listing'] = 'complete' if (value.get('scan_complete') is True
                                        and page.get('offset', 0) == 0
                                        and page.get('has_more') is False) else 'partial'
        out['returned'] = page.get('returned')
        out['has_more'] = page.get('has_more')
        out['offset'] = page.get('offset')
    if value.get('aggregates'):
        out['aggregates'] = {k: _measure_literal(v) for k, v in value['aggregates'].items()}
    if value.get('scope') is not None:
        out['scope'] = _scope_literal(value['scope'])
    rows = value.get('rows')
    if rows is not None:
        out['rows'] = [_pack(row) for row in rows]
    return out


def _is_measure(value):
    return (type(value.get('complete')) is bool and isinstance(value.get('op'), str)
            and {'value', 'unit'} <= set(value))


def _is_collection(value):
    return (type(value.get('scan_complete')) is bool
            and {'fields_complete', 'matched_count', 'known_matches'} <= set(value))


def _pack(value):
    if isinstance(value, list):
        return [_pack(item) for item in value]
    if not isinstance(value, dict):
        return value
    if _is_measure(value):
        return _measure_literal(value)
    if _is_collection(value):
        return _collection_literal(value)
    return {k: _pack(v) for k, v in value.items() if not k.startswith('_')}


def pack(data, issues=()):
    """把原生 payload 变成明文事实；不生成任何 handle。"""
    packed = _pack(copy.deepcopy(data))
    for issue in issues or ():
        path = issue.get('path') or []
        if len(path) < 2 or path[0] != 'results':
            continue
        _mark(packed, path, issue)
    return packed


def _mark(root, path, issue):
    """把未采集/不可用的槽位标成 unavailable。

    标量槽位只在确实为 null 时才替换；**对象子树同样要处理**：若整个子树都是
    null（例如整个 faction 未采集），必须把子树换成 unavailable 标记。
    留下裸 null 就是在制造"null 到底是什么意思"的歧义，而它正是
    “没读到却说成没有”那类错误的温床。
    """
    if not path:
        return
    current = root
    for part in path[:-1]:
        key = int(part) if isinstance(current, list) else part
        try:
            current = current[key]
        except (KeyError, IndexError, ValueError, TypeError):
            return
    last = path[-1]
    marker = {'state': 'unavailable', 'reason': issue.get('code'), 'message': issue.get('message')}
    try:
        if isinstance(current, list):
            index = int(last)
            target = current[index] if 0 <= index < len(current) else KeyError
        elif isinstance(current, dict):
            target = current.get(last, KeyError)
        else:
            return
    except (ValueError, TypeError):
        return
    if target is KeyError:
        return
    if target is None:
        replacement = marker
    elif isinstance(target, dict) and target and all(value is None for value in target.values()):
        # 整个子树都没采集到：逐字段的裸 null 没有任何信息量，换成一次说明。
        replacement = marker
    elif isinstance(target, list) and target and all(value is None for value in target):
        replacement = marker
    else:
        return
    if isinstance(current, list):
        current[int(last)] = replacement
    else:
        current[last] = replacement


def coverage_of(result):
    """读取已 pack 的集合覆盖率明文（scan/fields/listing/matched）。"""
    return {'scan': result.get('state', 'partial'),
            'fields': result.get('fields', 'partial'),
            'listing': result.get('listing', 'not_requested'),
            'matched': result.get('matched'),
            'known_matches': result.get('known_matches')}


def _subtree(row, at):
    node = row
    if at:
        for segment in at.split('.'):
            if not isinstance(node, dict):
                return None
            if _is_unavailable(node):
                # 中间层已经是 unavailable 标记时就此停下，不要把整个子树拆成 null。
                return node
            node = node.get(segment)
    return node


def _is_unavailable(value):
    return isinstance(value, dict) and value.get('state') == 'unavailable'


def _merge(out, path, value):
    parts = path.split('.')
    node = out
    for index, part in enumerate(parts[:-1]):
        current = node.get(part)
        if _is_unavailable(current):
            # 子树已明确为不可用（如整个 faction 未采集）：保留标记，不再拆出裸 null。
            return out
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return out


def _identity(row, fields):
    out = {}
    for name in fields:
        value = _subtree(row, name)
        if _is_unavailable(value) and '.' in name:
            # 整个子树不可用（如 faction 未采集）：在顶层键上标一次，
            # 而不是拆成四个逐字段标记。
            out[name.split('.')[0]] = value
            continue
        _merge(out, name, value)
    return out


def _field_note(surface, root, at, name):
    kind = surface.path_type(root, at)
    note = None
    for segment in name.split('.'):
        candidate = surface.notes.get(kind, {}).get(segment)
        if candidate:
            note = candidate
        kind = surface.child_type(kind, segment)
    return note


def unread_fields(surface, root, covered):
    """机械推导：本次没有读到的根字段（对被部分覆盖的字段展开一层）。"""
    kind = surface.path_type(root, '')
    index = surface.index.get(kind, {})
    out = []
    for name in index:
        children = {p.split('.', 1)[1] for p in covered if p.startswith(name + '.') and '.' in p}
        if name in covered and not children:
            continue
        if not children:
            out.append(name)
            continue
        child_kind = surface.child_type(kind, name)
        for child in (surface.index.get(child_kind) or {}):
            if child not in children:
                out.append(name + '.' + child)
    return out


def _not_read(plan, surface, result, issues=()):
    """只列高信号缺口，不枚举整个原生字段空间。

    早期实现把根类型里每一个未覆盖的原生字段都倒出来，一次白银计数就产生 42 条
    路径字符串。那既淹没真正重要的缺口（未读到的方面、身份未知、扫描不完整），
    又把路径词汇重新灌回模型上下文。这里只保留会影响结论的项。
    """
    items = []
    root_type = surface.path_type(plan.root, '')
    # 收窄范围时必须把「被排除的东西」直接点名。实测中模型写了 include_held=false
    # （只算地图上生成的物），却把结果描述成“含被持有容器内容”——那就是范围被收窄而未被声明。
    if root_type == 'Thing':
        if plan.object.get('include_held') is False:
            items.append('things inside holders (inventory, containers, equipment) are EXCLUDED by include_held=false; '
                         'the totals below are map-spawned objects only')
        if plan.object.get('include_fogged') is False:
            items.append('things under fog are EXCLUDED by include_fogged=false; the totals below are currently visible only')
    # 未请求的 aspect 折叠成一行，不再每个占一行（那是噪音，不是信号）。
    missing = [a for a in supported_aspects(plan.kind) if a not in plan.aspects]
    if missing:
        items.append('aspects not requested: ' + ', '.join(missing))
    # 身份现在属于“寻址”的一部分（见 _identity_fields），所以这里要查 **主语字段**
    # 而不是条目覆盖；否则会在已经读到 faction 时仍然声称身份未读。
    if root_type == 'Pawn' and not any(f.startswith('faction.') for f in plan.subject):
        items.append('faction/colonist-status (not read; the schema exposes no faction field; '
                     'identity is never inferred from kind, name or container)')
    for entry in plan.structure:
        if entry.get('limit') == 0 and entry['at']:
            items.append(entry['at'] + ' detail rows (count only; detail=0)')
    if plan.grouping:
        items.append(root_type + ' per-instance rows (a grouped read returns group aggregates)')
    coverage = coverage_of(result)
    if coverage['scan'] == 'partial':
        items.append(root_type + ' population remainder (scan incomplete: no exact total)')
    if coverage['listing'] == 'partial' and not plan.grouping:
        items.append(root_type + ' detail rows beyond the returned page')
    if coverage['fields'] == 'partial':
        items.append('selected fields that failed to read (see issues)')
    for issue in issues or ():
        path = issue.get('path') or []
        label = issue.get('code', 'issue')
        items.append(('results.' + '.'.join(str(p) for p in path[2:]) if len(path) > 2 else 'result')
                     + ' (' + label + ')')
    return sorted(dict.fromkeys(items))


def _semantics(plan, surface, result, resolution=None):
    notes = []
    kind = plan.kind
    if kind in PAWN_KINDS or kind in ('thing', 'things'):
        notes.append(SEMANTICS['containment'])
    # 字段是点分路径（held_by.id），必须用子串判断而不是全等。
    if kind in ('thing', 'things') and any(
            any(f.startswith('held_by') or f.startswith('faction') for f in (e.get('fields') or []))
            for e in plan.structure):
        notes.append(SEMANTICS['holder'])
    if kind in PAWN_KINDS:
        notes.append(SEMANTICS['pawn'])
    if 'health' in plan.aspects:
        notes.append(SEMANTICS['health'])
    if any(entry.get('key') == 'conditions' for entry in plan.structure):
        if STATED_FACTS:
            notes.append(SEMANTICS['conditions_scope'])
    # 字段是点分路径（parent_holder.runtime_type），必须用子串判断而不是全等。
    if any(any(f.startswith('parent_holder') for f in (entry.get('fields') or []))
           for entry in plan.structure):
        notes.append(SEMANTICS['parent_holder'])
    if any(any(f.startswith('hit_points') or f.startswith('max_hit_points') for f in (entry.get('fields') or []))
           for entry in plan.structure):
        notes.append(SEMANTICS['durability'])
    notes.append(SEMANTICS['unknown'])
    coverage = coverage_of(result)
    states = _states(result)
    if coverage['scan'] == 'partial' or 'lower_bound' in states:
        notes.append(SEMANTICS['lower_bound'])
    if plan.metrics.get('units') or 'quantity' in plan.aspects:
        notes.append(SEMANTICS['units'])
    if resolution is not None:
        notes.append(resolution['note'])
    for entry in plan.structure:
        for name in (entry.get('fields') or []):
            note = _field_note(surface, plan.root, entry['at'], name)
            if note:
                notes.append(name.split('.')[-1] + ': ' + note)
    return list(dict.fromkeys(notes))


def _states(value, found=None):
    found = set() if found is None else found
    if isinstance(value, list):
        for item in value:
            _states(item, found)
    elif isinstance(value, dict):
        if value.get('state') in ('lower_bound', 'unavailable', 'empty', 'partial') and 'operation' in value:
            found.add(value['state'])
        for item in value.values():
            _states(item, found)
    return found


def _facts(plan, result):
    if plan.root_limit == 0:
        return []
    rows = result.get('rows') or []
    subject = list(plan.subject)
    root_entries = [item for item in plan.structure if not item['at'] and item.get('fields')
                    and not item.get('listing')]
    facts = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = {'object': _identity(row, subject), 'facts': {}}
        # 根级别条目必须同样渲染：只请求 identity 时全部字段都在根上，
        # 早期实现只处理 at != '' 的条目，导致这类 look 返回空事实。
        for item in root_entries:
            entry['facts'].setdefault(item['aspect'], {})[item['key']] = _identity(row, item['fields'])
        for item in plan.structure:
            if item['at']:
                entry['facts'].setdefault(item['aspect'], {})[item['key']] = _subtree(row, item['at'])
        facts.append(entry)
    return facts


def _scope(plan, surface, result):
    raw = result.get('scope') or {}
    args = raw.get('args') or {}
    resolved = {}
    if 'map_id' in args:
        resolved['map_id'] = args['map_id']
    if 'id' in args:
        resolved['id'] = args['id']
    if 'def_name' in args:
        resolved['definition'] = args['def_name']
    if 'def_names' in args:
        resolved['definitions'] = list(args['def_names'])
    if 'ids' in args:
        resolved['ids'] = list(args['ids'])
    # 回显模型侧的同一套名字与极性，另附原生参数以便审计；不让两种极性同时出现在结果里。
    if 'include_held' in args:
        resolved['include_held'] = args['include_held']
    if 'visible_only' in args:
        resolved['include_fogged'] = not args['visible_only']
        resolved['native_visible_only'] = args['visible_only']
    # 打包后的结果只带 `notes`（_scope_literal 已经把 description 与
    # argument_semantics 合并进去并删掉旧键）。旧实现只读被删掉的旧键，
    # 于是把原生范围/参数说明静默丢弃——实测：30 次工具结果、覆盖 12/12
    # 场候选咨询的 scope.notes 变成空列表。两种形状都要认。
    notes = [note for note in (raw.get('notes') or []) if note]
    if raw.get('description'):
        notes.append(raw['description'])
    for text in (raw.get('argument_semantics') or {}).values():
        if text:
            notes.append(text)
    notes = list(dict.fromkeys(str(note) for note in notes))
    return {
        'object': {k: v for k, v in plan.object.items() if v is not None},
        'resolved': resolved,
        'native_root': plan.root,
        'native_type': surface.path_type(plan.root, ''),
        'notes': notes,
    }


def render(plan, native, resolution=None):
    """把一次原生读数渲染成明文结果。"""
    if plan.blocked is not None:
        return plan.blocked
    surface = plan.surface
    result = native['data']['results'][0]
    sample = native['data'].get('sample') or {}
    out = {
        'status': native['status'],
        'tool': plan.tool,
        'object': {k: v for k, v in plan.object.items() if v is not None},
        'scope': _scope(plan, surface, result),
        'sample': {'load_epoch': sample.get('load_epoch'), 'game_tick': sample.get('game_tick'),
                   'sequence': sample.get('sequence')},
        'coverage': coverage_of(result),
        'not_read': _not_read(plan, surface, result, native.get('issues')),
        'semantics': _semantics(plan, surface, result, resolution),
        'issues': [{'reason': issue.get('code'), 'message': issue.get('message'),
                    'path': list(issue.get('path') or [])} for issue in native.get('issues') or ()],
        'observation_id': native.get('observation_id'),
    }
    if plan.metrics:
        out['measures'] = dict(result.get('aggregates') or {})
    if plan.grouping:
        by = plan.grouping[0]
        native = GROUP_PATHS[plan.kind][by]
        out['groups'] = [{'key': {by: (row.get('key') or {}).get(native)},
                          'measures': dict(row.get('aggregates') or {})}
                         for row in (result.get('rows') or []) if isinstance(row, dict)]
        # `listing: complete` 只说列举完整，不说**共几组**，于是模型得自己数。
        # 实测（v1.10 建筑题）：渲染从不说组数，模型列出 30 组却写了“分属 31 个
        # 定义”。组数观测里本来就有，缺的只是送到模型眼前。
        if STATED_FACTS:
            out['coverage']['groups'] = len(out['groups'])
    else:
        out['facts'] = _facts(plan, result)
        # detail>0 时明细也是根行；单独回一份，不混进 aspects 事实。
        if any(item.get('listing') for item in plan.structure):
            out['listing'] = _listing_rows(plan, result)
    if resolution is not None:
        out['resolution'] = resolution
    return out


def _listing_rows(plan, result):
    rows = [row for row in (result.get('rows') or []) if isinstance(row, dict)]
    fields = list(LISTING_FIELDS.get(plan.kind) or ())
    return [_identity(row, fields) for row in rows]


def render_blocked(tool, obj, resolution, native):
    """歧义/缺失/不完整时只回候选，绝不回事实。"""
    result = (native['data']['results'] or [{}])[0]
    return {
        'status': 'unavailable',
        'tool': tool,
        'object': {k: v for k, v in obj.items() if v is not None},
        'resolution': resolution,
        'coverage': coverage_of(result),
        'measures': {},
        'facts': [],
        'not_read': ['object facts (the object was not resolved to exactly one match)'],
        'semantics': [SEMANTICS['unknown'],
                      'A display name or definition that is ambiguous, missing or only partially scanned is '
                      'never resolved to a single object.'],
        'issues': [{'reason': issue.get('code'), 'message': issue.get('message'),
                    'path': list(issue.get('path') or [])} for issue in native.get('issues') or ()],
        'observation_id': native.get('observation_id'),
    }
