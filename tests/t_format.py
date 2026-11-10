# coding=utf-8
"""P0-3 回归：模型输出**严格体检** + 最多一次纠错 + 不拿补写冒充新生成。

守的口径：

    · 五象限：valid / invalid（完整但非法）/ truncated / refusal / error；
    · 只有 invalid 纠错一次；截断/拒答/传输错误原样返回（走重生成或本地兜底）；
    · 第一次就合法 → 不多调一次；真人不自动纠错；
    · 纠错仍失败 → 返回原文，调用方走本地兜底（这里用 parse_intent_reply 验证）。
"""

from __future__ import annotations

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.memory import MemoryStore  # noqa: E402
from engine.models import FakeModel, ModelReply, parse_intent_reply  # noqa: E402
from engine.models.format_check import classify, repair_hint  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.state.assembler import Assembler  # noqa: E402

FAILS: list[str] = []
COUNT = 0
WHO = "甲"
AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def make_world() -> tuple[StateTree, GameLoop, object, object, dict]:
    st = StateTree.from_template(
        {WHO: new_character(WHO, level=3, job="战士", abilities=AB,
                            hp_max=24, ac=15)}, current=WHO)
    st.commit([{"op": "insert", "path": f"/角色列表/{WHO}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}}],
              actor="填表", reason="装备")
    unit = make_hero_unit(st, WHO)
    loop = GameLoop(st, Ledger(), FakeModel(name="bad"), memory=MemoryStore())
    loop.set_field([unit])
    menu = build_menu(unit, [unit])
    return st, loop, unit, menu, {unit.id: menu}


def pick_opt(menu) -> str:
    for o in menu.options:
        if not o.requires_target:
            return o.id
    return menu.options[0].id


def good_json(unit, menu) -> str:
    oid = pick_opt(menu)
    return json.dumps([{"unit": unit.id, "actions": [{"id": oid}]}],
                      ensure_ascii=False)


st, loop, unit, menu, menus = make_world()
GOOD = good_json(unit, menu)
asm = Assembler(st, Ledger()).build(unit.name, task="行动")


# --------------------------------------------------------------------------- #
section("A. 五象限：这段输出到底是什么")

ok(classify(ModelReply(text=GOOD, data=json.loads(GOOD)), menus).status == "valid",
   "A1 合法 JSON + 合法选项 → valid")
bad = json.dumps([{"unit": unit.id, "actions": [{"id": "a999"}]}])
ok(classify(ModelReply(text=bad, data=json.loads(bad)), menus).status == "invalid",
   "A2 ⭐ 完整 JSON 但选项不存在 → invalid")
bad2 = '{"unit": "甲", "actions": [{"id": "a1",}]}'
ok(classify(ModelReply(text=bad2, data=None), menus).status == "invalid",
   "A3 ⭐ 括号配平但 JSON 坏 → invalid（不是截断）")
part = '{"unit": "甲", "actions": [{"id": "a1"'
ok(classify(ModelReply(text=part, data=None), menus).status == "truncated",
   "A4 ⭐ 括号没配平 → truncated")
ok(classify(ModelReply(text="抱歉，我不能继续这个场景。", data=None),
            menus).status == "refusal",
   "A5 ⭐ 拒答/道歉 → refusal")
ok(classify(ModelReply(text="", data=None), menus).status == "truncated",
   "A6 空回复 → truncated")
ok(classify(ModelReply(ok=False, error="超时"), menus).status == "error",
   "A7 ⭐ 传输错误 → error（不再请求纠错）")
weird = json.dumps([{"unit": "幽灵", "actions": [{"id": "a1"}]}])
ok(classify(ModelReply(text=weird, data=json.loads(weird)), menus).status == "invalid",
   "A8 未知单位 → invalid")


# --------------------------------------------------------------------------- #
section("B. 纠错提示：只列问题和合法 id")

hint = repair_hint(["甲 选了菜单里没有的选项 'a999'"], menus)
ok("a999" in hint and "合法单位与选项" in hint,
   "B1 提示里有原问题、有合法清单", hint[:80])
ok(unit.id in hint and pick_opt(menu) in hint,
   "B2 ⭐ 合法清单里真有该单位的选项 id")
ok(len(repair_hint(["x"] * 20, menus)) < 900,
   "B3 问题再多也不把提示撑爆", len(repair_hint(["x"] * 20, menus)))


# --------------------------------------------------------------------------- #
section("C. 调用层：一次纠错，不重复问真人")


def run_checked(script: list[str], *, repair: bool = True) -> tuple[object, int]:
    model = FakeModel(script=script, name="scripted")
    loop2 = GameLoop(st, Ledger(), model)
    reply = loop2._call_checked(role="player", actor=unit.name, task="combat_turn",
                                assembled=asm, expect="intent", menus=menus,
                                allowed={}, repair=repair)
    return reply, model._i


reply, calls = run_checked([GOOD])
ok(classify(reply, menus).status == "valid" and calls == 1,
   "C1 第一次就合法 → 只调一次", (classify(reply, menus).line(), calls))

reply, calls = run_checked([bad, GOOD])
ok(classify(reply, menus).status == "valid" and calls == 2,
   "C2 ⭐ invalid → 纠错一次，用第二次的输出", (classify(reply, menus).line(), calls))

reply, calls = run_checked([bad, bad])
ok(classify(reply, menus).status == "invalid" and calls == 2,
   "C3 ⭐ 两次都非法 → 不再重试（返回原文）",
   (classify(reply, menus).line(), calls))
intents, problems = parse_intent_reply(reply, menus)
ok(intents == [] and problems,
   "C4 ⭐ 仍非法时调用方拿得到问题列表（走本地兜底，不静默）")

reply, calls = run_checked(["抱歉，我不能写这个。", GOOD])
# 2026-11-09 起：拒答/审核只走**一次虚构边界重述**（不是 invalid 纠错）；
# 重述成功就用新的，仍拒才兜底（见 t_guard）。
ok(classify(reply, menus).status == "valid" and calls == 2,
   "C5 ⭐ 拒答只重述一次（不走 invalid 纠错）",
   (classify(reply, menus).line(), calls))

reply, calls = run_checked([part, GOOD])
ok(classify(reply, menus).status == "truncated" and calls == 1,
   "C6 ⭐ 截断不纠错（补写不是新生成）", (classify(reply, menus).line(), calls))

reply, calls = run_checked([bad], repair=False)
ok(classify(reply, menus).status == "invalid" and calls == 1,
   "C7 repair=False 时不自动纠正（给真人座位留的口子）", calls)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")