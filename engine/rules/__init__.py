# coding=utf-8
"""规则与判定。

**这一层是"代码说了算"的落点**：骰子怎么掷、命中怎么算、伤害怎么扣、
状态能不能动、法术位扣没扣 —— 全部由这里决定。模型只出意图、只写叙事。

模块分工：
    constants.py  属性/技能/伤害类型/**状态**
    unit.py       战斗单位（数值改动的唯一入口）
    bestiary.py   SRD 怪物数值块 → 引擎动作
    adjudicate.py 检定/攻击/豁免/伤害的结算
    menu.py       「合法动作菜单」生成器（模型只能在这份菜单里挑）
"""

from .constants import (
    ABILITIES, ABILITY_CN, CONDITIONS, CONDITION_NAMES, DAMAGE_TYPES, SKILLS,
    ability_modifier, exhaustion_modifier, proficiency_bonus,
)
from .unit import Action, Attack, DamagePacket, DamageResult, Unit

__all__ = [
    "Unit", "Action", "Attack", "DamagePacket", "DamageResult",
    "ABILITIES", "ABILITY_CN", "SKILLS", "DAMAGE_TYPES",
    "CONDITIONS", "CONDITION_NAMES",
    "ability_modifier", "proficiency_bonus", "exhaustion_modifier",
]
