# coding=utf-8
"""DM 造的人物（H2）：把 `世界.角色.<名>.数值` 变成引擎 `Unit`。

和物品/技能同一条纪律：

    · 只有结构化字段进引擎；`人格/状态/身份` 仍只是叙事与后台状态；
    · 写入口严格校验（缺字段/坏骰式直接报错），读入口宽容降级并留痕；
    · 数值块是**当局状态**：跟存档，新开一局清空。

字段契约见 `docs/DM结构化设定协议.md` §4。这里只负责"人物能用自己的 AC、
生命、攻击、豁免、技能加值参与结算"；**事件临时修正**（H2b）还没接。
"""

from __future__ import annotations

from ..state.tree import StateTree
from ..state.world import World
from .constants import (
    ABILITY_CN, ABILITIES, SKILL_CN, SKILL_ABILITY, ability_modifier,
)
from .homebrew import coerce_action_spec, build_action, describe_spec
from .unit import Unit

__all__ = ["NpcError", "STATS_KEY", "coerce_npc_stats", "build_npc_unit",
           "npc_summary", "normalize_ability", "normalize_skill"]

#: `世界.角色.<名>` 下的数值块
STATS_KEY = "数值"

_LIMITS = {"等级": (1, 20), "护甲等级": (1, 40), "生命值": (1, 1000),
           "先攻加值": (-10, 20), "速度": (0, 200)}

_ABILITY_ALIASES = {**{cn: en for en, cn in ABILITY_CN.items()},
                    **{en: en for en in ABILITIES},
                    "str": "strength", "dex": "dexterity", "con": "constitution",
                    "int": "intelligence", "wis": "wisdom", "cha": "charisma"}

_SKILL_ALIASES = {**{cn: en for en, cn in SKILL_CN.items()},
                  **{en: en for en in SKILL_ABILITY}}


class NpcError(ValueError):
    """人物数值不合法 / 世界树里没有这个人。"""


def normalize_ability(raw) -> str:
    """中文/英文/缩写 → 英文键；认不出 → 空串（由调用方报错）。"""
    text = str(raw or "").strip()
    return _ABILITY_ALIASES.get(text, _ABILITY_ALIASES.get(text.lower(), ""))


def normalize_skill(raw) -> str:
    text = str(raw or "").strip()
    return _SKILL_ALIASES.get(text, _SKILL_ALIASES.get(text.lower(), ""))


def _int_field(field: str, val, *, low: int, high: int) -> int:
    try:
        num = int(str(val).strip())
    except (TypeError, ValueError) as exc:
        raise NpcError(f"{field} 必须是整数（拿到 {val!r}）") from exc
    if not (low <= num <= high):
        raise NpcError(f"{field} 超出范围 {low}..{high}（拿到 {num}）")
    return num


def coerce_npc_stats(payload: dict) -> dict:
    """网页/HTTP 写入口：严格校验，返回干净的数值块。**不写半截。**"""
    if not isinstance(payload, dict):
        raise NpcError("人物数值必须是对象")
    stats: dict = {}

    for field, rng in _LIMITS.items():
        if payload.get(field) in (None, ""):
            continue
        stats[field] = _int_field(field, payload[field], low=rng[0], high=rng[1])

    abil_raw = payload.get("属性")
    if abil_raw not in (None, "", {}):
        if not isinstance(abil_raw, dict):
            raise NpcError("属性必须是对象（如 {\"敏捷\": 14}）")
        abil: dict[str, int] = {}
        for k, v in abil_raw.items():
            if v in (None, ""):
                continue
            en = normalize_ability(k)
            if not en:
                raise NpcError(f"不认识的属性：{k!r}")
            abil[ABILITY_CN[en]] = _int_field(f"属性.{k}", v, low=1, high=30)
        if abil:
            stats["属性"] = abil

    skills_raw = payload.get("技能加值")
    if skills_raw not in (None, "", {}):
        if not isinstance(skills_raw, dict):
            raise NpcError("技能加值必须是对象（如 {\"察觉\": 4}）")
        skills: dict[str, int] = {}
        for k, v in skills_raw.items():
            if v in (None, ""):
                continue
            en = normalize_skill(k)
            if not en:
                raise NpcError(f"不认识的技能：{k!r}")
            skills[en] = _int_field(f"技能加值.{k}", v, low=-5, high=30)
        if skills:
            stats["技能加值"] = skills

    saves_raw = payload.get("豁免熟练")
    if saves_raw not in (None, "", []):
        if isinstance(saves_raw, str):
            saves_raw = [x.strip() for x in saves_raw.replace("，", ",").split(",")]
        if not isinstance(saves_raw, (list, tuple)):
            raise NpcError("豁免熟练必须是数组（如 [\"敏捷\",\"体质\"]）")
        saves = []
        for x in saves_raw:
            en = normalize_ability(x)
            if not en:
                raise NpcError(f"不认识的豁免属性：{x!r}")
            saves.append(en)
        if saves:
            stats["豁免熟练"] = saves

    actions_raw = payload.get("动作")
    if actions_raw not in (None, "", {}):
        if not isinstance(actions_raw, dict):
            raise NpcError("动作必须是对象（动作名 → 结构化字段）")
        actions: dict = {}
        for aname, spec in actions_raw.items():
            name = str(aname or "").strip()
            if not name:
                raise NpcError("动作名不能为空")
            try:
                actions[name] = coerce_action_spec(spec)
            except ValueError as exc:
                raise NpcError(f"动作「{name}」：{exc}") from exc
        if actions:
            stats["动作"] = actions

    if "生命值" not in stats:
        raise NpcError("人物数值至少要写「生命值」")
    return stats


def build_npc_unit(state: StateTree, name: str, *, team: str = "enemy",
                   uid: str | None = None) -> Unit:
    """从世界树造一个可参战的 `Unit`。缺数值/写坏 → `NpcError`（不造半个单位）。"""
    node = World(state).npc(name)
    if node is None:
        raise NpcError(f"世界树里没有人物「{name}」")
    stats = node.get(STATS_KEY)
    if not isinstance(stats, dict) or not stats:
        raise NpcError(f"人物「{name}」没有数值块；先在人物面板写数值，"
                       f"否则他只是一个世界树里的名字")
    if team not in ("enemy", "ally", "pc", "neutral"):
        raise NpcError(f"阵营只能是 enemy/ally（拿到 {team!r}）")

    try:
        level = int(stats.get("等级") or 1)
        ac = int(stats.get("护甲等级") or 10)
        hp = int(stats.get("生命值") or 10)
    except (TypeError, ValueError) as exc:
        raise NpcError(f"人物「{name}」的等级/AC/生命值不是整数") from exc
    level = max(1, min(20, level))
    ac = max(1, min(40, ac))
    hp = max(1, hp)

    abil = {}
    for k, v in (stats.get("属性") or {}).items():
        en = normalize_ability(k)
        if not en:
            continue
        try:
            abil[ABILITY_CN[en]] = max(1, min(30, int(v)))
        except (TypeError, ValueError):
            continue

    skills: dict[str, int] = {}
    for k, v in (stats.get("技能加值") or {}).items():
        en = normalize_skill(k)
        if en:
            try:
                skills[en] = int(v)
            except (TypeError, ValueError):
                continue

    prof = {normalize_ability(x) for x in (stats.get("豁免熟练") or [])}
    prof = {x for x in prof if x}

    u = Unit(id=uid or name, name=name, team=team, is_pc=(team == "pc"),
             level=level, ac=ac, hp_max=hp, hp=hp, abilities=abil,
             save_proficiencies=prof, skill_bonuses=skills,
             speeds={"walk": 30}, position=(0.0, 0.0))
    try:
        u.speeds["walk"] = max(0, int(stats.get("速度") or 30))
    except (TypeError, ValueError):
        pass
    if stats.get("先攻加值") not in (None, ""):
        try:
            u.init_override = int(stats["先攻加值"])
        except (TypeError, ValueError):
            u.notes.append(f"⚠️「{name}」的先攻加值不是整数，按敏捷调整值算")

    # 被动察觉：技能加值优先，否则 10 + 感知调整值
    if "perception" in skills:
        u.passive_perception = 10 + skills["perception"]
    else:
        u.passive_perception = 10 + ability_modifier(
            int(abil.get("感知", 10)))

    for aname, spec in (stats.get("动作") or {}).items():
        act, notes = build_action(str(aname), spec, level=level,
                                  abilities=abil)
        u.actions.append(act)
        u.notes.extend(notes)

    # 生物词条（H3）：`世界.角色.<名>.词条`（如坐骑的「千里之足」）
    from .item_effects import (apply_unit_traits, build_charges,
                           resolve_creature)
    extra = World(state).entries()
    effs, cmods, hp_bonus = resolve_creature(name, node.get("词条") or [],
                                             extra)
    apply_unit_traits(u, effs, cmods, hp_bonus, label="生物词条")
    u.item_charges = build_charges(effs)
    if not u.actions:
        u.notes.append(f"⚠️「{name}」没有写任何动作：加入战斗后只能移动/不能攻击")
    u.notes.append(f"DM 人物「{name}」按世界树数值块参战（H2）")
    return u


def npc_summary(stats: dict) -> str:
    """一行给人看的人物数值摘要。"""
    if not isinstance(stats, dict) or not stats:
        return "（还没有数值）"
    bits = [f"AC {stats.get('护甲等级', 10)}",
            f"HP {stats.get('生命值', 10)}"]
    if stats.get("等级"):
        bits.append(f"{stats['等级']} 级")
    for aname, spec in (stats.get("动作") or {}).items():
        bits.append(f"{aname}：{describe_spec(spec)}")
    return "｜".join(bits)