# coding=utf-8
"""引擎内置常量：属性、技能、伤害类型、**状态（Conditions）**。

为什么内置而不是从 SRD 拉：
    open5e 的 `srd-2024` 文档下 `conditions` / `damagetypes` / `sizes` / `languages` /
    `abilities` 这几个资源**返回 0 条**（已实测），`srd-2014` 的条件也是空的。
    而这几样是**判定引擎的地基**（"中毒了还能不能动"这类问题必须由代码回答），
    所以按 SRD 正文写成常量 —— 这是引擎该干的事，不是数据缺失的将就。

状态口径来源：D&D 2024 规则（与那张角色卡世界书「战斗面板」里列出的
14 个标准状态名一致：blinded/charmed/deafened/frightened/grappled/incapacitated/
invisible/paralyzed/petrified/poisoned/prone/restrained/stunned/unconscious）。

⚠️ 一条最容易搞错的：**Exhaustion（力竭）在 2024 版是 1~6 级的 *value* 型状态**，
不是"叠层"。它减 d20 检定与速度，6 级直接死亡。这里单独处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ABILITIES", "ABILITY_CN", "SKILLS", "SKILL_ABILITY", "DAMAGE_TYPES",
    "CONDITIONS", "Condition", "CONDITION_NAMES", "SIZES", "SIZE_CN",
    "EXHAUSTION_MAX", "EXHAUSTION_EFFECTS",
]


# --------------------------------------------------------------------------- #
# 六项属性
# --------------------------------------------------------------------------- #

ABILITIES = ("strength", "dexterity", "constitution",
             "intelligence", "wisdom", "charisma")

ABILITY_CN = {
    "strength": "力量", "dexterity": "敏捷", "constitution": "体质",
    "intelligence": "智力", "wisdom": "感知", "charisma": "魅力",
}

ABILITY_ABBR = {
    "strength": "STR", "dexterity": "DEX", "constitution": "CON",
    "intelligence": "INT", "wisdom": "WIS", "charisma": "CHA",
}


def ability_modifier(score: int) -> int:
    """属性调整值 = floor((分数 - 10) / 2)。**向下取整（负数也一样向小取）。**"""
    return (int(score) - 10) // 2


def proficiency_bonus(level: int) -> int:
    """熟练加值：1~4 级 +2，5~8 +3，9~12 +4，13~16 +5，17~20 +6。"""
    lv = max(1, min(20, int(level)))
    return 2 + (lv - 1) // 4


# --------------------------------------------------------------------------- #
# 十八项技能 → 属性
# --------------------------------------------------------------------------- #

SKILLS = {
    "acrobatics": "dexterity", "animal_handling": "wisdom", "arcana": "intelligence",
    "athletics": "strength", "deception": "charisma", "history": "intelligence",
    "insight": "wisdom", "intimidation": "charisma", "investigation": "intelligence",
    "medicine": "wisdom", "nature": "intelligence", "perception": "wisdom",
    "performance": "charisma", "persuasion": "charisma", "religion": "intelligence",
    "sleight_of_hand": "dexterity", "stealth": "dexterity", "survival": "wisdom",
}
#: 允许 open5e 用的连字符写法（"animal-handling"）
SKILL_ABILITY = {**SKILLS, **{k.replace("_", "-"): v for k, v in SKILLS.items()}}
SKILL_ABILITY.update({k.replace("_", " "): v for k, v in SKILLS.items()})

SKILL_CN = {
    "acrobatics": "体操", "animal_handling": "驯兽", "arcana": "奥秘",
    "athletics": "运动", "deception": "欺瞒", "history": "历史",
    "insight": "洞悉", "intimidation": "威吓", "investigation": "调查",
    "medicine": "医药", "nature": "自然", "perception": "察觉",
    "performance": "表演", "persuasion": "游说", "religion": "宗教",
    "sleight_of_hand": "巧手", "stealth": "隐匿", "survival": "求生",
}


# --------------------------------------------------------------------------- #
# 伤害类型（13 种）与体型
# --------------------------------------------------------------------------- #

DAMAGE_TYPES = (
    "acid", "bludgeoning", "cold", "fire", "force", "lightning", "necrotic",
    "piercing", "poison", "psychic", "radiant", "slashing", "thunder",
)
DAMAGE_TYPE_CN = {
    "acid": "强酸", "bludgeoning": "钝击", "cold": "寒冷", "fire": "火焰",
    "force": "力场", "lightning": "闪电", "necrotic": "死灵", "piercing": "穿刺",
    "poison": "毒素", "psychic": "心灵", "radiant": "光耀", "slashing": "挥砍",
    "thunder": "雷鸣",
}

#: 非魔法武器的三种物理伤害（用来判断"对非魔法武器的抗性"）
PHYSICAL_DAMAGE = ("bludgeoning", "piercing", "slashing")

SIZES = ("tiny", "small", "medium", "large", "huge", "gargantuan")
SIZE_CN = {"tiny": "微型", "small": "小型", "medium": "中型",
           "large": "大型", "huge": "巨型", "gargantuan": "超巨型"}


# --------------------------------------------------------------------------- #
# 状态（Conditions）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Condition:
    """一个状态的机械效果。

    每条效果都写成引擎能直接读的字段，**不要藏在描述文字里** ——
    "中毒还能不能动"这种问题必须能由代码回答，不能靠模型读描述。
    """
    key: str
    name_cn: str
    # --- 行动限制 ---
    incapacitated: bool = False        # 不能做动作/附赠动作/反应
    speed_zero: bool = False           # 速度变 0
    cant_speak: bool = False           # 不能说话（施法需要言语成分）
    # --- 攻防 ---
    attacks_against_advantage: bool = False   # 被打时对方有优势
    own_attacks_disadvantage: bool = False    # 自己打人有劣势
    auto_fail_str_dex_save: bool = False      # 力/敏豁免自动失败
    dex_save_disadvantage: bool = False       # 敏捷豁免有劣势（受缚）
    damage_resist_all: bool = False           # 对所有伤害有抗性（石化）
    # --- 数值 ---
    grants_attacker_advantage_within: int = 0  # 攻击者在 X 尺内有优势（0 = 不适用）
    # --- 说明 ---
    note: str = ""


CONDITIONS: dict[str, Condition] = {
    # 下面按 2024 规则逐条写。名称与角色卡世界书里的 14 个标准状态一致。
    "blinded": Condition(
        "blinded", "目盲",
        own_attacks_disadvantage=True, attacks_against_advantage=True,
        note="看不见：自己攻击有劣势；攻击你的人有优势；依赖视觉的检定自动失败。"),

    "charmed": Condition(
        "charmed", "魅惑",
        note="不能攻击魅惑者，魅惑者对你的社交检定有优势。"),

    "deafened": Condition(
        "deafened", "耳聋",
        note="听不见：依赖听觉的检定自动失败。"),

    "frightened": Condition(
        "frightened", "恐慌",
        own_attacks_disadvantage=True,
        note="只要看得见恐惧源：攻击检定与属性检定有劣势，且不能主动靠近。"),

    "grappled": Condition(
        "grappled", "受擒",
        speed_zero=True,
        note="速度变 0；擒抱者移动时会带着你走。"),

    "incapacitated": Condition(
        "incapacitated", "失能",
        incapacitated=True,
        note="不能做动作、附赠动作或反应；不能说话。"),

    "invisible": Condition(
        "invisible", "隐形",
        note="看不见你：你攻击有优势；攻击你的人有劣势。"),

    "paralyzed": Condition(
        "paralyzed", "麻痹",
        incapacitated=True, speed_zero=True, cant_speak=True,
        attacks_against_advantage=True, auto_fail_str_dex_save=True,
        note="失能 + 速度 0 + 不能说话；力量/敏捷豁免自动失败；"
             "5 尺内的攻击**必定重击**。"),

    "petrified": Condition(
        "petrified", "石化",
        incapacitated=True, speed_zero=True, cant_speak=True,
        attacks_against_advantage=True, auto_fail_str_dex_save=True,
        damage_resist_all=True,
        note="失能 + 速度 0 + 不能说话；力量/敏捷豁免自动失败；"
             "对伤害有抗性；免疫毒素与疾病；看起来像石头。"),

    "poisoned": Condition(
        "poisoned", "中毒",
        own_attacks_disadvantage=True,
        note="攻击检定与属性检定有劣势。"),

    "prone": Condition(
        "prone", "倒地",
        own_attacks_disadvantage=True,
        note="只能爬行或起身；自己攻击有劣势；"
             "5 尺内的攻击者对你**有优势**，更远的对你有**劣势**。"),

    "restrained": Condition(
        "restrained", "受缚",
        speed_zero=True, attacks_against_advantage=True,
        own_attacks_disadvantage=True, dex_save_disadvantage=True,
        note="速度 0；攻击你的人有优势，你的攻击有劣势；敏捷豁免有劣势。"),

    "stunned": Condition(
        "stunned", "震慑",
        incapacitated=True, speed_zero=True, cant_speak=True,
        attacks_against_advantage=True, auto_fail_str_dex_save=True,
        note="失能 + 速度 0 + 不能说话；力量/敏捷豁免自动失败；攻击你的人有优势。"),

    "unconscious": Condition(
        "unconscious", "昏迷",
        incapacitated=True, speed_zero=True, cant_speak=True,
        attacks_against_advantage=True, auto_fail_str_dex_save=True,
        note="失能 + 速度 0 + 不能说话；力量/敏捷豁免自动失败；"
             "5 尺内的攻击**必定重击**；不知道周围发生什么。"),
}

CONDITION_NAMES = tuple(sorted(CONDITIONS))

#: 附带"5 尺内攻击必定重击"的两个状态（2024 规则：麻痹、昏迷）
#: ⚠️ 石化**不**在此列 —— 它有力/敏自动失败和全伤害抗性，但没有必重击。
AUTO_CRIT_CONDITIONS = frozenset({"paralyzed", "unconscious"})


# --------------------------------------------------------------------------- #
# 力竭（Exhaustion）—— 2024 版是 1~6 级数值
# --------------------------------------------------------------------------- #

EXHAUSTION_MAX = 6

#: 每级的机械效果（2024 规则）
EXHAUSTION_EFFECTS = {
    1: "d20 检定与属性检定 -2",
    2: "d20 检定与属性检定 -4；速度 -5 尺",
    3: "d20 检定与属性检定 -6；速度 -10 尺",
    4: "d20 检定与属性检定 -8；速度 -15 尺",
    5: "d20 检定与属性检定 -10；速度 -20 尺",
    6: "死亡",
}


def exhaustion_modifier(level: int) -> int:
    """力竭对 d20 检定的修正（2024：每级 -2）。"""
    lv = max(0, min(EXHAUSTION_MAX, int(level)))
    return -2 * lv if lv else 0


def exhaustion_speed_penalty(level: int) -> int:
    """力竭对速度的扣减（2024：2 级起每级 -5 尺）。"""
    lv = max(0, min(EXHAUSTION_MAX, int(level)))
    return 5 * max(0, lv - 1)
