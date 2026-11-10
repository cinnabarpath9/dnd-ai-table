# coding=utf-8
"""合法动作菜单生成器 —— 模型唯一被允许做选择的地方。

对应决策记录 §5.8 与 §5.7：

    §5.7 说"敌人合理使用动作资源"考察六项能力，其中
        · 1 动作经济正确性   → **由本模块保证**（菜单里没有的选项，模型选不出来）
        · 2 结构化输出稳定性 → **由本模块保证**（选项带 id，模型只回 id）
        · 3~6 战术评分/资源取舍/行动排序/位置博弈 → **留给模型**（本模块不做决策）

所以本模块的职责是：**把"可选项"枚举清楚，并把非法项彻底排除掉**。
模型拿到菜单后只需要输出 `{"unit":"G1","action":"a1","target":"P2"}` 这种极短的意图。

为什么这很重要：一次调用要为 4 个敌人输出行动序列，如果让它自己判断"我这个回合
还能不能再用附赠动作"，格式一崩整个回合就废了（见 §5.10.6）。把合法性收进菜单，
模型的最弱环节（细节准确率）就不再是致命伤。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adjudicate import cover_bonus, grid_distance_ft
from .constants import ABILITY_CN, CONDITIONS
from .unit import Action, Attack, Unit

__all__ = [
    "Option", "Menu", "build_menu", "render_menu", "render_menus",
    "SLOT_CN", "MenuError", "spell_reach",
]


class MenuError(ValueError):
    """战场状态本身有问题（重复 id、没有参战单位…）时抛这个。"""


SLOT_CN = {"action": "动作", "bonus_action": "附赠动作", "reaction": "反应",
           "legendary": "传奇动作", "lair": "巢穴动作", "free": "自由动作"}


# --------------------------------------------------------------------------- #
# 选项
# --------------------------------------------------------------------------- #

@dataclass
class Option:
    """一个可选项。`id` 是模型唯一需要引用的东西。"""
    id: str
    kind: str                       # attack / save_action / spell / dash / disengage /
                                    # dodge / hide / help / ready / move / end_turn
    slot: str
    name: str
    summary: str                    # 一行说明（给模型看）
    # --- 目标 ---
    target_ids: list[str] = field(default_factory=list)
    requires_target: bool = False
    max_targets: int = 1            # >1 表示可多选（AoE）
    # --- 机械细节（引擎结算用，不必给模型看全） ---
    action_index: int = -1
    attack_index: int = -1
    #: kind == "spell" 时：法术的 key（找不到再按名字）与这一次用几环
    spell_key: str = ""
    slot_level: int = 0
    #: kind == "ready_attack" 时：触发条件（enters_reach / attacked）
    ready_condition: str = ""
    #: kind == "use_item" 时：喝哪一件（物品名；数量与效果在 Unit.consumables）
    item: str = ""
    distance: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "slot": self.slot, "name": self.name,
             "summary": self.summary, "requires_target": self.requires_target}
        if self.target_ids:
            d["targets"] = self.target_ids
        if self.max_targets > 1:
            d["max_targets"] = self.max_targets
        if self.kind == "spell":
            d["spell"] = self.name
            d["slot_level"] = self.slot_level
        if self.kind == "ready_attack":
            d["condition"] = self.ready_condition
        if self.kind == "use_item" and self.item:
            d["item"] = self.item
        if self.note:
            d["note"] = self.note
        return d

    def line(self, *, targets: str = "") -> str:
        s = f"  [{self.id}] {self.summary}"
        if self.note:
            s += f"　⚠️{self.note}"
        if targets:
            s += f"　🎯可选目标：{targets}"
        return s


@dataclass
class Menu:
    """一个单位这一回合的完整可选项。"""
    unit_id: str
    unit_name: str
    team: str
    slots_free: dict[str, bool] = field(default_factory=dict)
    movement_left: float = 0.0
    options: list[Option] = field(default_factory=list)
    #: 明确写出来的"做不到"，让模型不会去试
    blocked: list[str] = field(default_factory=list)
    #: 反应类能力（护盾术等）：**不是可选项**，满足条件时引擎自动触发
    reaction_notes: list[str] = field(default_factory=list)
    #: 传奇 / 巢穴动作：**不是可选项**，引擎按触发时机插入
    special_notes: list[str] = field(default_factory=list)

    def by_id(self, oid: str) -> Option:
        for o in self.options:
            if o.id == oid:
                return o
        raise MenuError(f"{self.unit_name} 的菜单里没有选项 {oid!r}；"
                        f"可选：{[o.id for o in self.options]}")

    def of_kind(self, kind: str) -> list[Option]:
        return [o for o in self.options if o.kind == kind]

    def to_dict(self) -> dict:
        return {"unit": self.unit_id, "name": self.unit_name, "team": self.team,
                "slots_free": self.slots_free, "movement_left": self.movement_left,
                "options": [o.to_dict() for o in self.options],
                "blocked": self.blocked,
                "reaction_notes": self.reaction_notes,
                "special_notes": self.special_notes}

    def render(self) -> str:
        """渲染成给模型看的文本块。**这是提示词里唯一允许出现的可选项来源。**

        `requires_target` 的选项把合法目标**贴在那一行**，而且只写 id：
        写名字会让模型把「E1 Giant Rat」整个抄进 `target`（M2 复验实测）。
        """
        head = (f"【{self.unit_id}】{self.unit_name}（{self.team}）"
                f"｜移动剩余 {self.movement_left:g} 尺")
        slots = " ".join(f"{SLOT_CN.get(k, k)}{'✓' if v else '✗'}"
                         for k, v in self.slots_free.items())
        lines = [head, f"  可用槽位：{slots}"]
        if self.options:
            lines.append("  可选行动：")
            for o in self.options:
                lines.append(o.line(targets=self._targets_of(o)))
        else:
            lines.append("  可选行动：**（无）**—— 该单位没有可执行的动作")
        if self.blocked:
            lines.append("  不可行：" + "；".join(self.blocked))
        if self.reaction_notes:
            lines.append("  反应（满足条件时自动触发）："
                         + "；".join(self.reaction_notes))
        if self.special_notes:
            lines.append("  特殊动作（引擎按触发时机插入）："
                         + "；".join(self.special_notes))
        return "\n".join(lines)

    @staticmethod
    def _targets_of(opt: "Option") -> str:
        """给模型看的目标串（**只到 id**）；不需要目标的选项返回空串。"""
        if not opt.requires_target or not opt.target_ids:
            return ""
        return "、".join(opt.target_ids)

    def next_id(self) -> str:
        """下一个可选 id = **现有 id 里最大数字 + 1**。

        不能用 `len(options) + 1`：攻击选项可能因为“移动后也够不着”被跳过，
        id 会留空号，个数与最大 id 就对不上了。`_add_end_turn` 与法术选项
        各写过一套发号规则，结果法术和“移动”撞了号（2026-11-08 外审 P1-1）。
        """
        nums = [int(o.id[1:]) for o in self.options if o.id[1:].isdigit()]
        return f"a{max(nums, default=0) + 1}"


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #

def _other_units(unit: Unit, battlefield: list[Unit], *, enemies_only: bool,
                 include_dying: bool = False) -> list[Unit]:
    """场上其他单位。

    `include_dying=True` 时**保留 0 HP 的濒死单位** —— 治疗/稳定/复活类效果
    必须能选中他们；敌方候选仍然只认活着的（补刀是另一条规则，见 §13）。
    """
    out = []
    for u in battlefield:
        if u.id == unit.id:
            continue
        if not u.alive and not include_dying:
            continue
        if enemies_only and u.team == unit.team:
            continue
        out.append(u)
    return out


def _is_enemy(a: Unit, b: Unit) -> bool:
    """敌我判定：pc/ally 一伙，enemy 对面，neutral 对谁都中立（可被打但不算敌人）。

    ⚠️ 这条口径要写死在一处。历史上很多 AI 项目在这里出过"友军互砍"的 bug。
    """
    good = {"pc", "ally"}
    if a.team in good and b.team in good:
        return False
    if a.team == "enemy" and b.team == "enemy":
        return False
    if a.team == "neutral" or b.team == "neutral":
        return True          # 中立单位视为可攻击对象
    return True


def _recharge_ready(unit: Unit, act) -> bool:
    """充能动作本回合能不能选。**没充上就是不可用**，别在菜单里空承诺。"""
    if not int(getattr(act, "recharge_on_roll", 0) or 0):
        return True
    return unit.resources.get(f"recharge:{act.name}") is not False


def build_menu(unit: Unit, battlefield: list[Unit], *,
               include_friendly_targets: bool = True) -> Menu:
    """为 `unit` 生成这一回合的合法动作菜单。"""
    if not unit.alive:
        raise MenuError(f"{unit.name} 已倒地，没有菜单")
    ids = [u.id for u in battlefield]
    if len(ids) != len(set(ids)):
        raise MenuError(f"战场里有重复的单位 id：{ids}")

    menu = Menu(unit_id=unit.id, unit_name=unit.name, team=unit.team,
                movement_left=max(0.0, unit.speed - unit.movement_used))

    # ---- 槽位可用性（这就是"动作经济正确性"的落点） ----
    for slot in ("action", "bonus_action", "reaction", "legendary", "lair"):
        can, why = unit.can_take(slot)
        menu.slots_free[slot] = can
        if not can and slot in ("action", "bonus_action", "reaction"):
            menu.blocked.append(f"{SLOT_CN[slot]}不可用（{why}）")

    # 传奇 / 巢穴动作**不进可选行动**：它们由引擎按触发时机插入（U6）
    if unit.legendary_actions_max > 0:
        menu.special_notes.append(
            f"传奇动作 {unit.legendary_left}/{unit.legendary_actions_max} 点："
            f"在**别人回合结束时**由引擎插入（不是轮到自己时主动用）"
            + ("（已用完）" if unit.legendary_left <= 0 else ""))
    if unit.lair_actions_max > 0:
        can, why = unit.can_take("lair")
        menu.special_notes.append(
            "巢穴动作：每轮一次，先攻 20 时由引擎触发"
            + ("" if can else f"（{why}）"))

    # 失能：什么都不能做（除了"结束回合"）
    if unit.is_incapacitated:
        menu.blocked.append("失能状态：本回合不能做任何动作")
        _add_end_turn(menu, unit)
        return menu

    # ---- 目标池 ----
    foes = [u for u in _other_units(unit, battlefield, enemies_only=True)
            if _is_enemy(unit, u)]
    friends = [u for u in _other_units(unit, battlefield, enemies_only=False,
                                      include_dying=True)
               if not _is_enemy(unit, u)]

    # ---- 1) 单位自带的动作（怪物数值块 / 角色技能） ----
    oid = 0
    for ai, act in enumerate(unit.actions):
        if act.slot in ("legendary", "lair"):
            # 触发时机由引擎掌握（别人回合结束 / 先攻 20），不列为普通选项
            continue
        if not _recharge_ready(unit, act):
            # 本回合充能没过：不列出来（否则菜单在"承诺一个引擎不会执行的规则"）
            continue
        can, why = unit.can_take(act.slot)
        if not can:
            continue

        # 使用次数限制
        if act.uses_per_day:
            key = f"action:{act.name}"
            rec = unit.resources.get(key)
            if rec is None:
                # 首次遇到就按 max 初始化（引擎自动建账，不用外部维护）
                unit.resources[key] = {"current": act.uses_per_day,
                                       "max": act.uses_per_day}
                rec = unit.resources[key]
            if rec["current"] <= 0:
                menu.blocked.append(f"「{act.name}」今日次数已用完（{act.uses_per_day}/日）")
                continue

        if act.attacks:
            # 攻击型动作：每个攻击各成一个选项
            for ti, atk in enumerate(act.attacks):
                # 远程候选按**最远射程**列，普通射程外只是劣势（不是够不着）。
                # 近战/远程都允许"选中后引擎自动移动接敌"，所以候选距离要加上
                # 本回合剩余移动力；否则菜单会给出空 target，模型选了再被拒
                #（M1 的 26 次纠错就来自这里）。
                base = (atk.reach or 5) if atk.is_melee else \
                    (atk.range_long or atk.range_normal or 5)
                reach = base + max(0.0, float(menu.movement_left or 0))
                in_range = [t for t in foes
                            if grid_distance_ft(unit.position, t.position) <= reach]
                oid += 1
                opt = Option(
                    id=f"a{oid}", kind="attack", slot=act.slot,
                    name=f"{act.name}" + (f"·{atk.name}" if len(act.attacks) > 1 else ""),
                    summary=(f"{act.name}：{atk.attack_bonus:+d} 攻击检定，"
                             f"{atk.damage_text()}，{atk.range_text()}"
                             + (f"（充能 d6≥{act.recharge_on_roll}）" if act.recharge_on_roll else "")
                             + (f"（{act.uses_per_day}/日）" if act.uses_per_day else "")),
                    target_ids=[t.id for t in in_range],
                    requires_target=True,
                    action_index=ai, attack_index=ti,
                    distance=min((grid_distance_ft(unit.position, t.position)
                                  for t in in_range), default=0.0),
                )
                if in_range and min(grid_distance_ft(unit.position, t.position)
                                    for t in in_range) > base:
                    opt.note = "选中后引擎会先移动接敌（在剩余移动力内）"
                if not in_range:
                    menu.blocked.append(
                        f"「{act.name}」移动后也够不着（{reach:g} 尺内无目标）")
                    continue
                menu.options.append(opt)
            continue

        # 治疗型（DM 造的技能，H1）：自己或友方，引擎按骰式回复
        if getattr(act, "heal_notation", ""):
            try:
                reach = float(act.range_ft or 5)
            except (TypeError, ValueError):
                reach = 5.0
            cand = [unit] + [t for t in friends
                             if grid_distance_ft(unit.position, t.position) <= reach]
            oid += 1
            menu.options.append(Option(
                id=f"a{oid}", kind="heal_action", slot=act.slot, name=act.name,
                summary=(f"{act.name}：回复 {act.heal_notation}"
                         f"（{reach:g} 尺内自己或友方）"
                         + (f"（{act.uses_per_day}/日）" if act.uses_per_day else "")),
                target_ids=[t.id for t in cand], requires_target=True,
                action_index=ai,
                note=("引擎按骰式回复；额外效果由 DM 裁定"
                      if not act.structured else "")))
            continue

        # 豁免型 AoE / 控制
        if act.save_ability and act.save_dc:
            size = act.area_size or 0
            limit = max(int(getattr(act, "range_ft", 0) or 0), size)
            cand = [t for t in foes
                    if not limit
                    or grid_distance_ft(unit.position, t.position) <= limit + 1e-9]
            if size:
                # AoE：射程/范围内的都能选；由模型挑落点（这里给候选集）
                summary = (f"{act.name}：{ABILITY_CN.get(act.save_ability, act.save_ability)}豁免 "
                           f"DC {act.save_dc}，{size} 尺{act.area_shape}"
                           + (f"，{act.damage_notation} {act.damage_type}" if act.damage_notation else "")
                           + ("（成功半伤）" if act.save_success_half else ""))
                oid += 1
                menu.options.append(Option(
                    id=f"a{oid}", kind="save_action", slot=act.slot, name=act.name,
                    summary=summary, target_ids=[t.id for t in cand],
                    requires_target=True, max_targets=max(1, len(cand)),
                    action_index=ai,
                    note=(f"充能 d6≥{act.recharge_on_roll}" if act.recharge_on_roll else ""),
                ))
            else:
                oid += 1
                menu.options.append(Option(
                    id=f"a{oid}", kind="save_action", slot=act.slot, name=act.name,
                    summary=(f"{act.name}：{ABILITY_CN.get(act.save_ability, act.save_ability)}"
                             f"豁免 DC {act.save_dc}"),
                    target_ids=[t.id for t in cand], requires_target=True,
                    action_index=ai,
                    note="引擎只结算豁免与伤害；附加效果由 DM 裁定" if not act.structured else "",
                ))
            continue

        # 纯描述型动作（回血、变身、召唤…）
        oid += 1
        menu.options.append(Option(
            id=f"a{oid}", kind="special", slot=act.slot, name=act.name,
            summary=f"{act.name}：{act.desc[:90]}" if act.desc else act.name,
            target_ids=[t.id for t in (friends if include_friendly_targets else [])],
            requires_target=False, action_index=ai,
            note="引擎不自动结算这条 —— 需 DM 裁定效果" if not act.structured else "",
        ))

    # ---- 2) 通用动作（人人都能做；2024 规则） ----
    if menu.slots_free.get("action"):
        oid += 1
        menu.options.append(Option(
            id=f"a{oid}", kind="dash", slot="action", name="疾走 Dash",
            summary="疾走：本回合额外获得等于速度的移动力"))
        oid += 1
        menu.options.append(Option(
            id=f"a{oid}", kind="disengage", slot="action", name="脱离 Disengage",
            summary="脱离：本回合的移动不会引发借机攻击"))
        oid += 1
        menu.options.append(Option(
            id=f"a{oid}", kind="dodge", slot="action", name="回避 Dodge",
            summary="回避：直到你下回合开始，攻击你的人有劣势；你的敏捷豁免有优势"))
        oid += 1
        menu.options.append(Option(
            id=f"a{oid}", kind="hide", slot="action", name="躲藏 Hide",
            summary="躲藏：做一次敏捷（隐匿）检定对抗对方被动察觉"))
        oid += 1
        menu.options.append(Option(
            id=f"a{oid}", kind="help", slot="action", name="协助 Help",
            summary="协助：让一个友方对某目标的下一次攻击有优势，或帮它挣脱状态",
            target_ids=[t.id for t in friends], requires_target=False))

        # Ready：声明"触发条件 + 准备好的攻击"，真正打出去是在别人行动时。
        ready_added = False
        for ai, act in enumerate(unit.actions):
            if act.slot != "action":
                continue
            for ti, atk in enumerate(act.attacks):
                for cond in ("enters_reach", "attacked"):
                    where = ("敌人进入触及范围" if atk.is_melee
                             else "敌人进入射程")
                    trigger = where if cond == "enters_reach" else "敌人攻击我"
                    oid += 1
                    menu.options.append(Option(
                        id=f"a{oid}", kind="ready_attack", slot="action",
                        name=f"准备·{trigger}",
                        summary=(f"准备：{trigger} → 用「{atk.name}」"
                                 f"以反应攻击它（{atk.attack_bonus:+d}，"
                                 f"{atk.damage_text()}）"),
                        action_index=ai, attack_index=ti,
                        ready_condition=cond,
                        note="触发时引擎自动执行；到你的下个回合开始仍未触发则失效"))
                    ready_added = True
        if not ready_added:
            oid += 1
            menu.options.append(Option(
                id=f"a{oid}", kind="ready", slot="action", name="准备 Ready",
                summary="准备：设定一个触发条件，条件满足时用反应执行某个动作",
                note="该单位没有可准备的攻击 —— 触发条件由 DM 裁定"))

    # ---- 附赠动作/动作：使用消耗品（U12；药水按 2024 是附赠动作） ----
    for iname, info in (unit.consumables or {}).items():
        try:
            qty = int((info or {}).get("数量") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        slot = str((info or {}).get("动作") or "bonus_action")
        if slot not in ("bonus_action", "action", "free"):
            slot = "bonus_action"
        if slot != "free" and not menu.slots_free.get(slot):
            continue
        oid += 1
        slot_cn = SLOT_CN.get(slot, slot)
        menu.options.append(Option(
            id=f"a{oid}", kind="use_item", slot=slot,
            name=f"使用物品：{iname}",
            summary=f"用{slot_cn}使用「{iname}」（剩 {qty}）",
            item=iname,
            note=("按 2024 规则：药水是附赠动作；其它物品按各自规定"
                  if slot == "bonus_action" else f"这件物品标的是{slot_cn}")))

    # 附赠动作：只有单位自带 bonus_action 动作时才列（已在上面处理）。
    # 通用附赠动作（如"副手攻击"）需要双持，第一版不自动给，避免凭空造能力。
    if menu.slots_free.get("bonus_action") and not any(
            o.slot == "bonus_action" for o in menu.options):
        menu.blocked.append("附赠动作可用，但该单位没有附赠动作能力"
                            "（双武器攻击等特例未在第一版实现）")

    # ---- 3) 移动 ----
    if menu.movement_left > 0:
        oid += 1
        menu.options.append(Option(
            id=f"a{oid}", kind="move", slot="free", name="移动 Move",
            summary=f"移动：最多 {menu.movement_left:g} 尺"
                    f"（战场按 5 尺一格；对角算 1 格）",
            note="移动本身不产生检定；离开敌人触及范围会引发借机攻击"))

    # ---- 4) 施法（角色认法术位；怪物走自带动作） ----
    for lv in sorted(unit.spell_slots):
        rec = unit.spell_slots[lv]
        if rec.get("current", 0) > 0:
            continue
        menu.blocked.append(f"{lv} 环法术位已用尽")

    castable = {lv for lv, rec in unit.spell_slots.items()
                if rec.get("current", 0) > 0}
    for sp in (getattr(unit, "spells", None) or []):
        _add_spell_options(menu, unit, sp, castable, foes, friends,
                           include_friendly_targets)

    _add_end_turn(menu, unit)
    return menu


# --------------------------------------------------------------------------- #
# 法术选项
# --------------------------------------------------------------------------- #

def spell_reach(sp) -> float:
    """法术的"够得着"距离。**自身范围法术也要有限度，不能全图。**

    · 有射程：射程 + 区域半径（火球 150 尺处炸，边缘再扩 20 尺）；
    · 自身锥/线/环：区域尺寸（15 尺锥就只到 15 尺）；
    · 触碰：5 尺。
    """
    if sp.range_ft:
        return float(sp.range_ft) + (sp.shape_size if sp.is_aoe else 0)
    if sp.is_aoe:
        return float(sp.shape_size)
    return 5.0 if sp.needs_targets else 0.0


def _spell_pool(unit: Unit, sp, foes: list[Unit], friends: list[Unit],
                include_friendly_targets: bool) -> list[Unit]:
    """这个法术该把谁列成候选目标。

    判据只看机械字段（不看名字）：
      · 有攻击检定 / 豁免 / 伤害骰 → 敌方；
      · 有治疗 / 临时生命 → 友方 + 自己；
      · 其余（增益、位移、侦测…）→ 友方 + 自己（效果交 DM 裁定）。
    """
    if sp.attack_roll or sp.save_ability or sp.damage_roll:
        return list(foes)
    ally_pool = (list(friends) + [unit]) if include_friendly_targets else [unit]
    if sp.heal_roll or sp.temp_hp_roll:
        return ally_pool
    return ally_pool if sp.needs_targets else []


def _can_upcast(sp) -> bool:
    """引擎能不能算出升环的**数值收益**。

    ⚠️ 只给"算得出差别"的法术列高环选项。像 `Magic Missile`（多一颗飞弹）
    或 `Hold Person`（多一个目标）这种 SRD 没给数值的升环，引擎算不出来 ——
    列出来只会让模型白白多花法术位，所以**不列**。
    """
    if sp.upcast_damage:
        return True
    return any((o.get("damage_roll") or o.get("heal_roll"))
               for o in sp.slot_options.values())


def _add_spell_options(menu: Menu, unit: Unit, sp, castable: set[int],
                       foes: list[Unit], friends: list[Unit],
                       include_friendly_targets: bool) -> None:
    """把一个法术展开成菜单选项（**每个可用环阶一条**）。"""
    if sp.slot == "reaction":
        # ⚠️ 反应法术**不进可选行动**：它们只能在触发条件满足时由引擎执行。
        can, why = unit.can_take("reaction")
        if (sp.name or "").strip().lower() == "shield":
            have = int((unit.spell_slots.get(1) or {}).get("current", 0))
            status = ("可用" if (can and have > 0)
                      else ("没有 1 环位" if can else f"不可用：{why}"))
            menu.reaction_notes.append(
                f"「{sp.name}」：被攻击命中时自动尝试（反应{status}）")
        else:
            menu.reaction_notes.append(
                f"「{sp.name}」：反应法术，引擎暂不自动触发（需 DM 裁定）")
        return

    can, _why = unit.can_take(sp.slot)
    if not can:
        # 槽位不可用（动作已用/失能…）时整条法术不列出 —— `menu.blocked` 已有原因。
        return

    if sp.is_cantrip:
        levels = [0]
    else:
        levels = [sp.level] if sp.level in castable else []
        if _can_upcast(sp):
            levels += [lv for lv in sorted(castable) if lv > sp.level]
        if not levels:
            have = "、".join(f"{lv} 环" for lv in sorted(castable)) or "无"
            menu.blocked.append(
                f"「{sp.name}」（{sp.level} 环）没有可用法术位（现有：{have}）")
            return

    pool = _spell_pool(unit, sp, foes, friends, include_friendly_targets)
    reach = spell_reach(sp)
    if reach:
        pool = [t for t in pool
                if grid_distance_ft(unit.position, t.position) <= reach]

    multi = bool(sp.is_aoe or sp.target_count > 1)
    requires = bool(sp.needs_targets) and not multi
    note = ""
    if pool:
        note = f"范围内 {len(pool)} 个目标" if multi else ""
    elif requires:
        note = f"当前没有目标在 {reach:g} 尺内 —— 需先移动"
    elif reach:
        note = f"射程 {reach:g} 尺内没有目标（引擎不对空地结算）"

    dist = min((grid_distance_ft(unit.position, t.position) for t in pool),
               default=0.0)

    for lv in levels:
        summary = sp.menu_line(lv, unit.level)
        if multi and pool:
            summary += f"（本次命中范围内全部 {len(pool)} 个目标）"
        menu.options.append(Option(
            id=menu.next_id(), kind="spell", slot=sp.slot,
            name=sp.name, summary=summary,
            target_ids=[t.id for t in pool],
            requires_target=requires,
            max_targets=(max(1, len(pool)) if multi else 1),
            spell_key=(sp.key or sp.name), slot_level=lv,
            distance=dist, note=note))


def _add_end_turn(menu: Menu, unit: Unit) -> None:
    menu.options.append(Option(
        id=menu.next_id(), kind="end_turn", slot="free", name="结束回合 End Turn",
        summary="结束回合：放弃剩余动作与移动"))


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #

def render_menu(menu: Menu, *, battlefield: list[Unit] | None = None) -> str:
    """渲染单个菜单（给的模型看的最小块）。

    目标**贴在选项那一行**：模型是在"挑哪条"的时候决定目标的，
    另起一块列目标会让它挑完之后忘了填 `target`（M1/M2 复验都是这么漏的）。
    """
    lines = [menu.render()]
    if battlefield:
        lines.append("")
        lines.append("  场上其他单位（距离为格距，5 尺/格）：")
        for u in battlefield:
            if u.id == menu.unit_id:
                continue
            me = next((x for x in battlefield if x.id == menu.unit_id), None)
            d = grid_distance_ft(me.position, u.position) if me else 0.0
            lines.append(f"    · {u.status_line()}　距离 {d:g} 尺")
    return "\n".join(lines)


def render_menus(menus: list[Menu], battlefield: list[Unit] | None = None) -> str:
    """把所有单位的菜单拼成一段（敌方规划模型一次拿到全部敌人的菜单）。"""
    blocks = []
    for i, m in enumerate(menus, 1):
        blocks.append(f"════ 单位 {i}/{len(menus)} ════\n"
                      + render_menu(m, battlefield=battlefield))
    return "\n\n".join(blocks)


