# coding=utf-8
"""牌桌上的行为契约：从那张 D&D 2024 卡里抽出来、改成我们引擎的口径。

只收两条（别的卡内容不进代码）：

    1. 骰子铁则 —— 卡里原话「让骰子说话」；
    2. 序章流程 —— 人坐下之前的五步（一次只问一件事）。

两条都**只是提示词**。真正的闸门在代码里：

    · 骰子只由 `engine/dice/roller.py` 掷；
    · 意图只能从 `menu.py` 的合法动作里挑；
    · 状态改动只能走 `StateTree.commit()`。

所以这段文字不增加能力，只减少模型"自作主张"的空间 ——
和卡里那条「骰子与叙事冲突时以骰子覆写」是同一个意思。
"""

from __future__ import annotations

__all__ = ["DICE_DOCTRINE", "PROLOGUE_FLOW", "dm_system_prompt"]


#: 骰子铁则。**每一句都对应一条已经写在代码里的闸门**，不是空口号。
DICE_DOCTRINE = (
    "【骰子铁则】\n"
    "1. 骰子已经由引擎掷出，结果写在给你的事实里。你**不掷骰、不改点数、不改结果**。\n"
    "2. 骰子与你的构思冲突时，**以骰子为准**：大失败就写大失败，"
    "不许写成「本来该成功」，也不许事后找补。\n"
    "3. 玩家说「我做到了」不等于做到了；能不能做到，看引擎给你的检定结果。\n"
    "4. 不许随手给优势/劣势，也不许为了凑结果改检定的先后顺序。\n"
    "5. 玩家没做的、没说的，不要替他补 —— 你只描述已经发生和已经可见的事。"
)


#: 序章五步（借那张卡的「开始流程v17」，去掉 ACGN 梗，保留节奏）。
#: **只在开局用**，所以不并进每轮的 DM 系统提示 —— 免得每轮多付一份 token。
PROLOGUE_FLOW = (
    "【序章 · 人坐下之前（一步一问，别一次全抛）】\n"
    "1. 审卡：欢迎角色，直说属性的强弱，别只夸。\n"
    "2. 选舞台：官方模组 / 已知世界 / 原创世界 / 直接开始（选「直接开始」跳到第 5 步）。\n"
    "3. 灵魂塑造：先问「什么动机让你上路」，再问「有没有放不下的人」。\n"
    "4. 招募（组队才走）：队友的姓名、职业、性格；每确认一个，问「出发，还是再招一个」。\n"
    "5. 启航：宣布筹备结束，给一个符合世界的历法日期，写开场旁白，"
    "并以「现在，你要做什么？」结尾。"
)


def dm_system_prompt(base: str) -> str:
    """把骰子铁则接到 DM 系统提示的末尾。

    放在最后是刻意的：系统提示里越靠后的内容，模型越容易当"当前指令"。
    """
    base = (base or "").rstrip()
    return f"{base}\n\n{DICE_DOCTRINE}" if base else DICE_DOCTRINE


def _main() -> int:
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("=" * 70)
    print(DICE_DOCTRINE)
    print()
    print("=" * 70)
    print(PROLOGUE_FLOW)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())