# coding=utf-8
"""回合执行：把一条「意图」变成战场上实际发生的事。

对应决策记录 §5.12.1 的 M5（调用编排）里**不调模型的那一半**，
以及 §5.7 里"引擎该兜底的部分"。

这一层负责把 P1/P2 的零件串起来：
    Intent（模型/本地 AI 给的） → 合法性检查 → 投骰结算 → 改状态 → 写台账

四个规则在这里补齐（P1 时列为未做）：
    · **多攻（Multiattack）** 展开成 n 次攻击检定
    · **借机攻击**：离开敌人触及范围时触发
    · **死亡豁免**：HP 归零后每回合掷 d20，3 成功稳定 / 3 失败死亡
    · **移动**：按格步进、记录路径、检查借机

⚠️ 一条纪律：**这一层绝不"猜"**。意图不合法就返回失败并说明原因，
不静默改成别的事（那会让"模型输出崩了"看起来像"单位没行动"）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..dice import Random, roll, roll_d20
from .economy import EconomyError, consumable_spec
from .adjudicate import (
    AdjudicationError, AttackResult, SpellCastResult, cast_spell, grid_distance_ft,
    resolve_attack, resolve_save_action,
)
from .menu import Menu, Option, spell_reach
from .reactions import (
    CONDITION_CN, ReadyTrigger, consume_ready, get_ready, ready_weapon,
    shield_spell,
)
from .unit import Attack, DamagePacket, Unit

__all__ = [
    "TurnOutcome", "MoveResult", "DeathSaveResult",
    "execute_intent", "expand_multiattack", "move_unit",
    "execute_special_action", "death_save", "DeathState",
    "OpportunityAttack",
]


# --------------------------------------------------------------------------- #
# 结果容器
# --------------------------------------------------------------------------- #

@dataclass
class TurnOutcome:
    """一个单位执行一条意图的完整结果。"""
    actor: str
    option_id: str = ""
    option_name: str = ""
    kind: str = ""
    ok: bool = True
    blocked: str = ""                      # 不合法/不成立的原因
    lines: list[str] = field(default_factory=list)
    attacks: list[AttackResult] = field(default_factory=list)
    save_results: list = field(default_factory=list)
    #: kind == "spell" 时的那次施法留痕（含法术位、豁免、伤害、专注）
    spell_cast: SpellCastResult | None = None
    moved: float = 0.0
    opportunity: list["OpportunityAttack"] = field(default_factory=list)
    #: 准备动作在这次行动里被触发的结果
    readied: list["OpportunityAttack"] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    needs_dm_ruling: bool = False

    def add(self, s: str) -> None:
        self.lines.append(s)

    def summary(self) -> str:
        head = f"{self.actor}"
        if self.option_name:
            head += f" 执行「{self.option_name}」"
        if not self.ok:
            return f"{head} —— ✗ {self.blocked}"
        return head + ("\n  " + "\n  ".join(self.lines) if self.lines else " —— 无实际效果")


@dataclass
class OpportunityAttack:
    """一次借机攻击 / 准备动作触发。"""
    attacker: str
    target: str
    result: AttackResult | None = None
    note: str = ""
    kind: str = "opportunity"        # opportunity / ready

    def line(self) -> str:
        tag = "🎯 准备动作" if self.kind == "ready" else "⚔ 借机攻击"
        head = f"{tag}：{self.attacker} → {self.target}"
        if self.result is not None:
            return f"{head}｜{self.result.summary()}"
        return f"{head}（{self.note}）"


@dataclass
class MoveResult:
    from_pos: tuple[float, float]
    to_pos: tuple[float, float]
    requested: float
    moved: float
    blocked: str = ""
    #: 移动**进入**某个敌人准备动作的射程时触发的反击
    readied: list[OpportunityAttack] = field(default_factory=list)

    def line(self) -> str:
        s = (f"移动 {self.moved:g}/{self.requested:g} 尺"
             f"（{self.from_pos} → {self.to_pos}）")
        if self.blocked:
            s += f"；{self.blocked}"
        return s


# --------------------------------------------------------------------------- #
# 多攻
# --------------------------------------------------------------------------- #

_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def expand_multiattack(unit: Unit, desc: str,
                       culprit_prefix: str = "") -> list[Attack]:
    """把 `Multiattack` 的描述展开成具体攻击列表。

    支持 SRD 的两种写法：
      · `The dragon makes three Rend attacks.`      → 3 × Rend
      · `makes two Tentacle attacks and uses ...`   → 2 × Tentacle（后半段忽略）

    ⚠️ **抽不到就返回空列表**，让调用方明确说"这条引擎不自动结算" ——
    不要猜次数（猜错会让怪凭空多打或少打）。
    """
    text = desc or ""
    m = re.search(r"makes?\s+(one|two|three|four|five|\d+)\s+"
                  r"([A-Za-z][A-Za-z'’\- ]*?)\s+attacks?", text, re.I)
    if not m:
        return []
    raw_n, kind = m.group(1).lower(), m.group(2).strip()
    n = _WORD_NUM.get(raw_n) or (int(raw_n) if raw_n.isdigit() else 0)
    if n <= 0:
        return []

    # 在单位已有的动作里找同名攻击
    want = re.sub(r"\s+", " ", kind).strip().lower()
    pool: list[tuple[str, Attack]] = []
    for act in unit.actions:
        an = re.sub(r"\s+", " ", act.name).strip().lower()
        for atk in act.attacks:
            an2 = re.sub(r"\s+", " ", atk.name).strip().lower()
            if want and (want in an or an in want or want in an2 or an2 in want):
                pool.append((act.name, atk))
    if not pool:
        return []
    _name, atk = pool[0]
    return [atk for _ in range(n)]


# --------------------------------------------------------------------------- #
# 移动 + 借机攻击
# --------------------------------------------------------------------------- #

def move_unit(unit: Unit, to_pos: tuple[float, float], battlefield: list[Unit],
              rng: Random, *, ignore_opportunity: bool = False,
              max_ft: float | None = None) -> tuple[MoveResult, list[OpportunityAttack]]:
    """把单位移到 `to_pos`，计算实际移动距离，并结算**借机攻击**。

    借机攻击的触发口径（2024）：**离开敌人的触及范围**时，该敌人可用反应打一次。
    · 触发者必须还有反应（`can_take("reaction")`）；
    · `ignore_opportunity=True`（脱离动作 Disengage）时不触发；
    · 移动距离按格距计算，超过剩余移动力则**只走到能走的距离**。

    返回 (移动结果, 借机攻击列表)。
    """
    budget = unit.speed - unit.movement_used
    if max_ft is not None:
        budget = min(budget, max_ft)
    if budget <= 0:
        return MoveResult(unit.position, unit.position, 0, 0,
                          "没有剩余移动力"), []

    from_pos = unit.position
    requested = grid_distance_ft(from_pos, to_pos)
    if requested <= budget:
        target = to_pos
        moved = requested
    else:
        # 只走到预算内的比例位置（按格取整）
        steps = int(budget // 5)
        if steps <= 0:
            return MoveResult(from_pos, from_pos, requested, 0,
                              "剩余移动力不足一格"), []
        dx = to_pos[0] - from_pos[0]
        dy = to_pos[1] - from_pos[1]
        norm = max(abs(dx), abs(dy)) or 1
        target = (from_pos[0] + round(dx / norm * steps) * 5,
                  from_pos[1] + round(dy / norm * steps) * 5)
        moved = grid_distance_ft(from_pos, target)

    res = MoveResult(from_pos, target, requested, moved)

    # ---- 借机攻击：谁在起点够得着他、终点够不着 ----
    opps: list[OpportunityAttack] = []
    if not ignore_opportunity and moved > 0:
        for foe in battlefield:
            if foe.id == unit.id or not foe.alive:
                continue
            if foe.team == unit.team:
                continue
            # 用触及最长的近战攻击来判"是否威胁到"
            reach = 5
            best: Attack | None = None
            for act in foe.actions:
                for atk in act.attacks:
                    if atk.is_melee and (atk.reach or 5) >= reach:
                        reach = atk.reach or 5
                        best = atk
            if best is None:
                continue
            was_in = grid_distance_ft(from_pos, foe.position) <= reach
            still_in = grid_distance_ft(target, foe.position) <= reach
            if not (was_in and not still_in):
                continue
            can, why = foe.can_take("reaction")
            if not can:
                opps.append(OpportunityAttack(foe.name, unit.name, None, why))
                continue
            try:
                ar = resolve_attack(
                    foe, unit, best, rng, distance=reach,
                    target_dodging=bool(unit.used.get("dodging")),
                    on_hit=lambda r, f=foe: _maybe_shield(unit, r, f, rng))
                foe.spend("reaction")
                opps.append(OpportunityAttack(foe.name, unit.name, ar))
            except AdjudicationError as exc:
                opps.append(OpportunityAttack(foe.name, unit.name, None, str(exc)))

    unit.position = target
    unit.movement_used += moved

    # ---- 准备动作：移动**进入**某个敌人的射程/触及 ----
    if moved > 0:
        for foe in battlefield:
            if foe.id == unit.id or not foe.alive or foe.team == unit.team:
                continue
            trig = get_ready(foe, "enters_reach")
            if trig is None:
                continue
            weapon = ready_weapon(foe, trig)
            if weapon is None:
                continue
            _act, atk = weapon
            reach = (atk.range_normal or 0) if not atk.is_melee else (atk.reach or 5)
            reach = reach or 5
            was_out = grid_distance_ft(from_pos, foe.position) > reach
            now_in = grid_distance_ft(target, foe.position) <= reach
            if not (was_out and now_in):
                continue
            can, why = foe.can_take("reaction")
            if not can:
                res.readied.append(OpportunityAttack(
                    foe.name, unit.name, None, why, kind="ready"))
                continue
            try:
                ar = resolve_attack(
                    foe, unit, atk, rng,
                    distance=grid_distance_ft(foe.position, target),
                    target_dodging=bool(unit.used.get("dodging")),
                    on_hit=lambda r, f=foe: _maybe_shield(unit, r, f, rng))
                foe.spend("reaction")
                consume_ready(foe, trig)
                res.readied.append(OpportunityAttack(
                    foe.name, unit.name, ar, kind="ready"))
            except AdjudicationError as exc:
                res.readied.append(OpportunityAttack(
                    foe.name, unit.name, None, str(exc), kind="ready"))

    return res, opps


# --------------------------------------------------------------------------- #
# 死亡豁免
# --------------------------------------------------------------------------- #

@dataclass
class DeathState:
    """一个单位的濒死进度。**HP 归零后才有意义。**"""
    successes: int = 0
    failures: int = 0
    stable: bool = False
    dead: bool = False

    def line(self) -> str:
        if self.dead:
            return "已死亡"
        if self.stable:
            return "已稳定（脱离濒死）"
        return f"濒死：成功 {self.successes}/3，失败 {self.failures}/3"


@dataclass
class DeathSaveResult:
    roll: int
    natural: int
    outcome: str                 # success / failure / crit_success / crit_failure / revive
    state: DeathState
    note: str = ""

    def line(self) -> str:
        return (f"死亡豁免 d20={self.natural} → {self.outcome}"
                f"（{self.state.line()}）" + (f"；{self.note}" if self.note else ""))


def death_save(unit: Unit, state: DeathState, rng: Random) -> DeathSaveResult:
    """HP 为 0 时的死亡豁免（2024 规则）。

    · 天然 20 → **回复 1 点 HP**，立刻脱离濒死
    · 天然 1  → **记 2 次失败**
    · ≥10     → 1 次成功；3 次成功 → 稳定
    · <10     → 1 次失败；3 次失败 → 死亡
    · 已经稳定或已死亡 → 不再掷
    """
    if state.dead:
        return DeathSaveResult(0, 0, "already_dead", state, "已死亡，不再掷")
    if state.stable:
        return DeathSaveResult(0, 0, "already_stable", state, "已稳定，不再掷")
    if unit.hp > 0:
        return DeathSaveResult(0, 0, "not_dying", state, "HP 大于 0，不需要死亡豁免")

    r = roll_d20(rng, 0)
    nat = r.natural or 0

    if nat == 20:
        unit.hp = max(1, unit.hp + 1)
        state.successes = 0
        state.failures = 0
        return DeathSaveResult(r.total, nat, "crit_success", state,
                               f"天然 20 → 回复 1 点 HP（现在 {unit.hp}）")
    if nat == 1:
        state.failures += 2
        out = "crit_failure"
        note = "天然 1 → 记 2 次失败"
    elif r.total >= 10:
        state.successes += 1
        out, note = "success", "成功 1 次"
    else:
        state.failures += 1
        out, note = "failure", "失败 1 次"

    if state.successes >= 3:
        state.stable = True
        note += "；3 次成功 → 已稳定"
    if state.failures >= 3:
        state.dead = True
        note += "；3 次失败 → 死亡"
    return DeathSaveResult(r.total, nat, out, state, note)


# --------------------------------------------------------------------------- #
# 执行一条意图
# --------------------------------------------------------------------------- #

def _heal_target_ok(caster: Unit, target: Unit) -> bool:
    """治疗只能落在自己或同阵营身上；**倒地队友也能救**。"""
    return target.team == caster.team


def _require_slot(unit: Unit, slot: str, out: TurnOutcome) -> bool:
    """通用动作的**再校验**：动作槽已用 / 失能 就拒（并写清原因）。

    为什么每个分支都要调：`execute_intent` 的口径是"不信任上游已经校验过"。
    敌人规划一次给两步、前端复用同一份菜单、测试手工造意图时，
    那份菜单可能早就过期了 —— 只靠菜单层会把第二次动作放过去。
    （O1 当时只修了攻击/法术，通用动作一直漏着。）
    """
    can, why = unit.can_take(slot)
    if not can:
        out.ok, out.blocked = False, why
        return False
    return True


def execute_intent(unit: Unit, intent, menu: Menu, battlefield: list[Unit],
                   rng: Random, *, apply_damage: bool = True,
                   distance_of: Callable[[Unit, Unit], float] | None = None,
                   on_use_item: Callable[[Unit, str, int], None] | None = None,
                   auto_target: bool = False
                   ) -> TurnOutcome:
    """执行一条 `Intent`，返回 `TurnOutcome`。

    `intent` 需要 `.option_id` / `.target` / `.targets`（见 models/client.Intent）。
    这里**再校验一次**合法性 —— 不信任上游已经校验过（模型可能被绕过）。
    """
    out = TurnOutcome(actor=unit.name, option_id=intent.option_id)
    if not unit.alive:
        out.ok, out.blocked = False, "已倒地"
        return out

    try:
        opt: Option = menu.by_id(intent.option_id)
    except Exception as exc:                             # noqa: BLE001
        out.ok, out.blocked = False, str(exc)
        return out
    # AI 座位容错：模型常只给法术 id 不给 target；在执行层补菜单首个
    # 合法目标。**默认关闭**，直接调执行层/真人仍严格拒绝。
    if (auto_target and opt.requires_target and not intent.target
            and opt.target_ids):
        _alive_ids = [u.id for u in battlefield
                      if u.id in opt.target_ids and u.alive]
        intent.target = (_alive_ids[0] if _alive_ids else opt.target_ids[0])
        intent.targets = [intent.target]
        out.notes.append(f"未给目标，自动选 {intent.target}")
    out.option_name, out.kind = opt.name, opt.kind

    by_id = {u.id: u for u in battlefield}
    dist = distance_of or (lambda a, b: grid_distance_ft(a.position, b.position))

    # ---------- 攻击 ----------
    if opt.kind == "attack":
        act = unit.actions[opt.action_index]
        atk = act.attacks[opt.attack_index]
        # ⚠️ 三条不同的失败要用三句不同的话说清楚，否则排查方向会被带偏：
        #    · 选项此刻没有合法目标（原目标死了/走了）→ 外层改打别人，**不是模型的错**
        #    · 有合法目标但模型没给 target        → 模型的格式问题
        #    · 给了 target 但那个单位不在了/死了   → 战场变化
        if not intent.target:
            if not opt.target_ids:
                out.ok = False
                out.blocked = (f"「{opt.name}」此刻没有合法目标"
                               f"（原目标已不在射程/已倒下）")
                return out
            out.ok = False
            out.blocked = (f"「{opt.name}」需要目标但没给"
                           f"（可选：{opt.target_ids}）")
            return out
        tgt = by_id.get(intent.target)
        if tgt is None or not tgt.alive:
            out.ok, out.blocked = False, (f"目标 {intent.target!r} 已不在战场上"
                                          f"（或已倒下）")
            return out

        # 多攻展开：动作描述里有 "makes N X attacks" 就一次打 N 下
        chain: list[Attack] = []
        if act.attacks[opt.attack_index] is not None and \
                re.search(r"\bmakes?\s+\w+\s+.*attacks?\b", act.desc or "", re.I) \
                and not act.attacks[opt.attack_index].damage_notation:
            chain = expand_multiattack(unit, act.desc)
        if not chain:
            chain = [atk]

        # 够不着 → 先移动接敌
        need = (atk.range_normal or 0) if not atk.is_melee else (atk.reach or 5)
        need = need or 5
        if dist(unit, tgt) > need:
            mv, opps = _approach(unit, tgt, need, battlefield, rng)
            out.opportunity += opps
            out.readied += mv.readied
            out.moved += mv.moved           # ⚠️ 接敌移动也要记进 outcome（否则外层看不到）
            out.add(mv.line())
            for rd in mv.readied:
                out.add(rd.line())
            if not unit.alive:
                out.ok = False
                out.blocked = "在接敌途中被准备动作击倒"
                return out
            if dist(unit, tgt) > need:
                out.ok = False
                out.blocked = (f"移动后仍够不着（距离 {dist(unit, tgt):g} 尺 > "
                               f"{need:g} 尺）")
                return out

        can, why = unit.can_take(act.slot)
        if not can:
            out.ok, out.blocked = False, why
            return out
        unit.spend(act.slot)
        _consume_recharge(unit, act)
        for i, a in enumerate(chain):
            try:
                ar = resolve_attack(
                    unit, tgt, a, rng,
                    distance=dist(unit, tgt),
                    apply_damage=apply_damage,
                    target_dodging=bool(tgt.used.get("dodging")),
                    on_hit=lambda r: _maybe_shield(tgt, r, unit, rng))
                out.attacks.append(ar)
                out.add(ar.summary())
            except AdjudicationError as exc:
                out.add(f"[第 {i + 1} 次攻击跳过] {exc}")
                break
            if not tgt.alive:
                out.add(f"{tgt.name} 已倒下，停止后续攻击")
                break
        # 使用次数记账
        if act.uses_per_day:
            unit.use_resource(f"action:{act.name}")
        # 被攻击者的「敌人攻击我」准备动作（反应还没被护盾术用掉才会打）
        for rd in _fire_attacked_ready(tgt, unit, battlefield, rng):
            out.readied.append(rd)
            out.add(rd.line())
        return out

    # ---------- 豁免型动作 ----------
    if opt.kind == "save_action":
        act = unit.actions[opt.action_index]
        targets = _save_action_targets(opt, act, intent, by_id)
        if not targets:
            out.ok, out.blocked = False, "没有合法目标"
            return out
        # 豁免型动作也要有距离：范围尺寸或声明的射程，不能再全图结算
        limit = max(int(getattr(act, "range_ft", 0) or 0),
                    int(getattr(act, "area_size", 0) or 0))
        if limit:
            targets = [t for t in targets
                       if grid_distance_ft(unit.position, t.position) <= limit + 1e-9]
            if not targets:
                out.ok, out.blocked = False, \
                    f"「{act.name}」范围内没有目标（{limit:g} 尺）"
                return out
        try:
            sr = resolve_save_action(unit, targets, act, rng,
                                     apply_damage=apply_damage)
            out.save_results.append(sr)
            unit.spend(act.slot)
            _consume_recharge(unit, act)
            if act.uses_per_day:
                unit.use_resource(f"action:{act.name}")
            for ln in sr.summary().splitlines():
                out.add(ln)
            if not act.structured:
                out.needs_dm_ruling = True
                out.notes.append("该动作的附加效果需 DM 裁定")
        except AdjudicationError as exc:
            out.ok, out.blocked = False, str(exc)
        return out

    # ---------- 治疗型动作（DM 造的技能，H1） ----------
    if opt.kind == "heal_action":
        act = unit.actions[opt.action_index] if opt.action_index >= 0 else None
        if act is None or not getattr(act, "heal_notation", ""):
            out.ok, out.blocked = False, "找不到该治疗动作"
            return out
        # 没给目标 = 治自己；给了目标就必须是友方（倒地队友也能救）
        tgt = by_id.get(intent.target) if intent.target else unit
        if tgt is None:
            out.ok, out.blocked = False, f"目标 {intent.target!r} 已不在战场上"
            return out
        if not _heal_target_ok(unit, tgt):
            out.ok, out.blocked = False, \
                f"「{act.name}」只能治疗自己或友方（{tgt.name} 不是）"
            return out
        reach = float(getattr(act, "range_ft", 0) or 5)
        if dist(unit, tgt) > reach:
            out.ok, out.blocked = False, \
                (f"「{act.name}」够不着 {tgt.name}"
                 f"（{dist(unit, tgt):g} 尺 > {reach:g} 尺）")
            return out
        can, why = unit.can_take(act.slot)
        if not can:
            out.ok, out.blocked = False, why
            return out
        if act.uses_per_day:
            rec = unit.resources.get(f"action:{act.name}")
            if rec is not None and int(rec.get("current", 0)) < 1:
                out.ok, out.blocked = False, f"「{act.name}」今日次数已用完"
                return out
        unit.spend(act.slot)
        _consume_recharge(unit, act)
        if act.uses_per_day:
            rec = unit.resources.get(f"action:{act.name}")
            if rec is not None:
                unit.use_resource(f"action:{act.name}")
        rolled = roll(act.heal_notation, rng=rng)
        got = tgt.heal(int(rolled.total))
        out.add(f"{unit.name} 用「{act.name}」治疗 {tgt.name}："
                f"{act.heal_notation} = {rolled.total}，回复 {got} 点"
                f"（{tgt.hp}/{tgt.hp_max}）")
        if not act.structured:
            out.needs_dm_ruling = True
            out.notes.append("该动作的附加效果需 DM 裁定")
        return out

    # ---------- 施法 ----------
    if opt.kind == "spell":
        sp = _find_spell(unit, opt.spell_key or opt.name)
        if sp is None:
            out.ok = False
            out.blocked = (f"菜单里的法术「{opt.name}」在 {unit.name} 的法术书里找不到了")
            return out

        # 槽位再校验一次（菜单可能是上一刻生成的）
        can, why = unit.can_take(sp.slot)
        if not can:
            out.ok, out.blocked = False, why
            return out

        # 治疗/临时生命可以选中 0 HP 的濒死同伴（与 cast_spell 的口径一致）；
        # 伤害/控制仍只认活着的人。
        heal_like = bool(sp.heal_roll or sp.temp_hp_roll)
        live = [by_id[t] for t in (intent.targets
                                   or ([intent.target] if intent.target else []))
                if t in by_id and (by_id[t].alive or heal_like)]
        # AoE / 多目标：以菜单候选为准，不因模型只给一个目标就缩圈
        if opt.max_targets > 1 or not opt.requires_target:
            cand = [by_id[t] for t in opt.target_ids
                    if t in by_id and (by_id[t].alive or heal_like)]
            if cand or not live:
                live = cand

        if sp.needs_targets and not live:
            out.ok = False
            out.blocked = (f"「{sp.name}」需要目标"
                           + (f"（射程内可选：{opt.target_ids}）" if opt.target_ids
                              else "，但射程内没有合法目标"))
            return out

        # 够不着 → 先移动（和攻击走同一条路），**移动后仍够不着就不施法**
        need = spell_reach(sp)
        if need and live:
            if dist(unit, live[0]) > need:
                mv, opps = _approach(unit, live[0], need, battlefield, rng)
                out.opportunity += opps
                out.moved += mv.moved
                out.add(mv.line())
                live = [t for t in live if t.alive]
                if not live:
                    out.ok, out.blocked = False, "目标在接近途中倒下了"
                    return out
                if dist(unit, live[0]) > need:
                    out.ok = False
                    out.blocked = (f"移动后仍够不着（距离 {dist(unit, live[0]):g} 尺 > "
                                   f"{need:g} 尺）")
                    return out

        sr = cast_spell(unit, sp, live, rng,
                        slot_level=opt.slot_level,
                        cast_ability=(getattr(unit, "cast_ability", "") or "charisma"),
                        apply_damage=apply_damage,
                        distance_of=dist)
        out.spell_cast = sr
        for ln in sr.summary().splitlines():
            out.add(ln)
        out.needs_dm_ruling = bool(sr.needs_dm_ruling)
        if not sr.ok:
            out.ok, out.blocked = False, sr.blocked_reason
        return out

    # ---------- 喝药（U12：2024 规则 = 附赠动作） ----------
    if opt.kind == "use_item":
        slot = str(getattr(opt, "slot", "") or "bonus_action")
        if slot not in ("bonus_action", "action", "free"):
            slot = "bonus_action"
        if slot != "free" and not _require_slot(unit, slot, out):
            return out
        name = str(getattr(opt, "item", "") or "").strip()
        info = (unit.consumables or {}).get(name)
        if not isinstance(info, dict) or int(info.get("数量") or 0) <= 0:
            out.ok, out.blocked = False, f"背包里没有「{name}」了"
            return out
        formula = str(info.get("治疗") or "").strip()
        if not formula:
            try:
                formula = str(consumable_spec(name).get("治疗") or "")
            except EconomyError:
                formula = ""      # 查不到数值 → 不当成失败：照常消耗，交 DM 裁定
        slot_cn = {"bonus_action": "附赠动作", "action": "动作",
                   "free": "自由动作"}.get(slot, slot)
        healed = 0
        rolled_text = ""
        if formula:
            rolled = roll(formula, rng=rng)
            healed = unit.heal(int(rolled.total))
            rolled_text = f"：{formula} = {rolled.total}"
        left = int(info.get("数量") or 0) - 1
        if left > 0:
            info["数量"] = left
        else:
            unit.consumables.pop(name, None)
        if slot != "free":
            unit.spend(slot)
        if on_use_item is not None:
            on_use_item(unit, name, max(0, left))
        tail = "，这件用完" if left <= 0 else f"，还剩 {left}"
        if formula:
            out.add(f"{unit.name} 用{slot_cn}使用「{name}」{rolled_text}，"
                    f"回复 {healed} 点（{unit.hp}/{unit.hp_max}{tail}）")
        else:
            out.add(f"{unit.name} 用{slot_cn}使用「{name}」{tail}；"
                    f"**数值效果引擎不认识，需 DM 裁定**")
            out.needs_dm_ruling = True
        return out

    # ---------- 移动 ----------
    if opt.kind == "move":
        tgt = by_id.get(intent.target) if intent.target else None
        if tgt is None:
            # 没给目标 → 靠向最近的敌人（本地兜底也走这条路）
            foes = [u for u in battlefield
                    if u.alive and u.id != unit.id and u.team != unit.team]
            if not foes:
                out.ok, out.blocked = False, "没有敌人可接近"
                return out
            tgt = min(foes, key=lambda u: dist(unit, u))
        dest = _step_point(unit.position, tgt.position, unit.speed - unit.movement_used)
        mv, opps = move_unit(
            unit, dest, battlefield, rng,
            ignore_opportunity=bool(unit.used.get("disengaged")))
        out.moved, out.opportunity, out.readied = mv.moved, opps, mv.readied
        out.add(mv.line())
        for rd in mv.readied:
            out.add(rd.line())
        return out

    # ---------- 通用动作 ----------
    if opt.kind == "dash":
        if not _require_slot(unit, "action", out):
            return out
        unit.spend("action")
        # 允许 movement_used 变负：负值就是"额外移动力"，预算 = speed - used。
        # 先 `max(0, …)` 会把赠送的移动力抹掉（先疾走再移动只有 30 尺）。
        _grant_dash(unit)
        out.add(f"疾走：本回合移动力 +{unit.speed:g} 尺")
        return out

    if opt.kind == "disengage":
        if not _require_slot(unit, "action", out):
            return out
        unit.spend("action")
        unit.used["disengaged"] = 1
        out.add("脱离：本回合移动不引发借机攻击")
        return out

    if opt.kind == "dodge":
        if not _require_slot(unit, "action", out):
            return out
        unit.spend("action")
        unit.used["dodging"] = 1
        out.add("回避：直到你下回合开始，攻击你的人有劣势")
        return out

    if opt.kind == "hide":
        if not _require_slot(unit, "action", out):
            return out
        unit.spend("action")
        out.add("躲藏：做一次敏捷（隐匿）检定对抗对方被动察觉（由 DM/引擎裁定）")
        out.needs_dm_ruling = True
        return out

    if opt.kind == "ready_attack":
        if not _require_slot(unit, "action", out):
            return out
        if opt.ready_condition not in CONDITION_CN:
            out.ok, out.blocked = False, f"不认识的触发条件：{opt.ready_condition!r}"
            return out
        weapon = None
        try:
            weapon = (unit.actions[opt.action_index],
                      unit.actions[opt.action_index].attacks[opt.attack_index])
        except (IndexError, TypeError):
            weapon = None
        if weapon is None:
            out.ok, out.blocked = False, "准备动作指向的攻击不存在"
            return out
        _act, atk = weapon
        unit.spend("action")
        unit.reactions.append(ReadyTrigger(
            condition=opt.ready_condition,
            action_index=opt.action_index,
            attack_index=opt.attack_index,
            label=atk.name))
        out.add(f"准备：{CONDITION_CN[opt.ready_condition]} → 用「{atk.name}」"
                f"以反应反击（到你的下个回合开始前有效）")
        return out

    if opt.kind in ("help", "ready"):
        if not _require_slot(unit, "action", out):
            return out
        unit.spend("action")
        out.add(f"{opt.name}：已声明，效果由 DM 裁定")
        out.needs_dm_ruling = True
        return out

    if opt.kind == "special":
        act = unit.actions[opt.action_index] if opt.action_index >= 0 else None
        if act is None:
            out.ok, out.blocked = False, "找不到该动作"
            return out
        # ⚠️ 通用动作也要**再校验动作槽**（O1 当时只修了攻击/法术；
        #    敌人规划一次给两步、或测试复用同一份菜单时会白嫖第二次）
        if not _require_slot(unit, act.slot, out):
            return out
        # ---- Multiattack：把"makes N X attacks"真正展开成 N 次攻击 ----
        if (act.damage_notation or "").startswith("__multiattack__"):
            chain = expand_multiattack(unit, act.desc)
            tgt = by_id.get(intent.target) if intent.target else None
            if tgt is None or not tgt.alive:
                foes = [t for t in battlefield
                        if t.alive and t.id != unit.id and t.team != unit.team]
                tgt = min(foes, key=lambda t: dist(unit, t)) if foes else None
            if not chain or tgt is None:
                out.ok, out.blocked = False, "Multiattack 没有可执行的具体攻击"
                return out
            unit.spend(act.slot)
            _consume_recharge(unit, act)
            if act.uses_per_day:
                unit.use_resource(f"action:{act.name}")
            for i, a in enumerate(chain):
                try:
                    ar = resolve_attack(
                        unit, tgt, a, rng,
                        distance=dist(unit, tgt),
                        apply_damage=apply_damage,
                        target_dodging=bool(tgt.used.get("dodging")),
                        on_hit=lambda r: _maybe_shield(tgt, r, unit, rng))
                    out.attacks.append(ar)
                    out.add(ar.summary())
                except AdjudicationError as exc:
                    out.add(f"[第 {i + 1} 次攻击跳过] {exc}")
                    break
                if not tgt.alive:
                    out.add(f"{tgt.name} 已倒下，停止后续攻击")
                    break
            return out
        # 每日次数：登记了资源才判"用完"，没登记的不假装有次数
        rec = unit.resources.get(f"action:{act.name}")
        if act.uses_per_day and rec is not None and int(rec.get("current", 0)) < 1:
            out.ok, out.blocked = False, f"「{act.name}」今日次数已用完"
            return out
        unit.spend(act.slot)
        if act.uses_per_day:
            unit.use_resource(f"action:{act.name}")
        out.add(f"{act.name}：{act.desc[:120]}")
        out.needs_dm_ruling = True
        out.notes.append("引擎不自动结算这条动作")
        return out

    if opt.kind == "end_turn":
        out.add("结束回合")
        return out

    out.ok, out.blocked = False, f"引擎还不支持这类选项：{opt.kind}"
    return out


def execute_special_action(unit: Unit, action, battlefield: list[Unit],
                           rng: Random, *, apply_damage: bool = True
                           ) -> TurnOutcome:
    """执行一个**传奇 / 巢穴**动作（U6）。**不进菜单，由引擎挑时机插入。**

    - 传奇动作按 `action.legendary_cost` 扣点（不是固定 1 点）；
    - 巢穴动作每轮一次；
    - 攻击型走攻击检定、豁免型走豁免结算；纯描述型标记"需 DM 裁定"；
    - **没有合法目标就返回失败且不扣资源**（不白花）。
    """
    out = TurnOutcome(actor=unit.name, option_name=action.name, kind=action.slot)
    if not unit.alive:
        out.ok, out.blocked = False, "已倒地"
        return out
    can, why = unit.can_take(action.slot)
    if not can:
        out.ok, out.blocked = False, why
        return out
    if action.slot == "legendary":
        cost = max(1, int(action.legendary_cost or 1))
        if cost > unit.legendary_left:
            out.ok = False
            out.blocked = (f"「{action.name}」要 {cost} 点传奇动作，"
                           f"只剩 {unit.legendary_left} 点")
            return out

    foes = [u for u in battlefield
            if u.alive and u.id != unit.id and u.team != unit.team]
    live = [u for u in foes if u.hp > 0]

    if action.attacks:
        atk = action.attacks[0]
        need = (atk.range_normal or 0) if not atk.is_melee else (atk.reach or 5)
        need = need or 5
        in_range = [t for t in live
                    if grid_distance_ft(unit.position, t.position) <= need]
        if not in_range:
            out.ok = False
            out.blocked = f"「{action.name}」没有目标在 {need:g} 尺内"
            return out
        tgt = min(in_range, key=lambda t: (t.hp, t.id))
        _spend_special(unit, action)
        try:
            ar = resolve_attack(unit, tgt, atk, rng,
                                distance=grid_distance_ft(unit.position, tgt.position),
                                apply_damage=apply_damage,
                                target_dodging=bool(tgt.used.get("dodging")),
                                on_hit=lambda r: _maybe_shield(tgt, r, unit, rng))
            out.attacks.append(ar)
            out.add(ar.summary())
        except AdjudicationError as exc:
            out.add(f"[跳过] {exc}")
        return out

    if action.save_ability and action.save_dc:
        targets = live
        if not targets:
            out.ok = False
            out.blocked = f"「{action.name}」没有合法目标"
            return out
        _spend_special(unit, action)
        try:
            sr = resolve_save_action(unit, targets, action, rng,
                                     apply_damage=apply_damage)
            out.save_results.append(sr)
            for ln in sr.summary().splitlines():
                out.add(ln)
        except AdjudicationError as exc:
            out.ok, out.blocked = False, str(exc)
        return out

    # 纯描述型（SRD 的传奇动作大多是这种：数值在文本里，引擎不自动结算）
    _spend_special(unit, action)
    out.add(f"{action.name}：{(action.desc or '')[:160]}")
    out.needs_dm_ruling = True
    out.notes.append("引擎不自动结算这条传奇/巢穴动作")
    return out


def _save_action_targets(opt, act, intent, by_id: dict) -> list:
    """豁免型动作的目标池：AoE 必须打到全部候选，单体才只打一个。"""
    targets = [by_id[t] for t in (intent.targets or []) if t in by_id]
    if int(opt.max_targets or 1) > 1 or int(getattr(act, "area_size", 0) or 0) > 0:
        targets = [by_id[t] for t in (opt.target_ids or [])
                   if t in by_id and by_id[t].alive]
    if not targets and opt.target_ids:
        targets = [by_id[t] for t in opt.target_ids if t in by_id]
    return targets


def _grant_dash(unit: Unit) -> None:
    """疾走：把 `movement_used` 减掉一个速度（负值 = 额外移动力）。"""
    unit.movement_used -= unit.speed


def _consume_recharge(unit: Unit, action) -> None:
    """用完一条充能动作 → 本回合不可再用，下次回合初再掷。"""
    if int(getattr(action, "recharge_on_roll", 0) or 0):
        unit.resources[f"recharge:{action.name}"] = False


def _spend_special(unit: Unit, action) -> None:
    if action.slot == "legendary":
        unit.spend_legendary(max(1, int(action.legendary_cost or 1)))
    else:
        unit.spend(action.slot)
    _consume_recharge(unit, action)


def _find_spell(unit: Unit, key: str):
    """按 SRD key 或名字在单位的法术书里找法术（两者都认，找不到返回 None）。"""
    for sp in (getattr(unit, "spells", None) or []):
        if key and (sp.key == key or sp.name == key):
            return sp
    return None


def _maybe_shield(target: Unit, res: AttackResult, attacker: Unit,
                  rng: Random) -> AttackResult:
    """护盾术反应：**只在"加了 5 AC 就能把命中翻成未命中"时才花法术位**。

    口径（第一版）：
      · 天然 20 必中，翻不掉，不用；
      · 现有总值已经 ≥ 新 AC（加了也还是中）→ 不浪费法术位；
      · 翻成功就扣 1 环位 + 反应，并把这次攻击改成未命中（不再结算伤害）。
    **不保留持续的 +5 AC**（那需要"直到下回合"的 buff，第一版没做）。
    """
    if res.reaction_cast is not None or not target.alive:
        return res
    sp = shield_spell(target)
    if sp is None:
        return res
    can, _why = target.can_take("reaction")
    if not can:
        return res
    slot = target.spell_slots.get(1)
    if not slot or int(slot.get("current", 0)) <= 0:
        return res
    if res.check.a.is_crit:
        return res
    new_dc = int(res.check.dc) + 5
    if res.check.total >= new_dc:
        return res

    sr = cast_spell(target, sp, [], rng, slot_level=1,
                    cast_ability=(getattr(target, "cast_ability", "") or "charisma"),
                    apply_damage=False)
    if not sr.ok:
        return res

    res.check.dc = new_dc
    res.hit = False
    res.crit = False
    res.reaction_cast = sr
    res.reaction_note = f"护盾术：AC {new_dc - 5} → {new_dc}，这次命中改为未命中"
    return res


def _fire_attacked_ready(target: Unit, attacker: Unit, battlefield: list[Unit],
                         rng: Random) -> list[OpportunityAttack]:
    """触发「敌人攻击我」的准备动作。返回触发结果（可能为空）。"""
    if not target.alive:
        return []
    trig = get_ready(target, "attacked")
    if trig is None:
        return []
    can, _why = target.can_take("reaction")
    if not can:
        return []
    weapon = ready_weapon(target, trig)
    if weapon is None:
        return []
    _act, atk = weapon
    reach = (atk.range_normal or 0) if not atk.is_melee else (atk.reach or 5)
    reach = reach or 5
    if grid_distance_ft(target.position, attacker.position) > reach:
        return []
    try:
        ar = resolve_attack(
            target, attacker, atk, rng,
            distance=grid_distance_ft(target.position, attacker.position),
            target_dodging=bool(attacker.used.get("dodging")),
            on_hit=lambda r: _maybe_shield(attacker, r, target, rng))
    except AdjudicationError:
        return []
    target.spend("reaction")
    consume_ready(target, trig)
    return [OpportunityAttack(target.name, attacker.name, ar, kind="ready")]


def _approach(unit: Unit, target: Unit, need: float, battlefield: list[Unit],
              rng: Random) -> tuple[MoveResult, list[OpportunityAttack]]:
    """朝目标移动，停在 `need` 尺处。"""
    budget = unit.speed - unit.movement_used
    dest = _step_point(unit.position, target.position, budget, stop_at=need)
    return move_unit(unit, dest, battlefield, rng,
                     ignore_opportunity=bool(unit.used.get("disengaged")))


def _step_point(frm: tuple[float, float], to: tuple[float, float],
                budget: float, *, stop_at: float = 5.0) -> tuple[float, float]:
    """从 `frm` 朝 `to` 走最多 `budget` 尺，**停在离 to `stop_at` 尺处**。

    按 5 尺格步进，保证落在格上（战场是方格）。
    """
    d = grid_distance_ft(frm, to)
    if d <= stop_at:
        return frm
    travel = min(budget, d - stop_at)
    steps = int(travel // 5)
    if steps <= 0:
        return frm
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    norm = max(abs(dx), abs(dy)) or 1
    return (frm[0] + round(dx / norm * steps) * 5,
            frm[1] + round(dy / norm * steps) * 5)
