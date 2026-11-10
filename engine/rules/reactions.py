# coding=utf-8
"""反应与准备动作（Ready）的触发层。

对应决策记录 §5.18（U5）。**纯本地判断，不调模型。**

两条纪律：

1. **反应不是普通选项**。`护盾术`这类反应法术不能"轮到自己时主动放"，
   只能在触发条件满足时由引擎执行；Ready 也一样 —— 声明时只登记条件，
   真正打出去是在别人行动时。
2. **触发条件必须是引擎看得见的**。第一版只做两种：
   - `enters_reach`：敌人移动后进入你的攻击射程/触及；
   - `attacked`：敌人攻击你（结算后，你还活着且还有反应）。
   看不懂的条件不猜，宁可让 DM 裁定。

触发记录挂在 `Unit.reactions` 上；`Unit.reset_turn()` 会清空 ——
Ready 的寿命就是"到你的下个回合开始"。
"""

from __future__ import annotations

from dataclasses import dataclass

from .unit import Action, Attack, Unit

__all__ = [
    "ReadyTrigger", "CONDITIONS", "CONDITION_CN",
    "get_ready", "consume_ready", "ready_weapon",
    "has_shield", "shield_spell",
]

#: 第一版支持的触发条件
CONDITIONS = ("enters_reach", "attacked")
CONDITION_CN = {
    "enters_reach": "敌人进入射程/触及",
    "attacked": "敌人攻击我",
}


@dataclass
class ReadyTrigger:
    """一条已经声明、等着触发的准备动作。"""
    condition: str                 # enters_reach / attacked
    action_index: int
    attack_index: int
    label: str = ""                # 给人看的说明

    def to_dict(self) -> dict:
        return {"condition": self.condition,
                "action_index": self.action_index,
                "attack_index": self.attack_index,
                "label": self.label}

    @classmethod
    def from_dict(cls, d: dict) -> "ReadyTrigger":
        if not isinstance(d, dict):
            raise ValueError(f"准备动作必须是对象：{d!r}")
        cond = str(d.get("condition", ""))
        if cond not in CONDITIONS:
            raise ValueError(f"不认识的触发条件：{cond!r}（可选 {CONDITIONS}）")
        ai = int(d.get("action_index", -1))
        ti = int(d.get("attack_index", -1))
        if ai < 0 or ti < 0:
            raise ValueError(f"准备动作的索引非法：action={ai}, attack={ti}")
        return cls(condition=cond, action_index=ai, attack_index=ti,
                   label=str(d.get("label", "")))

    def line(self) -> str:
        return (f"准备（{CONDITION_CN.get(self.condition, self.condition)}）"
                + (f"：{self.label}" if self.label else ""))


def _reactions(unit: Unit) -> list:
    return getattr(unit, "reactions", None) or []


def get_ready(unit: Unit, condition: str) -> ReadyTrigger | None:
    """取第一条匹配条件的准备动作（**不消耗**）。"""
    for r in _reactions(unit):
        if isinstance(r, ReadyTrigger) and r.condition == condition:
            return r
    return None


def consume_ready(unit: Unit, trigger: ReadyTrigger) -> bool:
    """消耗掉一条准备动作。返回是否真的移除了（防止重复触发）。"""
    rs = _reactions(unit)
    for i, r in enumerate(rs):
        if r is trigger or (isinstance(r, ReadyTrigger)
                            and r.to_dict() == trigger.to_dict()):
            del rs[i]
            return True
    return False


def ready_weapon(unit: Unit, trigger: ReadyTrigger) -> tuple[Action, Attack] | None:
    """把触发记录还原成 (动作, 攻击)。索引坏了就返回 None，不猜。"""
    if trigger.action_index < 0 or trigger.attack_index < 0:
        return None
    try:
        act = unit.actions[trigger.action_index]
        atk = act.attacks[trigger.attack_index]
    except (IndexError, TypeError):
        return None
    return act, atk


def shield_spell(unit: Unit):
    """单位法术书里的护盾术（没有就 None）。"""
    for sp in (getattr(unit, "spells", None) or []):
        if (sp.name or "").strip().lower() == "shield":
            return sp
    return None


def has_shield(unit: Unit) -> bool:
    return shield_spell(unit) is not None
