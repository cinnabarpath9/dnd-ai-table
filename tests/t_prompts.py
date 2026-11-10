# coding=utf-8
"""P5-E 回归：牌桌上的行为契约（骰子铁则 / 序章流程）。

守四件事：

    A. 骰子铁则**真的接进了 DM 系统提示**，而且没把原来的硬规则挤掉；
    B. 它**没进玩家提示词**（玩家不该背 DM 的契约）；
    C. 序章流程是独立的、五步、一步一问，**不并进每轮 DM 提示**（省 token）；
    D. 契约文字是确定的，没有随机/时间依赖（否则前缀缓存会抖）。

零网络、零花费。
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.prompts import DICE_DOCTRINE, PROLOGUE_FLOW  # noqa: E402
from engine.rules.prompts import dm_system_prompt  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


section("A. 骰子铁则：内容与接线")

ok("不掷骰" in DICE_DOCTRINE, "A1 写明 DM 不掷骰")
ok("以骰子为准" in DICE_DOCTRINE, "A2 ⭐ 冲突时以骰子为准（覆写，不是找补）")
ok("优势" in DICE_DOCTRINE, "A3 禁止随手给优势/劣势")
ok("我做到了" in DICE_DOCTRINE, "A4 ⭐ 玩家声明 ≠ 做到，要看检定")
ok("不要替他补" in DICE_DOCTRINE, "A5 不替玩家补没做的、没说的")
ok(DICE_DOCTRINE.count("\n1.") == 1 and DICE_DOCTRINE.count("\n5.") == 1,
   "A6 五条编号完整", DICE_DOCTRINE[:40])

base = "你是 DM。\n硬规则：以引擎为准。"
merged = dm_system_prompt(base)
ok(merged.startswith(base), "A7 原系统提示原样保留在前面")
ok(merged.rstrip().endswith(DICE_DOCTRINE.rstrip()),
   "A8 ⭐ 铁则接在系统提示**最后**（越靠后越像当前指令）")
ok(dm_system_prompt("") == DICE_DOCTRINE, "A9 空 base 时只返回铁则")

ok("骰子铁则" in GameLoop.DM_SYSTEM, "A10 ⭐ 铁则进了 GameLoop.DM_SYSTEM")
ok("你不掷骰、不改数值、不改 HP" in GameLoop.DM_SYSTEM,
   "A11 原来的硬规则没被挤掉")
ok("不许把角色没说出口的私有信息复述进旁白" in GameLoop.DM_SYSTEM,
   "A12 防全知那条还在")
ok("≤60 字" in GameLoop.PLAYER_SYSTEM, "say 有长度上限（≤60 字），防止把 JSON 撑断")
ok("骰子铁则" not in GameLoop.PLAYER_SYSTEM,
   "A13 ⭐ 玩家系统提示里没有 DM 的契约")
ok("序章" not in GameLoop.DM_SYSTEM,
   "A14 ⭐ 序章流程没有并进每轮 DM 提示（省 token）")


section("B. 序章流程：五步、一步一问、结尾固定")

ok("1. 审卡" in PROLOGUE_FLOW and "2. 选舞台" in PROLOGUE_FLOW
   and "3. 灵魂塑造" in PROLOGUE_FLOW and "4. 招募" in PROLOGUE_FLOW
   and "5. 启航" in PROLOGUE_FLOW, "B1 五步齐全")
ok("一步一问" in PROLOGUE_FLOW, "B2 写明一步一问")
ok("现在，你要做什么？" in PROLOGUE_FLOW, "B3 结尾固定句在")
ok("直接开始" in PROLOGUE_FLOW, "B4 保留「直接开始」的跳过路径")


section("C. 真提示词：DM 拿到、玩家拿不到")

st = StateTree.from_template({"甲": new_character(
    "甲", level=3, job="战士",
    abilities={"力量": 16, "敏捷": 14, "体质": 14}, hp_max=24, ac=15)},
    current="甲")
st.commit([{"op": "insert", "path": "/角色列表/甲/物品/长剑",
            "value": {"类别": "武器", "已装备": True}}],
          actor="填表", reason="装备")


class CaptureModel(FakeModel):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.reqs = []

    def _complete(self, req):
        self.reqs.append(req)
        return super()._complete(req)


dm = CaptureModel(name="dm")
pc = CaptureModel(name="pc")
loop = GameLoop(st, Ledger(), TableClient(dm=dm, planner=FakeModel(),
                                          players={"甲": pc}),
                rng=Random(1))
loop.set_field([make_hero_unit(st, "甲")])
loop.run_explore_round_all()
dm_p = next(r.prompt for r in dm.reqs if r.task == "narrate")
pc_p = next(r.prompt for r in pc.reqs if r.task == "explore_action")
ok("骰子铁则" in dm_p and "以骰子为准" in dm_p,
   "C1 ⭐ 真 DM 提示词里有骰子铁则")
ok("骰子铁则" not in pc_p, "C2 ⭐ 真玩家提示词里没有")
ok("序章" not in dm_p, "C3 序章没有混进每轮提示")

ok(dm_system_prompt(base) == merged and DICE_DOCTRINE == dm_system_prompt(""),
   "C4 同一个 base 两次拼出来一样（前缀缓存不会抖）")


print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")