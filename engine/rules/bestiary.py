# coding=utf-8
"""把 SRD 怪物数值块解析成引擎能结算的 `Unit`。

SRD 的记录是**半结构化**的：
    · `actions[].attacks[]` 有结构化的伤害骰/加值/触及 —— 这部分可信；
    · 攻击加值、豁免 DC、充能骰只能从 `desc` 文本里正则抽（"Melee Attack Roll: +14"
      / "DC 21" / "Recharge 5-6"）—— 这部分是**启发式**，抽不到就把
      `Action.structured` 置 False，让引擎明确说"这条我不自动结算"，**绝不猜**。

这条纪律对应狼人杀项目的教训：宁可在界面上说"我不确定"，也不要静默按错的规则打。
"""

from __future__ import annotations

import re

from ..srd.store import NotFound, get_store
from .constants import ABILITIES
from .unit import Action, Attack, Unit

__all__ = ["parse_creature", "build_unit", "describe_unparsed", "MonsterParseError"]


class MonsterParseError(KeyError):
    """怪物数据缺关键字段时抛这个，不要静默用默认值。"""


# --------------------------------------------------------------------------- #
# 从描述里抽数值
# --------------------------------------------------------------------------- #

_RE_ATTACK_BONUS = re.compile(
    r"(?:Melee|Ranged|Melee\s+or\s+Ranged)?\s*Attack\s+Roll\s*:\s*([+-]\d+)", re.I)
_RE_SAVE_DC = re.compile(r"DC\s*(\d+)", re.I)
_RE_RECHARGE = re.compile(r"Recharge\s+(\d)", re.I)
_RE_AREA = re.compile(
    r"(\d+)[\s-]*foot(?:-long)?\s*(?:,\s*\d+[\s-]*foot-wide\s+)?"
    r"(Cone|Sphere|Cube|Line|Emanation|Cylinder)", re.I)
#: "20-foot-radius Sphere" 这种"半径在前"的写法
_RE_AREA_RADIUS = re.compile(
    r"(\d+)[\s-]*foot-radius\s+(Cone|Sphere|Cube|Line|Emanation|Cylinder)", re.I)
_RE_PER_DAY = re.compile(r"(\d+)\s*/\s*Day", re.I)
_RE_DICE = re.compile(r"\((\d+d\d+(?:\s*[+-]\s*\d+)?)\)")
_RE_FLAT_DAMAGE = re.compile(r"Failure:\s*(?:\d+\s*)?\((\d+d\d+(?:\s*[+-]\s*\d+)?)\)", re.I)
_RE_HALF_ON_SUCCESS = re.compile(r"Success:\s*Half\s+damage", re.I)
_RE_REACH = re.compile(r"reach\s+(\d+)\s*(?:ft|feet|foot)", re.I)
_RE_RANGE = re.compile(r"range\s+(\d+)\s*/\s*(\d+)\s*(?:ft|feet|foot)", re.I)
#: “within 60 feet”这类单体/范围豁免的射程
_RE_WITHIN = re.compile(r"within\s+(\d+)\s*(?:ft|feet|foot)", re.I)
_RE_SAVE_ABILITY = re.compile(
    r"\b(Strength|Dexterity|Constitution|Intelligence|Wisdom|Charisma)\s+Saving\s+Throw", re.I)
#: 多攻：The dragon makes three Rend attacks.
_RE_MULTI = re.compile(
    r"makes?\s+(one|two|three|four|five|\d+)\s+([A-Za-z][A-Za-z' -]*?)\s+attacks?", re.I)
_RE_SPELL_SAVE_DC = re.compile(r"spell\s+save\s+DC\s*(\d+)", re.I)
_RE_SPELL_ATTACK = re.compile(r"([+-]\d+)\s+to\s+hit\s+with\s+spell\s+attacks", re.I)

_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

_ACTION_SLOT = {
    "ACTION": "action",
    "BONUS_ACTION": "bonus_action",
    "REACTION": "reaction",
    "LEGENDARY_ACTION": "legendary",
    "LAIR_ACTION": "lair",
    "MYTHIC_ACTION": "action",
    "FREE_ACTION": "free",
}


def _norm_dice(text: str) -> str:
    """规整骰式：去掉空格、小写。"""
    return re.sub(r"\s+", "", (text or "").lower())


def _damage_type_from(attack: dict) -> tuple[str, str]:
    """返回 (主伤害类型, 附加伤害类型)。

    ⚠️ SRD 的字段名有坑：民兵团长的 `Light Crossbow` 把类型放在
    `extra_damage_type` 里、而 `damage_type` 是 null。所以要按"哪边有骰子"来认。
    """
    main = (attack.get("damage_type") or {})
    extra = (attack.get("extra_damage_type") or {})
    main_t = (main.get("key") if isinstance(main, dict) else "") or ""
    extra_t = (extra.get("key") if isinstance(extra, dict) else "") or ""
    has_extra_dice = bool(attack.get("extra_damage_die_count"))
    if not main_t and not has_extra_dice and extra_t:
        # 只有一种类型、却写在 extra 里 → 那就是主类型
        return extra_t, ""
    return main_t, extra_t


def _attack_from_struct(a: dict, fallback_bonus: int) -> Attack:
    """从 attacks[] 的一条结构化记录建 Attack。"""
    cnt = int(a.get("damage_die_count") or 0)
    die = (a.get("damage_die_type") or "").lower()
    bonus = int(a.get("damage_bonus") or 0)
    main_t, extra_t = _damage_type_from(a)

    # 只写在 extra 里的那种情况：把它当主伤害
    if not main_t and a.get("extra_damage_die_count"):
        e_cnt = int(a["extra_damage_die_count"])
        e_die = (a["extra_damage_die_type"] or "").lower()
        e_bonus = int(a.get("extra_damage_bonus") or 0)
        main_t, extra_t = extra_t, ""
        notation = f"{e_cnt}{e_die}{e_bonus:+d}" if e_bonus else f"{e_cnt}{e_die}"
        return Attack(
            name=a.get("name") or "攻击",
            attack_bonus=fallback_bonus,
            damage_notation=notation,
            damage_type=main_t,
            reach=a.get("reach"),
            range_normal=a.get("range"),
            range_long=a.get("long_range"),
            is_melee=bool(a.get("reach")),
        )

    notation = f"{cnt}{die}{bonus:+d}" if (cnt and bonus) else (f"{cnt}{die}" if cnt else "")
    extra_notation = ""
    if a.get("extra_damage_die_count"):
        e_cnt = int(a["extra_damage_die_count"])
        e_die = (a["extra_damage_die_type"] or "").lower()
        e_bonus = int(a.get("extra_damage_bonus") or 0)
        extra_notation = f"{e_cnt}{e_die}{e_bonus:+d}" if e_bonus else f"{e_cnt}{e_die}"
    return Attack(
        name=a.get("name") or "攻击",
        attack_bonus=fallback_bonus,
        damage_notation=notation,
        damage_type=main_t,
        extra_damage_notation=extra_notation,
        extra_damage_type=extra_t,
        reach=a.get("reach"),
        range_normal=a.get("range"),
        range_long=a.get("long_range"),
        is_melee=a.get("reach") is not None,
    )


def parse_action(raw: dict, *, spell_save_dc: int = 0,
                 spell_attack_bonus: int | None = None) -> Action:
    """把一个 SRD action 解析成引擎 Action。"""
    name = raw.get("name") or "（无名动作）"
    desc = raw.get("desc") or ""
    slot = _ACTION_SLOT.get((raw.get("action_type") or "ACTION").upper(), "action")
    act = Action(name=name, slot=slot, desc=desc,
                 legendary_cost=int(raw.get("legendary_action_cost") or 0))

    lim = raw.get("usage_limits") or {}
    if lim:
        t = (lim.get("type") or "").upper()
        if t == "RECHARGE_ON_ROLL":
            act.recharge_on_roll = int(lim.get("param") or 0)
        elif t == "PER_DAY":
            act.uses_per_day = int(lim.get("param") or 0)
    if not act.recharge_on_roll:
        m = _RE_RECHARGE.search(desc)
        if m:
            act.recharge_on_roll = int(m.group(1))

    # ---- 攻击：先用结构化 attacks[]，再加描述里的攻击加值 ----
    m_bonus = _RE_ATTACK_BONUS.search(desc)
    fallback_bonus = int(m_bonus.group(1)) if m_bonus else 0
    struct_attacks = raw.get("attacks") or []
    for sa in struct_attacks:
        act.attacks.append(_attack_from_struct(sa, fallback_bonus))

    # 描述里有攻击加值、但没有结构化 attacks[]：只当"文字建议"，不自动结算
    if m_bonus and not struct_attacks:
        act.structured = False
        act.attacks.append(Attack(name=name, attack_bonus=fallback_bonus))
        act.damage_notation = ""
        return act

    # ---- 豁免型动作（吐息、控制…） ----
    m_ab = _RE_SAVE_ABILITY.search(desc)
    if m_ab:
        act.save_ability = m_ab.group(1).lower()
    m_dc = _RE_SAVE_DC.search(desc)
    if m_dc:
        act.save_dc = int(m_dc.group(1))
    elif m_ab and spell_save_dc:
        act.save_dc = spell_save_dc
    if act.save_ability and not act.save_dc:
        # 有豁免要求、却没有 DC：不猜
        act.structured = False

    # 伤害：既可能写在 attack 上，也可能写在动作本身（AoE）
    if not act.attacks:
        m_d = _RE_FLAT_DAMAGE.search(desc) or _RE_DICE.search(desc)
        if m_d:
            act.damage_notation = _norm_dice(m_d.group(1))
            act.damage_type = _guess_damage_type(desc)
    act.save_success_half = bool(_RE_HALF_ON_SUCCESS.search(desc))

    m_area = _RE_AREA.search(desc) or _RE_AREA_RADIUS.search(desc)
    if m_area:
        act.area_size = int(m_area.group(1))
        act.area_shape = m_area.group(2).lower()

    # 豁免型动作的射程：`Range 30/120` 取最远；`within 60 feet` 直接取
    m_rg = _RE_RANGE.search(desc)
    if m_rg:
        act.range_ft = int(m_rg.group(2))
    else:
        m_within = _RE_WITHIN.search(desc)
        if m_within:
            act.range_ft = int(m_within.group(1))

    # ---- 多攻：只记"几次什么攻击"（引擎据此展开成多次攻击检定） ----
    if not act.attacks:
        m_multi = _RE_MULTI.search(desc)
        if m_multi:
            n_raw, kind = m_multi.group(1).lower(), m_multi.group(2).strip().lower()
            n = _WORD_NUM.get(n_raw) or (int(n_raw) if n_raw.isdigit() else 0)
            if n:
                # 把多攻展开成 n 次同名攻击（引擎按名字去怪物动作里找）
                act.name = name
                act.damage_notation = f"__multiattack__:{n}:{kind}"
                act.structured = False   # 具体每次攻击要由调用方补齐

    # ---- 施法 ----
    if not act.attacks and not act.save_ability and spell_attack_bonus is not None:
        if "spell attack" in desc.lower():
            act.attacks.append(Attack(name=name, attack_bonus=spell_attack_bonus))
    return act


_DAMAGE_WORDS = (
    "Acid", "Bludgeoning", "Cold", "Fire", "Force", "Lightning", "Necrotic",
    "Piercing", "Poison", "Psychic", "Radiant", "Slashing", "Thunder",
)


def _guess_damage_type(desc: str) -> str:
    """从描述里找第一个伤害类型词。找不到给空串（不猜）。"""
    for w in _DAMAGE_WORDS:
        if re.search(rf"\b{w}\b", desc, re.I):
            return w.lower()
    return ""


# --------------------------------------------------------------------------- #
# 生物 → Unit
# --------------------------------------------------------------------------- #

def parse_creature(record: dict) -> tuple[Unit, list[str]]:
    """把一个 SRD 生物记录解析成 Unit。返回 (Unit, 未结构化解析的动作名列表)。"""
    if not record.get("name"):
        raise MonsterParseError("生物记录没有 name")
    for need in ("armor_class", "hit_points"):
        if record.get(need) is None:
            raise MonsterParseError(f"{record.get('name')} 缺 {need}")

    name = record["name"]
    uid = record.get("key") or re.sub(r"\W+", "-", name.lower())

    # 施法信息（先解析，动作解析要用）
    spell_dc, spell_atk = 0, None
    for t in (record.get("traits") or []) + [
            a for a in (record.get("actions") or []) if a.get("name") == "Spellcasting"]:
        d = t.get("desc") or ""
        m = _RE_SPELL_SAVE_DC.search(d)
        if m:
            spell_dc = int(m.group(1))
        m = _RE_SPELL_ATTACK.search(d)
        if m:
            spell_atk = int(m.group(1))

    actions: list[Action] = []
    unparsed: list[str] = []
    for raw in (record.get("actions") or []):
        act = parse_action(raw, spell_save_dc=spell_dc, spell_attack_bonus=spell_atk)
        actions.append(act)
        if not act.structured and "__multiattack__" not in (act.damage_notation or ""):
            unparsed.append(act.name)

    # ⚠️ **特性不是动作**：`Death Throes`（死亡触发）、`Stench`/`Fear Aura`（光环）、
    # `Pack Tactics`（被动）文本里也可能出现 DC / attack roll，但把它们塞进
    # `unit.actions` 会变成"每回合可用的主动技能"（Balor 每回合死一次）。
    # 这里只收 `actions` 数组；特性保持只读（引擎暂不自动触发），不再生成选项。
    # （2026-11-05 内部审查：57 条特性曾被误塞成动作）

    # 抗性/免疫
    ri = record.get("resistances_and_immunities") or {}
    res = {str(x.get("key") if isinstance(x, dict) else x).lower()
           for x in (ri.get("damage_resistances") or [])}
    imm = {str(x.get("key") if isinstance(x, dict) else x).lower()
           for x in (ri.get("damage_immunities") or [])}
    vul = {str(x.get("key") if isinstance(x, dict) else x).lower()
           for x in (ri.get("damage_vulnerabilities") or [])}
    cimm = {str(x.get("key") if isinstance(x, dict) else x).lower()
            for x in (ri.get("condition_immunities") or [])}

    sp = record.get("speed_all") or record.get("speed") or {}
    speeds = {k: int(v) for k, v in sp.items()
              if k != "unit" and isinstance(v, (int, float)) and v}

    unit = Unit(
        id=uid,
        name=name,
        team="enemy",
        ac=int(record["armor_class"]),
        ac_note=record.get("armor_detail") or "",
        hp_max=int(record["hit_points"]),
        hp=int(record["hit_points"]),
        abilities={k: int(v) for k, v in (record.get("ability_scores") or {}).items()
                   if k in ABILITIES},
        save_bonuses={k: int(v) for k, v in (record.get("saving_throws_all") or {}).items()},
        skill_bonuses={k: int(v) for k, v in (record.get("skill_bonuses_all") or {}).items()},
        passive_perception=int(record.get("passive_perception") or 10),
        speeds=speeds or {"walk": 30},
        size=(record.get("size") or {}).get("key") or "medium",
        senses={k: int(v) for k, v in record.items()
                if k.endswith("_range") and isinstance(v, int) and v
                and k != "normal_sight_range"},
        actions=actions,
        resistances=res, immunities=imm, vulnerabilities=vul,
        condition_immunities=cimm,
        source_key=record.get("key") or "",
    )
    if spell_dc:
        unit.notes.append(f"法术豁免 DC {spell_dc}")
    if spell_atk is not None:
        unit.notes.append(f"法术攻击 +{spell_atk}")
    if record.get("legendary_action_description") or any(
            a.slot == "legendary" for a in actions):
        unit.legendary_actions_max = 3     # SRD 标准：传奇生物每轮 3 次
    return unit, unparsed


def build_unit(creature: str, *, uid: str | None = None,
               team: str = "enemy") -> tuple[Unit, list[str]]:
    """按名字/ key 从本地 SRD 建一个 Unit。"""
    store = get_store()
    try:
        rec = store.by_key("creatures", creature)
    except NotFound:
        rec = store.by_name("creatures", creature)
    unit, unparsed = parse_creature(rec)
    if uid:
        unit.id = uid
    unit.team = team
    return unit, unparsed


def describe_unparsed(creature: str) -> list[str]:
    """快速看某个怪有哪些动作引擎**不自动结算**（写测试/排查用）。"""
    _, unparsed = build_unit(creature)
    return unparsed
