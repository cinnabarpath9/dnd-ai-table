# coding=utf-8
"""结算：把"我想做什么"变成"发生了什么"。

**这个模块是模型的替代品。** 模型说出意图（`cast fireball at G3`），
这里负责投骰、判定、扣血、改状态、**并把每一步留痕**，然后把结果回给模型去写叙事。

三条口径（对应决策记录 §5.7 的能力清单）：
    1. **动作经济正确性**由 `Unit.can_take()` 保证 —— 非法动作根本进不了结算；
    2. **结构化输出稳定性**由引擎兜底 —— 模型给不出合法意图，就退化成"该单位不行动"；
    3. **战术评分/资源取舍/排序**留给模型 —— 这些是本模块**不做**的（它不做决策）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable

from ..dice import AbilityCheck, Random, RollMods, roll, roll_d20
from .constants import (
    ABILITIES, ABILITY_CN, AUTO_CRIT_CONDITIONS, CONDITIONS, SKILL_ABILITY,
    SKILL_CN, ability_modifier, exhaustion_modifier,
)
from .effects import total as _mod_total
from .unit import Action, Attack, DamagePacket, DamageResult, Unit

__all__ = [
    "AdjudicationError", "CheckResult", "AttackResult", "SaveResult",
    "ability_check", "skill_check", "attack_roll", "saving_throw",
    "resolve_attack", "resolve_save_action", "resolve_damage_expression",
    "SpellCastResult", "cast_spell", "spell_save_dc", "spell_attack_bonus",
    "concentration_check", "break_concentration",
    "distance_ft", "grid_distance_ft", "attack_advantage", "cover_bonus",
    "DC_TABLE", "DC_TABLE_CN",
]


class AdjudicationError(ValueError):
    """意图不合法（单位已倒下、动作不在它身上、目标非法…）时抛这个。

    不要返回"什么都没发生"——那会让非法意图看起来像打空了。
    """


# --------------------------------------------------------------------------- #
# 距离（战场是 5 尺一格的方格）
# --------------------------------------------------------------------------- #

def distance_ft(a: tuple[float, float], b: tuple[float, float]) -> float:
    """直线距离（尺）。"""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def grid_distance_ft(a: tuple[float, float], b: tuple[float, float]) -> float:
    """格距（尺）—— D&D 用切比雪夫距离：**对角算 1 格**。

    ⚠️ 别用欧氏距离判"能不能打到"：`10,0` 与 `0,10` 的欧氏距离是 14.1 尺，
    但它们其实是**斜向相邻的两格（10 尺）**，用欧氏会把合法的近战判成打不到。
    """
    dx = abs(a[0] - b[0])
    dy = abs(a[1] - b[1])
    return max(dx, dy)


grid_distance = grid_distance_ft


def cover_bonus(cover: str) -> int:
    """掩护给的 AC/敏捷豁免加值（2024 规则）。"""
    return {"none": 0, "half": 2, "+2": 2,
            "three_quarters": 5, "+5": 5, "total": 99}.get(
        (cover or "none").lower(), 0)


# --------------------------------------------------------------------------- #
# 优势/劣势 的合成
# --------------------------------------------------------------------------- #

def _env_advantage(attacker: Unit, target: Unit, *, ranged: bool,
                   distance: float) -> RollMods:
    """由**状态与位置**推出的优势/劣势（不含主动选择）。

    保留这个薄封装是为了给外部（UI/测试）一个只看环境、不看主动修饰的入口；
    真正的合成逻辑在 `attack_advantage()`，理由也由它给出。
    """
    mods, _ = attack_advantage(attacker, target, ranged=ranged, distance=distance)
    return mods


_CN = {"blinded": "目盲", "poisoned": "中毒", "frightened": "恐慌", "prone": "倒地",
       "restrained": "受缚", "paralyzed": "麻痹", "petrified": "石化",
       "stunned": "震慑", "unconscious": "昏迷", "invisible": "隐形",
       "grappled": "受擒", "charmed": "魅惑", "deafened": "耳聋",
       "incapacitated": "失能"}


def _cn(key: str) -> str:
    return _CN.get(key, key)


def attack_advantage(attacker: Unit, target: Unit, *, ranged: bool = False,
                     distance: float = 5.0,
                     extra_adv: bool = False, extra_dis: bool = False,
                     target_dodging: bool = False) -> tuple[RollMods, list[str]]:
    """合成一次攻击的优势/劣势，并**说明理由**（理由要能给人看）。"""
    reasons: list[str] = []
    adv = dis = False

    if attacker.has_condition_effect("incapacitated"):
        dis = True
        reasons.append("攻击者失能")

    # 状态的攻防字段由 Condition 统一提供（不要再在别处硬编码一份）
    for c in attacker.conditions:
        if getattr(CONDITIONS.get(c), "own_attacks_disadvantage", False):
            dis = True
            reasons.append(f"攻击者{_cn(c)}")
    for c in target.conditions:
        if getattr(CONDITIONS.get(c), "attacks_against_advantage", False):
            adv = True
            reasons.append(f"目标{_cn(c)}")
    if "invisible" in target.conditions:
        dis = True
        reasons.append("目标隐形")
    if "invisible" in attacker.conditions:
        adv = True
        reasons.append("攻击者隐形")
    if "prone" in target.conditions:
        if distance <= 5:
            adv = True
            reasons.append("目标倒地且 5 尺内")
        else:
            dis = True
            reasons.append("目标倒地且超过 5 尺")
    if target_dodging:
        dis = True
        reasons.append("目标采取了回避动作（Dodge）")

    if extra_adv:
        adv = True
        reasons.append("（本次另有优势来源）")
    if extra_dis:
        dis = True
        reasons.append("（本次另有劣势来源）")

    if adv and dis:
        reasons.append("优势与劣势抵消")
        return RollMods(), reasons
    return (RollMods(advantage=True) if adv else
            RollMods(disadvantage=True) if dis else RollMods()), reasons


# --------------------------------------------------------------------------- #
# 检定 / 豁免 / 攻击
# --------------------------------------------------------------------------- #

@dataclass
class CheckResult:
    check: AbilityCheck
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        s = self.check.summary()
        if self.reasons:
            s += "（" + "；".join(self.reasons) + "）"
        return s

    def to_dict(self) -> dict:
        d = self.check.to_dict()
        d["reasons"] = self.reasons
        return d


def ability_check(unit: Unit, ability: str, dc: int, rng: Random, *,
                  advantage: bool = False, disadvantage: bool = False,
                  extra_bonus: int = 0,
                  extra_breakdown: dict[str, int] | None = None) -> CheckResult:
    """属性检定。"""
    a = (ability or "").lower()
    if a not in ABILITIES:
        raise AdjudicationError(f"不认识的属性：{ability!r}（可选 {', '.join(ABILITIES)}）")
    if not unit.alive:
        raise AdjudicationError(f"{unit.name} 已倒地，不能做检定")

    # ⚠️ 走 `unit.mod()`：它中英双语都认（stat_data 用中文键），
    #    直接拿英文键查 `unit.abilities` 会让中文键角色全部按 10 算。
    extra = _mod_total(unit, "check", a)
    bonus = unit.mod(a) + exhaustion_modifier(unit.exhaustion) + extra
    bd = {ABILITY_CN[a]: unit.mod(a)}
    if unit.exhaustion:
        bd["力竭"] = exhaustion_modifier(unit.exhaustion)
    if extra:
        bd["修正"] = extra
    if extra_bonus:
        bd["其他"] = extra_bonus
    bd.update(extra_breakdown or {})

    mods = RollMods(advantage=advantage, disadvantage=disadvantage)
    r = roll_d20(rng, bonus + extra_bonus, mods)
    chk = AbilityCheck(kind="check", label=f"{ABILITY_CN[a]}检定", a=r,
                       bonus=bonus + extra_bonus, dc=int(dc), bonus_breakdown=bd)
    return CheckResult(chk)


def skill_check(unit: Unit, skill: str, dc: int, rng: Random, *,
                advantage: bool = False, disadvantage: bool = False,
                extra_bonus: int = 0) -> CheckResult:
    """技能检定。熟练加值从 `skill_bonuses`（已算好的总加值）或熟练表取。"""
    s = (skill or "").lower().replace(" ", "_")
    ability = SKILL_ABILITY.get(s) or SKILL_ABILITY.get(s.replace("_", "-"))
    if not ability:
        raise AdjudicationError(f"不认识的技能：{skill!r}")
    if not unit.alive:
        raise AdjudicationError(f"{unit.name} 已倒地，不能做检定")

    cn = SKILL_CN.get(s, s)
    extra = _mod_total(unit, "skill", s)
    # 已经在 skill_bonuses 里给了总加值 → 直接用它，别再叠属性调整值
    if s in unit.skill_bonuses:
        total_bonus = (unit.skill_bonuses[s] + exhaustion_modifier(unit.exhaustion)
                       + extra_bonus + extra)
        bd = {cn: unit.skill_bonuses[s]}
    else:
        base = unit.mod(ability)
        total_bonus = (base + exhaustion_modifier(unit.exhaustion)
                       + extra_bonus + extra)
        bd = {ABILITY_CN[ability]: base}
    if unit.exhaustion:
        bd["力竭"] = exhaustion_modifier(unit.exhaustion)
    if extra:
        bd["修正"] = extra
    if extra_bonus:
        bd["其他"] = extra_bonus

    mods = RollMods(advantage=advantage, disadvantage=disadvantage)
    r = roll_d20(rng, total_bonus, mods)
    chk = AbilityCheck(kind="check", label=f"{cn}检定", a=r, bonus=total_bonus,
                       dc=int(dc), bonus_breakdown=bd)
    return CheckResult(chk)


def saving_throw(unit: Unit, ability: str, dc: int, rng: Random, *,
                 advantage: bool = False, disadvantage: bool = False,
                 can_succeed: bool = False) -> CheckResult:
    """豁免检定。

    `can_succeed=True` 模拟"传奇抗性"（失败可改为成功）—— 由调用方决定是否消耗。
    自动失败（麻痹/震慑/昏迷/石化的力敏豁免）在这里直接判失败。
    """
    a = (ability or "").lower()
    if a not in ABILITIES:
        raise AdjudicationError(f"不认识的属性：{ability!r}")

    reasons: list[str] = []
    if not unit.alive:
        # 倒地/死亡不能豁免（死亡豁免是另一套，不在这里）
        raise AdjudicationError(f"{unit.name} 已倒地，不能做豁免")

    # 回避：敏捷豁免有优势（菜单里承诺过，这里必须真的生效）
    if a == "dexterity" and unit.used.get("dodging") and not disadvantage:
        advantage = True
        reasons.append("回避动作：敏捷豁免有优势")
    # 受缚：敏捷豁免有劣势（不是自动失败）
    if a == "dexterity" and unit.has_condition_effect("dex_save_disadvantage") \
            and not advantage:
        disadvantage = True
        reasons.append("受缚：敏捷豁免有劣势")

    if a in ("strength", "dexterity") and unit.has_condition_effect("auto_fail_str_dex_save"):
        bad = [c for c in unit.conditions
               if getattr(CONDITIONS.get(c), "auto_fail_str_dex_save", False)]
        reasons.append("状态导致力/敏豁免自动失败"
                       + (f"（{_cn(bad[0])}）" if bad else ""))
        r = roll_d20(rng, 0, RollMods())
        r.natural, r.kept, r.detail = 1, [[1]], [[1]]
        chk = AbilityCheck(kind="save", label=f"{ABILITY_CN[a]}豁免", a=r, bonus=0,
                           dc=int(dc), bonus_breakdown={})
        return CheckResult(chk, reasons)

    bonus = unit.save_total(a)
    keys = (a, ABILITY_CN.get(a, a))
    if any(k in unit.save_bonuses for k in keys):
        parts = {"预置加值": next(unit.save_bonuses[k] for k in keys
                                  if k in unit.save_bonuses)}
    else:
        parts = {ABILITY_CN[a]: unit.mod(a)}
        if any(k in unit.save_proficiencies for k in keys):
            parts["熟练"] = unit.proficiency
    if unit.exhaustion:
        parts["力竭"] = exhaustion_modifier(unit.exhaustion)

    mods = RollMods(advantage=advantage, disadvantage=disadvantage)
    r = roll_d20(rng, bonus, mods)
    chk = AbilityCheck(kind="save", label=f"{ABILITY_CN[a]}豁免", a=r, bonus=bonus,
                       dc=int(dc), bonus_breakdown=parts)
    if can_succeed and not chk.success:
        reasons.append("传奇抗性：失败改为成功")
        # ⚠️ 只覆盖结论，**不伪造骰面**（改 natural 会让复盘看到不存在的 d20）
        chk.forced = True
    return CheckResult(chk, reasons)


# --------------------------------------------------------------------------- #
# 攻击与伤害
# --------------------------------------------------------------------------- #

@dataclass
class AttackResult:
    label: str
    check: AbilityCheck
    hit: bool
    crit: bool
    reasons: list[str] = field(default_factory=list)
    damage_notation: str = ""
    damage_type: str = ""
    damage_rolls: list = field(default_factory=list)      # 主伤害各段的 Roll
    damage_total: int = 0
    damage_result: DamageResult | None = None
    #: 打中了谁（NPC 事件驱动要用；只记 id/name，不存 Unit 引用）
    target_id: str = ""
    target_name: str = ""
    #: 被反应（如护盾术）改写时的留痕
    reaction_cast: "SpellCastResult | None" = None
    reaction_note: str = ""

    def summary(self) -> str:
        # check.summary() 里已经带了 label，这里不要重复贴
        head = self.check.summary()
        if self.reasons:
            head += "（" + "；".join(self.reasons) + "）"
        if self.reaction_note:
            head += f"［反应：{self.reaction_note}］"
        if not self.hit:
            return head
        dmg = f" ⇒ 伤害 {self.damage_total} {self.damage_type}"
        if self.crit:
            dmg += "（**重击：伤害骰翻倍**）"
        if self.damage_result:
            dmg += f"；{self.damage_result.summary()}"
        return head + dmg

    def to_dict(self) -> dict:
        return {
            "label": self.label, "check": self.check.to_dict(),
            "hit": self.hit, "crit": self.crit, "reasons": self.reasons,
            "damage_notation": self.damage_notation, "damage_type": self.damage_type,
            "damage_total": self.damage_total,
            "damage_rolls": [r.to_dict() for r in self.damage_rolls],
            "reaction_note": self.reaction_note,
            "reaction": (None if self.reaction_cast is None
                         else self.reaction_cast.to_dict()),
            "damage_result": (None if self.damage_result is None else {
                "hp_before": self.damage_result.hp_before,
                "hp_after": self.damage_result.hp_after,
                "immune": self.damage_result.immune,
                "resisted": self.damage_result.resisted,
                "vulnerable": self.damage_result.vulnerable,
                "notes": self.damage_result.notes,
                "summary": self.damage_result.summary(),
            }),
            "summary": self.summary(),
        }


def attack_roll(attacker: Unit, target: Unit, attack_bonus: int, target_ac: int,
                rng: Random, *, ranged: bool = False, distance: float = 5.0,
                cover: str = "none", label: str = "攻击",
                extra_adv: bool = False, extra_dis: bool = False,
                target_dodging: bool = False) -> AttackResult:
    """攻击检定（不含伤害）。"""
    if not attacker.alive:
        raise AdjudicationError(f"{attacker.name} 已倒地，不能攻击")
    if not target.alive:
        raise AdjudicationError(f"{target.name} 已倒地，不能再被打")

    mods, reasons = attack_advantage(
        attacker, target, ranged=ranged, distance=distance,
        extra_adv=extra_adv, extra_dis=extra_dis, target_dodging=target_dodging)
    cb = cover_bonus(cover)
    eff_ac = int(target_ac) + cb
    if cb:
        reasons.append(f"目标有掩护（AC +{cb}）")

    # 5 尺内的麻痹/昏迷目标 → 攻击必定重击（石化不在其中）
    auto_crit = (distance <= 5 and any(
        c in target.conditions for c in AUTO_CRIT_CONDITIONS))
    r = roll_d20(rng, int(attack_bonus), mods)
    chk = AbilityCheck(kind="attack", label=label, a=r, bonus=int(attack_bonus),
                       dc=eff_ac, bonus_breakdown={"攻击加值": int(attack_bonus)})
    crit = chk.success and (chk.a.is_crit or auto_crit)
    if auto_crit:
        reasons.append("目标失能且 5 尺内 → 必定重击")
    return AttackResult(label=label, check=chk, hit=chk.success, crit=crit,
                        reasons=reasons, target_id=target.id,
                        target_name=target.name)


def resolve_damage_expression(notation: str, rng: Random, *,
                              crit: bool = False) -> tuple[int, list]:
    """掷一段伤害。**重击时骰子数量翻倍，固定加值不翻倍**（2024 规则）。

    返回 (点数, Roll 列表)。重击会返回两颗 Roll（各掷一遍），便于复盘对账。
    """
    return _roll_damage(notation, rng, crit)


_DICE_IN = re.compile(r"(\d*)d(\d+)")


def _double_dice(notation: str) -> str:
    """把骰式里的每段 XdY 变成 2XdY（固定加值不动）。"""
    def rep(m):
        cnt = int(m.group(1)) if m.group(1) else 1
        return f"{cnt * 2}d{m.group(2)}"
    return _DICE_IN.sub(rep, notation)


def resolve_attack(attacker: Unit, target: Unit, attack: Attack,
                   rng: Random, *, distance: float = 5.0,
                   cover: str = "none", target_dodging: bool = False,
                   extra_adv: bool = False, extra_dis: bool = False,
                   apply_damage: bool = True,
                   on_hit: Callable[[AttackResult], AttackResult] | None = None
                   ) -> AttackResult:
    """完整的攻击结算：检定 → （命中则）伤害 → 扣血。

    **这是模型最常走的那条路**：模型说"我砍 B 哥布林"，这里出全部数字。

    `on_hit` 是**命中后、伤害前**的钩子（反应层用）：可以改 AC/命中，
    比如护盾术把这次命中翻成未命中。**默认不挂任何反应** —— 直接调用这个
    底层函数的 demo/测试不会被偷偷加戏。
    """
    ranged = not attack.is_melee
    # 远程的可达距离是**最远射程**，不是普通射程；普通射程外只是劣势
    if ranged:
        need = attack.range_long or attack.range_normal or 0
    else:
        need = attack.reach or 5
    if need and distance > need + 1e-9:
        # 够不着就是够不着 —— 抛错，别静默打空
        raise AdjudicationError(
            f"{attacker.name} 的「{attack.name}」够不到 {target.name}："
            f"距离 {distance:g} 尺 > {need} 尺")
    far_shot = bool(ranged and attack.range_normal and attack.range_long
                    and attack.range_normal < distance <= attack.range_long)

    res = attack_roll(attacker, target,
                      attack.attack_bonus + _mod_total(attacker, "attack"),
                      target.ac_total, rng,
                      ranged=ranged, distance=distance, cover=cover,
                      label=f"{attacker.name} 用「{attack.name}」攻击 {target.name}",
                      extra_adv=extra_adv, extra_dis=extra_dis or far_shot,
                      target_dodging=target_dodging)

    if far_shot:
        res.reasons.append(f"超出普通射程（{attack.range_normal} 尺）→ 劣势")

    if not res.hit:
        return res

    if on_hit is not None:
        res = on_hit(res)
        if not res.hit:
            return res

    # ---- 伤害 ----
    packets: list[DamagePacket] = []
    rolls = []
    total = 0
    dmg_mod = _mod_total(attacker, "damage")
    if attack.damage_notation:
        amt, rolls = _roll_damage(attack.damage_notation, rng, res.crit)
        amt = max(0, amt + dmg_mod)
        total += amt
        packets.append(DamagePacket(amt, attack.damage_type or "", magical=False))
    if attack.extra_damage_notation:
        amt2, rolls2 = _roll_damage(attack.extra_damage_notation, rng, res.crit)
        if not attack.damage_notation:
            amt2 = max(0, amt2 + dmg_mod)
        total += amt2
        rolls += rolls2
        packets.append(DamagePacket(amt2, attack.extra_damage_type or "", magical=False))

    res.damage_notation = attack.damage_notation or ""
    res.damage_type = attack.damage_type or ""
    res.damage_rolls = rolls
    res.damage_total = total

    if apply_damage and packets:
        # 归零后的溢出伤害不再继续扣（已经是 0 了）
        res.damage_result = target.take_damage(packets)
        line = damage_concentration_line(target, res.damage_total, rng, res.damage_result)
        if line:
            res.reasons.append(line)
    return res


def damage_concentration_line(target: Unit, amount: int, rng: Random,
                              dr=None) -> str:
    """受伤后的专注检定。**主体是受伤者**，不是打出伤害的人。

    `amount` 是掷骰原始值；有 `dr`（DamageResult）时用**减免之后**的实际伤害，
    免疫/抗性目标不该按原始伤害掷（甚至 0 伤害也掷）。

    通过返回空串；失败返回一行留痕（并把专注打断）。
    """
    actual = amount
    if dr is not None:
        actual = sum(int(p.amount) for p in dr.packets)
    if actual <= 0 or not getattr(target, "concentrating_on", ""):
        return ""
    if (not target.alive) or target.is_incapacitated:
        was = break_concentration(target, reason="倒地/失能")
        return (f"⚡ {target.name} 倒地/失能，「{was}」的专注中断"
                if was else "")
    chk = concentration_check(target, actual, rng)
    if chk.check.success:
        return ""
    was = break_concentration(target, reason=f"受 {actual} 点伤害未过专注检定")
    return f"⚡ {target.name} 专注检定失败，打断了「{was}」"


def _roll_damage(notation: str, rng: Random, crit: bool) -> tuple[int, list]:
    """掷一段伤害，返回 (点数, Roll 列表)。重击把骰子翻倍。"""
    if not notation:
        return 0, []
    if crit:
        # 翻倍的正确做法：**再掷一遍同样的骰**（不是把结果乘 2）
        r1 = roll(notation, rng)
        r2 = roll(notation, rng)
        return r1.dice_total + r2.dice_total + r1.modifier, [r1, r2]
    r = roll(notation, rng)
    return r.dice_total + r.modifier, [r]


# --------------------------------------------------------------------------- #
# 豁免型动作（吐息、控制法术…）
# --------------------------------------------------------------------------- #

@dataclass
class SaveResult:
    """一次豁免型动作的结算（AoE 吐息 / 控制法术）。"""
    label: str
    save_ability: str
    save_dc: int
    targets: list[dict] = field(default_factory=list)   # 每个目标一条留痕
    damage_notation: str = ""

    def summary(self) -> str:
        lines = [f"{self.label}：{ABILITY_CN.get(self.save_ability, self.save_ability)}豁免 "
                 f"DC {self.save_dc}"]
        for t in self.targets:
            s = f"  · {t['name']}：{t['save_summary']}"
            if t.get("damage_summary"):
                s += f" ⇒ {t['damage_summary']}"
            lines.append(s)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"label": self.label, "save_ability": self.save_ability,
                "save_dc": self.save_dc, "damage_notation": self.damage_notation,
                "targets": self.targets, "summary": self.summary()}


def resolve_save_action(actor: Unit, targets: list[Unit], action: Action,
                        rng: Random, *, apply_damage: bool = True,
                        magical: bool = False) -> SaveResult:
    """结算一个"目标豁免"的动作。

    半伤口径：`action.save_success_half` 为真时，豁免成功受**半伤**（向下取整）。
    """
    if not action.save_ability:
        raise AdjudicationError(f"「{action.name}」没有豁免属性，不是豁免型动作")
    if not action.save_dc:
        raise AdjudicationError(f"「{action.name}」没有豁免 DC，引擎不自动结算这条")
    if not targets:
        raise AdjudicationError(f"「{action.name}」没有指定目标")

    out = SaveResult(label=f"{actor.name} 使用「{action.name}」",
                     save_ability=action.save_ability, save_dc=action.save_dc,
                     damage_notation=action.damage_notation)

    for t in targets:
        if not t.alive:
            out.targets.append({"name": t.name, "save_summary": "（已倒地，跳过）"})
            continue
        sr = saving_throw(t, action.save_ability, action.save_dc, rng)
        row = {"name": t.name, "save_summary": sr.summary(),
               "success": sr.check.success}
        if action.damage_notation:
            dmg_mod = _mod_total(actor, "damage")
            if sr.check.success:
                if action.save_success_half:
                    # 半伤：掷一次再折半（固定修正先加再折半）
                    amt, _rolls = _roll_damage(action.damage_notation, rng, False)
                    amt = max(0, amt + dmg_mod) // 2
                    row["damage_note"] = "豁免成功 → 半伤"
                else:
                    amt = 0
                    row["damage_note"] = "豁免成功 → 无伤害"
            else:
                amt, _rolls = _roll_damage(action.damage_notation, rng, False)
                amt = max(0, amt + dmg_mod)
            if apply_damage and amt:
                dr = t.take_damage([DamagePacket(amt, action.damage_type or "", magical)])
                row["damage_summary"] = dr.summary()
                line = damage_concentration_line(t, amt, rng, dr)
                if line:
                    row["damage_summary"] += "；" + line
            elif amt == 0:
                row["damage_summary"] = "无伤害"
        out.targets.append(row)
    return out


#: 常用难度（角色卡世界书里的 DC 六档，与 SRD 一致）
DC_TABLE = {"very_easy": 5, "easy": 10, "medium": 15,
            "hard": 20, "very_hard": 25, "nearly_impossible": 30}
DC_TABLE_CN = {"非常简单": 5, "简单": 10, "中等": 15,
               "困难": 20, "非常困难": 25, "近乎不可能": 30}


# --------------------------------------------------------------------------- #
# 施法
# --------------------------------------------------------------------------- #

@dataclass
class SpellCastResult:
    """一次施法的完整留痕。"""
    spell_name: str
    slot_level: int                  # 实际用的环阶（戏法 = 0）
    caster: str
    ok: bool = True
    blocked_reason: str = ""         # 没施成的原因（法术位不够、已失能…）
    slot_consumed: bool = False
    attack: AttackResult | None = None
    save: SaveResult | None = None
    target_rows: list[dict] = field(default_factory=list)
    concentration_started: str = ""
    concentration_check: CheckResult | None = None
    notes: list[str] = field(default_factory=list)
    needs_dm_ruling: bool = False    # 引擎不自动结算的部分

    def summary(self) -> str:
        if not self.ok:
            return f"{self.caster} 施放「{self.spell_name}」失败：{self.blocked_reason}"
        tag = "戏法" if self.slot_level == 0 else f"{self.slot_level} 环"
        head = f"{self.caster} 施放「{self.spell_name}」（{tag}）"
        if self.slot_consumed:
            head += "，消耗法术位"
        lines = [head]
        if self.attack:
            lines.append("  " + self.attack.summary())
        if self.save:
            lines.append("  " + self.save.summary().replace("\n", "\n  "))
        for r in self.target_rows:
            lines.append("  · " + r.get("summary", ""))
        if self.concentration_started:
            lines.append(f"  ⇒ 开始专注「{self.concentration_started}」")
        if self.concentration_check:
            lines.append("  " + self.concentration_check.summary())
        for n in self.notes:
            lines.append("  " + n)
        if self.needs_dm_ruling:
            lines.append("  ⚠️ 附加效果需 DM 裁定（引擎只结算了骰子与豁免）")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "spell": self.spell_name, "slot_level": self.slot_level,
            "caster": self.caster, "ok": self.ok,
            "blocked_reason": self.blocked_reason,
            "slot_consumed": self.slot_consumed,
            "attack": self.attack.to_dict() if self.attack else None,
            "save": self.save.to_dict() if self.save else None,
            "targets": self.target_rows,
            "concentration_started": self.concentration_started,
            "concentration_check": (self.concentration_check.to_dict()
                                    if self.concentration_check else None),
            "needs_dm_ruling": self.needs_dm_ruling,
            "notes": self.notes,
            "summary": self.summary(),
        }


def spell_save_dc(caster: Unit, ability: str = "charisma") -> int:
    """施法者法术豁免 DC = 8 + 熟练加值 + 施法属性调整值。"""
    return 8 + caster.proficiency + caster.mod(ability)


def spell_attack_bonus(caster: Unit, ability: str = "charisma") -> int:
    return caster.proficiency + caster.mod(ability)


def spell_max_distance(spell) -> float:
    """法术“最远能影响到的距离”。**校验在结算层也要有，不能只靠菜单。**

    · 有射程：射程 + 区域半径（火球在 150 尺处炸，边缘还会再扩 20 尺）；
    · 自身范围（锥/线/环）：区域尺寸；
    · 触碰/普通单体：5 尺。
    """
    if spell.range_ft:
        return float(spell.range_ft) + (spell.shape_size if spell.is_aoe else 0)
    if spell.is_aoe:
        return float(spell.shape_size)
    return 5.0 if spell.needs_targets else 0.0


def cast_spell(caster: Unit, spell, targets: list[Unit], rng: Random, *,
               slot_level: int | None = None, cast_ability: str = "charisma",
               has_verbal: bool = True, has_somatic: bool = True,
               material_ok: bool = True, free_cast: bool = False,
               apply_damage: bool = True,
               distance_of: Callable[[Unit, Unit], float] | None = None
               ) -> SpellCastResult:
    """结算一次施法。

    按法术的判定形态分三条路（对应 SRD 的三个字段）：
      · `attack_roll`  → 法术攻击检定（打单体）
      · `save_ability` → 目标豁免（AoE 时逐个结算）
      · 两者都无、但有伤害/治疗 → 直接结算（魔法飞弹、治疗术…）

    **法术位由引擎扣**（`slot_consumed`），扣不动就直接拒绝 —— 不让模型越过资源约束。

    引擎**不自动结算**的部分（`needs_dm_ruling=True`）：控制效果的持续轮数、
    召唤物、变形、传送落点等。这些只把骰子与豁免算清楚，效果描述交 DM。
    """
    lv = spell.level if slot_level is None else int(slot_level)
    res = SpellCastResult(spell_name=spell.name, slot_level=lv, caster=caster.name)

    # ---- 能不能施 ----
    if not caster.alive:
        res.ok, res.blocked_reason = False, "施法者已倒地"
        return res
    if caster.is_incapacitated:
        res.ok, res.blocked_reason = False, "施法者处于失能状态"
        return res
    if has_verbal and caster.has_condition_effect("cant_speak") \
            and "V" in (spell.components or ""):
        res.ok, res.blocked_reason = False, "不能说言语成分（当前状态封口）"
        return res
    if "V" in (spell.components or "") and not has_verbal:
        res.ok, res.blocked_reason = False, "这个法术需要言语成分"
        return res
    if "S" in (spell.components or "") and not has_somatic:
        res.ok, res.blocked_reason = False, "这个法术需要姿势成分"
        return res
    if "M" in (spell.components or "") and not material_ok:
        res.ok, res.blocked_reason = False, "缺少材料成分"
        return res
    # ---- 环阶合法性（先验：任何状态改动之前）----
    if lv < 0 or lv > 9 or (spell.is_cantrip and lv != 0) \
            or (not spell.is_cantrip and lv == 0):
        res.ok, res.blocked_reason = False, f"环阶不合法：{lv}（戏法只能 0 环）"
        return res
    if lv > 0 and lv < spell.level:
        res.ok, res.blocked_reason = False, f"{lv} 环低于该法术最低环阶 {spell.level}"
        return res

    # ---- 目标 / 距离合法性（先验：失败不扣法术位、动作、专注）----
    # 治疗/临时生命**可以选中 0 HP 的濒死同伴**（救人是治疗的核心用途）；
    # 伤害/攻击/豁免仍然只认活着的目标。
    heal_like = bool(spell.heal_roll or spell.temp_hp_roll)
    alive = [t for t in targets if t.alive]
    pool = list(targets) if heal_like else list(alive)
    if not heal_like:
        for t in targets:
            if not t.alive:
                res.notes.append(f"目标 {t.name} 已倒地，跳过")
    if distance_of is not None and (spell.needs_targets or spell.is_aoe):
        maxd = spell_max_distance(spell)
        if maxd:
            alive = [t for t in alive
                     if distance_of(caster, t) <= maxd + 1e-9]
            pool = [t for t in pool
                    if distance_of(caster, t) <= maxd + 1e-9]
    needs_effect = bool(spell.attack_roll or spell.save_ability
                        or spell.damage_roll or spell.heal_roll
                        or spell.temp_hp_roll)
    if needs_effect and not pool:
        res.ok, res.blocked_reason = False, "没有可结算的目标（射程/范围内为空）"
        return res
    if spell.needs_targets and not pool:
        res.ok, res.blocked_reason = False, "这个法术需要目标，但没给"
        return res

    can, why = caster.can_take(spell.slot)
    if not can:
        res.ok, res.blocked_reason = False, why
        return res

    # ---- 扣法术位（戏法不扣）----
    if lv > 0 and not free_cast:
        if not caster.use_spell_slot(lv):
            have = caster.spell_slots.get(lv)
            res.ok = False
            res.blocked_reason = (f"{lv} 环法术位不足"
                                  f"（现有 {have['current'] if have else 0}）")
            return res
        res.slot_consumed = True

    # ---- 消耗动作槽 ----
    caster.spend(spell.slot)

    # ---- 专注：开始新专注会打断旧的 ----
    if spell.concentration:
        if caster.concentrating_on:
            res.notes.append(f"打断了对「{caster.concentrating_on}」的专注")
        caster.concentrating_on = spell.name
        res.concentration_started = spell.name

    # ---- 三条判定路 ----
    if spell.attack_roll:
        bonus = spell_attack_bonus(caster, cast_ability) + _mod_total(caster, "attack")
        n = spell.projectiles_at(lv) if hasattr(spell, "projectiles_at") else 1
        first = None
        for _k in range(n):
            tgt = next((t for t in alive if t.alive), None)
            if tgt is None:
                break
            d = distance_of(caster, tgt) if distance_of else 5.0
            ar = attack_roll(caster, tgt, bonus, tgt.ac_total, rng,
                             ranged=not spell.is_melee_attack,
                             distance=d,
                             label=f"{caster.name} 的「{spell.name}」法术攻击")
            if first is None:
                first = ar
            if ar.hit and spell.damage_roll and apply_damage:
                dmg_expr = spell.damage_at(lv, caster.level)
                amt, rolls = _roll_damage(dmg_expr, rng, ar.crit)
                amt = max(0, amt + _mod_total(caster, "damage"))
                dr = tgt.take_damage([DamagePacket(amt, spell.damage_type,
                                                   magical=True)])
                line = damage_concentration_line(tgt, amt, rng, dr)
                if line:
                    res.notes.append(line)
                # ⚠️ 伤害必须写回 AttackResult，否则台账里 damage_total 恒为 0
                ar.damage_notation = dmg_expr
                ar.damage_type = spell.damage_type
                ar.damage_total = amt
                ar.damage_rolls = list(rolls)
                ar.damage_result = dr
                res.target_rows.append({
                    "name": tgt.name, "hit": True, "damage": amt,
                    "summary": f"{tgt.name} 受到 {amt} {spell.damage_type}（法术）；{dr.summary()}"})
            elif not ar.hit:
                res.target_rows.append({"name": tgt.name, "hit": False,
                                        "summary": f"{tgt.name} 未命中"})
            else:
                res.target_rows.append({"name": tgt.name, "hit": True,
                                        "summary": f"{tgt.name} 被命中（无伤害结算）"})
        res.attack = first

    elif spell.save_ability:
        dc = spell_save_dc(caster, cast_ability)
        act = Action(name=spell.name, slot=spell.slot,
                     save_ability=spell.save_ability, save_dc=dc,
                     save_success_half=spell.save_success_half,
                     damage_notation=spell.damage_at(lv, caster.level),
                     damage_type=spell.damage_type)
        sr = resolve_save_action(caster, list(alive), act, rng,
                                 apply_damage=apply_damage, magical=True)
        res.save = sr
        for row in sr.targets:
            res.target_rows.append({
                "name": row.get("name", ""),
                "summary": f"{row.get('name','')}：{row.get('save_summary','')}"
                           + (f" ⇒ {row['damage_summary']}" if row.get("damage_summary") else "")})

    else:
        # 无判定：直接伤害 / 治疗 / 临时生命（魔法飞弹、治疗术…）
        if spell.damage_roll and apply_damage:
            n = spell.projectiles_at(lv) if hasattr(spell, "projectiles_at") else 1
            dmg_expr = spell.damage_at(lv, caster.level)
            for _k in range(n):
                tgt = next((t for t in alive if t.alive), None)
                if tgt is None:
                    break
                amt, _rolls = _roll_damage(dmg_expr, rng, False)
                amt = max(0, amt + _mod_total(caster, "damage"))
                dr = tgt.take_damage([DamagePacket(amt, spell.damage_type,
                                                   magical=True)])
                line = damage_concentration_line(tgt, amt, rng, dr)
                if line:
                    res.notes.append(line)
                res.target_rows.append({
                    "name": tgt.name, "hit": True, "damage": amt,
                    "summary": f"{tgt.name} 受到 {amt} {spell.damage_type}；{dr.summary()}"})
        else:
            for t in pool:
                row = {"name": t.name}
                if spell.heal_roll:
                    expr = spell.heal_at(lv)
                    amt, _rolls = _roll_damage(expr, rng, False)
                    bonus = caster.mod(cast_ability) if spell.heal_adds_modifier else 0
                    got = t.heal(amt + bonus)
                    row["summary"] = (f"{t.name} 回复 {got} 点生命"
                                      f"（{expr}{f' {bonus:+d} 施法属性' if bonus else ''}）"
                                      f" ⇒ HP {t.hp}/{t.hp_max}")
                elif spell.temp_hp_roll:
                    amt, _rolls = _roll_damage(spell.temp_hp_roll, rng, False)
                    got = t.grant_temp_hp(amt)
                    row["summary"] = f"{t.name} 获得 {got} 点临时生命（共 {t.temp_hp}）"
                else:
                    row["summary"] = f"{t.name}：法术生效（效果由 DM 裁定）"
                res.target_rows.append(row)

    # ---- 引擎不自动结算的部分 ----
    if not spell.structured:
        res.needs_dm_ruling = True
        res.notes.append(spell.unstructured_reason or "该法术没有可自动结算的机械字段")
    return res


def concentration_check(unit: Unit, damage_taken: int, rng: Random) -> CheckResult:
    """专注检定：受伤时 DC = max(10, 伤害的一半)，体质豁免。"""
    dc = min(30, max(10, int(damage_taken) // 2))
    return saving_throw(unit, "constitution", dc, rng)


def break_concentration(unit: Unit, *, reason: str = "") -> str:
    """打断专注。返回被中断的法术名（没有则空串）。"""
    was = unit.concentrating_on
    unit.concentrating_on = ""
    if was and reason:
        unit.notes.append(f"专注「{was}」因{reason}中断")
    return was

