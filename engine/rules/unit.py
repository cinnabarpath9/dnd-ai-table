# coding=utf-8
"""战斗单位（Unit）—— 引擎里"一个会行动的东西"。

武器/护甲/状态/法术位**全都挂在 Unit 上**，模型只看到渲染出来的摘要；
任何数值改动都必须经过这里的方法（`take_damage` / `heal` / `consume_slot` …），
**不许外部直接改字段** —— 那样状态就会绕过结算，变成"模型说了算"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..dice import Roll
from .constants import (
    ABILITY_CN, CONDITION_NAMES, CONDITIONS, PHYSICAL_DAMAGE,
    ability_modifier, exhaustion_modifier, exhaustion_speed_penalty,
)

__all__ = ["Attack", "Action", "Unit", "DamageResult", "DamagePacket"]


# --------------------------------------------------------------------------- #
# 伤害包
# --------------------------------------------------------------------------- #

@dataclass
class DamagePacket:
    """一段伤害：点数额 + 类型 + 是否魔法来源。"""
    amount: int
    damage_type: str
    magical: bool = False

    def __str__(self) -> str:
        return f"{self.amount} {self.damage_type}{'（魔法）' if self.magical else ''}"


@dataclass
class DamageResult:
    """一次伤害结算的留痕。"""
    packets: list[DamagePacket] = field(default_factory=list)
    hp_before: int = 0
    hp_after: int = 0
    temp_before: int = 0
    temp_after: int = 0
    immune: list[str] = field(default_factory=list)
    resisted: list[str] = field(default_factory=list)
    vulnerable: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dropped_to_zero: bool = False

    @property
    def total_dealt(self) -> int:
        return self.hp_before - self.hp_after

    def summary(self) -> str:
        bits = []
        for p in self.packets:
            s = str(p)
            if p.damage_type in self.immune:
                s += "（免疫→0）"
            elif p.damage_type in self.resisted:
                s += "（抗性→半）"
            elif p.damage_type in self.vulnerable:
                s += "（易伤→双倍）"
            bits.append(s)
        head = " + ".join(bits) if bits else "无伤害"
        tail = f"HP {self.hp_before}"
        if self.temp_before:
            tail += f"（临时 {self.temp_before}）"
        tail += f" → {self.hp_after}"
        if self.temp_after:
            tail += f"（临时 {self.temp_after}）"
        s = f"{head} ⇒ {tail}"
        if self.dropped_to_zero:
            s += " **归零**"
        return s


# --------------------------------------------------------------------------- #
# 动作与攻击
# --------------------------------------------------------------------------- #

#: 动作经济槽位（2024 规则）
SLOTS = ("action", "bonus_action", "reaction", "legendary", "lair", "free")


@dataclass
class Attack:
    """一次攻击的机械定义。"""
    name: str
    attack_bonus: int                       # 攻击检定加值
    damage_notation: str = ""               # 主伤害骰式，如 "1d6+1"
    damage_type: str = ""
    extra_damage_notation: str = ""         # 附加伤害（如龙爪的 +2d4 火焰）
    extra_damage_type: str = ""
    reach: int | None = None                # 触及（尺）
    range_normal: int | None = None         # 射程（尺）
    range_long: int | None = None           # 远射程（尺）
    is_melee: bool = True
    properties: tuple[str, ...] = ()        # 武器属性（finesse / heavy / reach …）

    def range_text(self) -> str:
        if self.is_melee:
            return f"触及 {self.reach or 5} 尺"
        if self.range_long:
            return f"射程 {self.range_normal}/{self.range_long} 尺"
        return f"射程 {self.range_normal or 0} 尺"

    def damage_text(self) -> str:
        bits = []
        if self.damage_notation:
            bits.append(f"{self.damage_notation} {self.damage_type}".strip())
        if self.extra_damage_notation:
            bits.append(f"{self.extra_damage_notation} {self.extra_damage_type}".strip())
        return " + ".join(bits) if bits else "—"


@dataclass
class Action:
    """一个可用的动作。"""
    name: str
    slot: str = "action"                    # 见 SLOTS
    attacks: list[Attack] = field(default_factory=list)
    #: 该动作要求目标做的豁免（没有就是空）
    save_ability: str = ""
    save_dc: int = 0
    save_success_half: bool = True           # 豁免成功是否只受半伤
    #: 直接把伤害写在动作上（AoE 吐息这类，不属于某个 attack）
    damage_notation: str = ""
    damage_type: str = ""
    #: 治疗型动作的回复骰式（DM 造的技能/H1）；空 = 不是治疗动作
    heal_notation: str = ""
    #: 影响范围（尺）：0 = 单体
    area_size: int = 0
    area_shape: str = ""                     # cone / sphere / cube / line / emanation
    #: 动作能打到的距离（尺）：0 = 未知（不按距离过滤）
    range_ft: int = 0
    #: 使用次数限制
    recharge_on_roll: int = 0                # 例 5 表示 d6 >= 5 时充能
    uses_per_day: int = 0
    legendary_cost: int = 0
    #: 原文描述（**模型读这个**；引擎只读上面的结构字段）
    desc: str = ""
    #: 结构化解析是否可信；False 表示这条规则引擎只当"文字建议"，不自动结算
    structured: bool = True

    def __str__(self) -> str:
        tag = {"action": "动作", "bonus_action": "附赠动作", "reaction": "反应",
               "legendary": "传奇动作", "lair": "巢穴动作", "free": "自由"}.get(self.slot, self.slot)
        bits = [f"[{tag}] {self.name}"]
        if self.attacks:
            a = self.attacks[0]
            bits.append(f"{a.attack_bonus:+d} / {a.damage_text()} / {a.range_text()}")
        if self.save_ability:
            bits.append(f"{ABILITY_CN.get(self.save_ability, self.save_ability)}豁免 DC {self.save_dc}")
            if self.damage_notation:
                bits.append(f"{self.damage_notation} {self.damage_type}")
        if self.heal_notation:
            bits.append(f"回复 {self.heal_notation}")
        if self.area_size:
            bits.append(f"{self.area_size} 尺{self.area_shape}")
        if self.recharge_on_roll:
            bits.append(f"充能 d6≥{self.recharge_on_roll}")
        if self.uses_per_day:
            bits.append(f"{self.uses_per_day}/日")
        if not self.structured:
            bits.append("（引擎不自动结算，需 DM 裁定）")
        return "｜".join(bits)


# --------------------------------------------------------------------------- #
# 单位
# --------------------------------------------------------------------------- #

@dataclass
class Unit:
    """一个战斗单位。"""
    id: str
    name: str
    team: str = "neutral"               # pc / ally / enemy / neutral
    is_pc: bool = False
    level: int = 1

    # --- 防御 ---
    ac: int = 10
    ac_note: str = ""

    # --- 生命 ---
    hp_max: int = 1
    hp: int = 1
    temp_hp: int = 0

    # --- 属性与熟练 ---
    abilities: dict[str, int] = field(default_factory=dict)
    save_proficiencies: set[str] = field(default_factory=set)
    skill_bonuses: dict[str, int] = field(default_factory=dict)   # 已经算好的技能总加值
    save_bonuses: dict[str, int] = field(default_factory=dict)    # 已经算好的豁免总加值

    # --- 移动与感官 ---
    speeds: dict[str, int] = field(default_factory=dict)          # walk/fly/swim/climb/burrow
    size: str = "medium"
    senses: dict[str, int] = field(default_factory=dict)          # darkvision 等
    passive_perception: int = 10

    # --- 战场 ---
    position: tuple[float, float] = (0.0, 0.0)                    # 单位：尺
    conditions: dict[str, int] = field(default_factory=dict)      # 状态 → 剩余回合（-1 = 直到解除）
    exhaustion: int = 0

    # --- 动作与资源 ---
    actions: list[Action] = field(default_factory=list)
    #: 已准备的法术（`engine.rules.spells.Spell` 对象）。
    #: ⚠️ 这里只声明 `list` 不 import Spell —— `spells.py` 依赖 SRD store，
    #:    而 unit 是更底层的模块，import 会绕成环。菜单/回合按鸭子类型读字段。
    spells: list = field(default_factory=list)
    #: 施法属性（英文键：intelligence / wisdom / charisma…）。
    #: 空 = 用 `cast_spell()` 的默认值（魅力）。
    cast_ability: str = ""
    #: 已声明、等待触发的准备动作（`engine.rules.reactions.ReadyTrigger`）。
    #: 和 `spells` 一样只声明 `list`，避免底层模块反向依赖规则层。
    reactions: list = field(default_factory=list)
    spell_slots: dict[int, dict[str, int]] = field(default_factory=dict)  # 环阶 → {current,max}
    #: 消耗品（U12）：物品名 → {"数量": n, "治疗": "2d4+2"}。
    #: 战斗里喝一瓶是**附赠动作**（2024）；数量归零就从这里删掉。
    consumables: dict[str, dict] = field(default_factory=dict)
    #: 资源名 → {current,max}；**充能键**例外，直接存 bool（本回合能不能用）。
    resources: dict[str, dict[str, int] | bool] = field(default_factory=dict)
    resistances: set[str] = field(default_factory=set)
    immunities: set[str] = field(default_factory=set)
    vulnerabilities: set[str] = field(default_factory=set)
    condition_immunities: set[str] = field(default_factory=set)

    # --- 本回合动作经济 ---
    used: dict[str, int] = field(default_factory=dict)            # slot → 已用次数
    movement_used: float = 0.0
    legendary_actions_max: int = 0
    legendary_actions_used: int = 0
    #: 巢穴动作：每轮一次（先攻 20 触发）。SRD 怪物数据里通常没有，留给 DM/剧本定义。
    lair_actions_max: int = 0
    reaction_used: bool = False
    concentrating_on: str = ""

    # --- 出处 ---
    source_key: str = ""                # SRD 的 key（有的话）
    #: 先攻**总值**覆盖（DM 人物数值块用）；None = 用敏捷调整值
    init_override: int | None = None
    #: 临时修正（H2b）：AC/攻击/豁免/技能/检定/伤害；见 `rules/effects.py`
    mods: list[dict] = field(default_factory=list)
    #: 装备特效（H3）：触发式效果（免死/临时生命/回合回复）；见 `rules/item_effects.py`
    item_effects: list[dict] = field(default_factory=list)
    #: 装备特效的充能：`物品:特效名 → {max, current, cooldown}`
    item_charges: dict[str, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # ======================= 派生值 =======================

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def proficiency(self) -> int:
        from .constants import proficiency_bonus
        return proficiency_bonus(self.level)

    def mod(self, ability: str) -> int:
        """属性调整值。

        ⚠️ 属性名**中英双语都认**，而且**两种键都要能在 `abilities` 里找到**：
        数据来自 `stat_data` 时是中文键（力量），来自 SRD 时是英文键（strength），
        调用方不该关心这件事 —— 所以这里两个方向都试。
        """
        a = (ability or "").strip()
        low = a.lower()
        # 中文 → 英文 / 英文 → 英文
        en = ABILITY_CN_REV.get(a, ABILITY_CN_REV.get(low, low))
        # 英文 → 中文 / 中文 → 中文
        cn = ABILITY_CN.get(en, a)
        for key in (en, cn, a, low):
            if key and key in self.abilities:
                return ability_modifier(self.abilities[key])
        return 0

    @property
    def ac_total(self) -> int:
        """有效 AC = 基础 AC + 临时修正（H2b）。引擎判定用这个，显示基础值。"""
        from .effects import total as _mod_total
        return max(0, int(self.ac) + _mod_total(self, "ac"))

    @property
    def initiative_bonus(self) -> int:
        """先攻加值：DM 人物可以写总加值覆盖（数值块 `先攻加值`）。"""
        if self.init_override is not None:
            return int(self.init_override)
        return self.mod("dexterity")

    @property
    def speed(self) -> int:
        """当前步行速度（含状态与力竭修正）。"""
        base = self.speeds.get("walk", 30)
        if self.has_condition_effect("speed_zero"):
            return 0
        from .effects import total as _mod_total
        return max(0, base - exhaustion_speed_penalty(self.exhaustion)
                   + _mod_total(self, "speed"))

    @property
    def is_incapacitated(self) -> bool:
        return self.has_condition_effect("incapacitated")

    def condition(self, key: str):
        return CONDITIONS.get((key or "").lower())

    def has_condition_effect(self, attr: str) -> bool:
        """自己身上**任意**状态是否带有某个效果位（如 speed_zero）。"""
        for c in self.conditions:
            cond = CONDITIONS.get(c)
            if cond and getattr(cond, attr, False):
                return True
        return False

    # ======================= 行动限制 =======================

    def can_take(self, slot: str) -> tuple[bool, str]:
        """这一回合还能不能再用某个槽位。返回 (能否, 原因)。"""
        if not self.alive:
            return False, "已倒地/死亡"
        if slot == "reaction":
            if self.is_incapacitated:
                return False, "失能状态：不能做反应"
            if self.reaction_used:
                return False, "本回合反应已用过"
            return True, ""
        if self.is_incapacitated and slot in ("action", "bonus_action", "legendary", "lair"):
            return False, "失能状态：不能做动作/附赠动作"
        if slot == "legendary":
            cap = self.legendary_actions_max - self.legendary_actions_used
            if cap <= 0:
                return False, "传奇动作次数已用完"
            return True, ""
        if slot == "lair":
            if self.lair_actions_max <= 0:
                return False, "该单位没有巢穴动作"
            used = self.used.get("lair", 0)
            if used >= self.lair_actions_max:
                return False, "本轮的巢穴动作已用过"
            return True, ""
        cap = 1
        if slot == "free":
            return True, ""
        used = self.used.get(slot, 0)
        if used >= cap:
            return False, f"本回合的{'动作' if slot == 'action' else '附赠动作'}已用过"
        return True, ""

    @property
    def legendary_left(self) -> int:
        """还剩几点传奇动作资源（0 = 用完）。"""
        return max(0, self.legendary_actions_max - self.legendary_actions_used)

    def spend_legendary(self, cost: int = 1) -> None:
        """按**动作标价**扣传奇动作资源（不是固定 1 点）。"""
        self.legendary_actions_used += max(1, int(cost))

    def spend(self, slot: str) -> None:
        if slot == "reaction":
            self.reaction_used = True
        elif slot == "legendary":
            self.legendary_actions_used += 1
        elif slot == "lair":
            self.used["lair"] = self.used.get("lair", 0) + 1
        elif slot in ("action", "bonus_action"):
            self.used[slot] = self.used.get(slot, 0) + 1

    def reset_round(self) -> None:
        """新的一轮开始：巢穴动作**每轮一次**（先攻 20 触发）。

        ⚠️ 传奇动作**不在这里重置** —— 它在"自己回合开始"刷新（`reset_turn`）。
        """
        self.used.pop("lair", None)

    def reset_turn(self) -> None:
        """轮到自己时重置：动作/附赠动作/移动/传奇动作（反应在**自己回合开始**刷新）。

        同时清空**准备动作** —— Ready 只活到你的下个回合开始（2024 规则）。
        """
        self.used.pop("action", None)
        self.used.pop("bonus_action", None)
        # 回合性标记不能跨回合：脱离/回避只保到本回合结束
        self.used.pop("disengaged", None)
        self.used.pop("dodging", None)
        self.movement_used = 0.0
        self.reaction_used = False
        self.legendary_actions_used = 0
        if self.reactions:
            self.reactions.clear()
        # 轮次修正（H2b）：每到自己回合扣一轮，到 0 掉
        from .effects import tick_round as _tick_mods
        gone = _tick_mods(self)
        if gone:
            self.notes.append("修正到期：" + "、".join(gone))
        # H3：装备特效的回合开始回复（冷却也在这里扣）
        from .item_effects import trigger_turn_start as _item_turn
        for line in _item_turn(self):
            self.notes.append(line)

    # ======================= 数值改动（唯一入口） =======================

    def take_damage(self, packets: list[DamagePacket]) -> DamageResult:
        """扣血。抗性/易伤/免疫在这里生效；**临时生命先扣**（2024 规则）。"""
        res = DamageResult(hp_before=self.hp, temp_before=self.temp_hp)
        total = 0
        for p in packets:
            dt = (p.damage_type or "").lower()
            amount = int(p.amount)
            # 免疫
            if dt in self.immunities or (p.magical and f"magical_{dt}" in self.immunities):
                res.immune.append(dt)
                amount = 0
            # 对非魔法武器的抗性：SRD 里写作 "bludgeoning,piercing,slashing"
            elif (not p.magical and dt in PHYSICAL_DAMAGE
                  and any(f"nonmagical" in x for x in self.resistances)):
                res.resisted.append(dt)
                amount //= 2
            # 石化：对所有伤害有抗性（不叠加：已经在上面判过免疫/非魔法抗性）
            elif self.has_condition_effect("damage_resist_all"):
                res.resisted.append(dt)
                amount //= 2
            elif dt in self.resistances:
                res.resisted.append(dt)
                amount //= 2
            elif dt in self.vulnerabilities:
                res.vulnerable.append(dt)
                amount *= 2
            res.packets.append(DamagePacket(amount, dt, p.magical))
            total += amount

        # 临时生命先吸收
        absorbed = min(self.temp_hp, total)
        self.temp_hp -= absorbed
        rest = total - absorbed
        if absorbed:
            res.notes.append(f"临时生命吸收 {absorbed}")
        self.hp = max(0, self.hp - rest)
        res.hp_after = self.hp
        res.temp_after = self.temp_hp
        res.dropped_to_zero = self.hp == 0
        # H3：只在"将要降到 0"时拦一次 —— 已经 0 HP 再挨打不算致命一击，
        # 否则倒地角色会被未消耗的免死原地拉起来（外审 P1-1）。
        if res.dropped_to_zero and res.hp_before > 0:
            from .item_effects import try_death_ward
            ward = try_death_ward(self, rest, res)
            if ward:
                res.notes.append(ward)
                res.dropped_to_zero = False
                res.hp_after = self.hp
            else:
                res.notes.append("HP 归零")
        elif res.dropped_to_zero:
            res.notes.append("HP 归零（已在 0，不免死）")
        return res

    def heal(self, amount: int) -> int:
        """治疗。**不超过最大生命；不恢复临时生命。**返回实际治疗量。"""
        before = self.hp
        self.hp = min(self.hp_max, self.hp + max(0, int(amount)))
        return self.hp - before

    def grant_temp_hp(self, amount: int) -> int:
        """给临时生命。**2024 规则：不叠加，取较高者。**"""
        amount = max(0, int(amount))
        if amount > self.temp_hp:
            gained = amount - self.temp_hp
            self.temp_hp = amount
            return gained
        return 0

    def use_resource(self, name: str, amount: int = 1) -> bool:
        """消耗一个通用资源（充能、每日次数…）。不够就返回 False，**不静默扣成负数**。"""
        rec = self.resources.get(name)
        if rec is None:
            return False
        if rec.get("current", 0) < amount:
            return False
        rec["current"] -= amount
        return True

    def use_spell_slot(self, level: int) -> bool:
        rec = self.spell_slots.get(int(level))
        if not rec or rec.get("current", 0) <= 0:
            return False
        rec["current"] -= 1
        return True

    # ======================= 状态 =======================

    def add_condition(self, key: str, rounds: int = -1) -> bool:
        """加状态。免疫则加不上（返回 False）。"""
        k = (key or "").lower()
        if k not in CONDITIONS:
            raise KeyError(f"不认识的战状态：{key!r}（可选：{', '.join(CONDITION_NAMES)}）")
        if k in self.condition_immunities:
            return False
        self.conditions[k] = rounds
        return True

    def remove_condition(self, key: str) -> bool:
        return self.conditions.pop((key or "").lower(), None) is not None

    def tick_conditions(self) -> list[str]:
        """回合结束时倒计时，返回本回合结束的状态。"""
        expired = []
        for k in list(self.conditions):
            r = self.conditions[k]
            if r < 0:
                continue
            r -= 1
            if r <= 0:
                expired.append(k)
                del self.conditions[k]
            else:
                self.conditions[k] = r
        return expired

    # ======================= 渲染（给模型看的摘要） =======================

    def status_line(self) -> str:
        """一行状态。**给模型看的就是这个**，不是整张角色卡。"""
        bits = [f"{self.name}", f"HP {self.hp}/{self.hp_max}"]
        if self.temp_hp:
            bits.append(f"临时 {self.temp_hp}")
        bits.append(f"AC {self.ac}")
        if self.conditions:
            cs = []
            for k, r in self.conditions.items():
                cn = CONDITIONS[k].name_cn if k in CONDITIONS else k
                cs.append(f"{cn}({r}轮)" if r >= 0 else cn)
            bits.append("状态: " + "、".join(cs))
        if self.exhaustion:
            bits.append(f"力竭 {self.exhaustion} 级")
        if self.concentrating_on:
            bits.append(f"专注: {self.concentrating_on}")
        if not self.alive:
            bits.append("**已倒下**")
        return "｜".join(bits)

    def combat_block(self) -> str:
        """战斗中真正需要的那一小块（≈260 tok）。**不要塞完整角色卡。**"""
        # 属性名可能是中文（来自 stat_data）也可能是英文（来自 SRD）——
        # 统一按"英文缩写 + 中文标签"渲染，两种来源都能用。
        from .constants import ABILITY_ABBR, ABILITY_CN
        order = list(ABILITY_ABBR.values())          # STR DEX CON INT WIS CHA
        name_to_key = {v: k for k, v in ABILITY_ABBR.items()}

        def norm(key: str) -> str:
            k = str(key)
            return ABILITY_CN_REV.get(k, k.lower())

        def rank(key: str) -> int:
            k = norm(key)
            ab = ABILITY_ABBR.get(k, "")
            return order.index(ab) if ab in order else 99

        saves = " ".join(
            f"{ABILITY_ABBR.get(norm(a), a[:1])}{self.save_total(a):+d}"
            for a in sorted(self.abilities, key=rank))
        lines = [
            f"【{self.id}】{self.name}",
            f"  {self.status_line()}",
            f"  速度 {self.speed} 尺｜先攻加值 {self.initiative_bonus:+d}"
            f"｜被动察觉 {self.passive_perception}",
            f"  豁免 {saves}",
        ]
        if self.spell_slots:
            lines.append("  法术位 " + " ".join(
                f"{lv}环 {v['current']}/{v['max']}"
                for lv, v in sorted(self.spell_slots.items())))
        return "\n".join(lines)

    def save_total(self, ability: str) -> int:
        """某项豁免的总加值：优先用算好的，否则属性调整值。

        ⚠️ 属性名**中英双语都认** —— `stat_data` 那棵树里用的是中文键
        （`力量` / `敏捷`…），而 SRD 数据用的是英文键，两个来源会混着进来。
        熟练集合也可能写中文，所以查找时把两个方向的键都试一遍。
        """
        a = (ability or "").strip()
        low = a.lower()
        en = ABILITY_CN_REV.get(a, ABILITY_CN_REV.get(low, low))
        cn = ABILITY_CN.get(en, a)
        keys = (en, cn, a, low)
        from .effects import total as _mod_total
        extra = _mod_total(self, "save", en)

        for k in keys:
            if k in self.save_bonuses:
                # ⚠️ 力竭修正必须也加在这里 —— 提前 return 会让有预置豁免的
                #    单位（SRD 怪物全是）完全不吃力竭。
                return (self.save_bonuses[k] + exhaustion_modifier(self.exhaustion)
                        + extra)
        v = self.mod(a)
        if any(k in self.save_proficiencies for k in keys):
            v += self.proficiency
        return v + exhaustion_modifier(self.exhaustion) + extra


#: 中文属性名 → 英文（`mod()` 用）
ABILITY_CN_REV = {v: k for k, v in ABILITY_CN.items()}
