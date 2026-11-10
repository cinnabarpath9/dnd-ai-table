# coding=utf-8
"""战斗意图解析：真模型常见的五种形状都要认，但单位/选项/目标三关不放松。"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.models import ModelReply, parse_intent_reply  # noqa: E402
from engine.rules.menu import Menu, Option  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def mk(uid: str) -> Menu:
    return Menu(unit_id=uid, unit_name=uid, team="enemy", options=[
        Option(id="a1", kind="attack", slot="action", name="轻弩",
               summary="射一箭", target_ids=["布兰"], requires_target=True),
        Option(id="a12", kind="move", slot="action", name="移动", summary="走"),
        Option(id="a2", kind="save_action", slot="action", name="火球",
               summary="群体", target_ids=["E1", "E2", "E3"],
               requires_target=True, max_targets=2),
    ])


MENUS = {"E1": mk("E1"), "E2": mk("E2")}
ONE = {"布兰": Menu(unit_id="布兰", unit_name="布兰", team="pc", options=[
    Option(id="a1", kind="attack", slot="action", name="长剑",
           summary="砍", target_ids=["E1"], requires_target=True)])}


def parse(data, menus):
    intents, probs = parse_intent_reply(ModelReply(data=data), menus)
    return [(i.unit, i.option_id, i.target) for i in intents], probs


def parse_fill(data, menus):
    intents, probs = parse_intent_reply(ModelReply(data=data), menus,
                                        fill_missing_target=True)
    return ([(i.unit, i.option_id, i.target) for i in intents], probs,
            [i.extra.get("auto_target") for i in intents])


out, probs = parse([{"unit": "E1", "actions": [{"id": "a1", "target": "布兰"}]}],
                   MENUS)
ok(out == [("E1", "a1", "布兰")] and not probs, "A1 标准数组", (out, probs))

out, probs = parse({"E1": {"action": {"id": "a1", "target": "布兰"},
                           "movement": {"id": "a12", "distance_ft": 0}},
                    "E2": {"action": {"id": "a1", "target": "布兰"}}}, MENUS)
ok(sorted(out) == [("E1", "a1", "布兰"), ("E2", "a1", "布兰")] and not probs,
   "A2 ⭐ 单位当键 `{E1:{action:...}}` 能拆", (out, probs))

out, probs = parse({"actions": [
    {"unit": "E1", "action": "a1", "target": "布兰"},
    {"unit": "E2", "id": "a1", "target": "布兰"}]}, MENUS)
ok(sorted(out) == [("E1", "a1", "布兰"), ("E2", "a1", "布兰")] and not probs,
   "A3 ⭐ actions 每条自带 unit 能分组", (out, probs))

out, probs = parse({"id": "a1", "target": "E1"}, ONE)
ok(out == [("布兰", "a1", "E1")] and not probs,
   "A4 ⭐ 单单位 `{id,target}` 能落到唯一单位", (out, probs))

out, probs = parse({"unit": "布兰", "action": {"id": "a1", "target": "E1"}}, ONE)
ok(out == [("布兰", "a1", "E1")] and not probs,
   "A5 条目只有 action（没有 actions）也能认", (out, probs))

out, probs = parse([{"unit": "E9", "actions": [{"id": "a1"}]}], MENUS)
ok(not out and any("未知单位" in p for p in probs), "A6 未知单位照样拒", probs)

out, probs = parse([{"unit": "E1", "actions": [{"id": "a1"}]}], MENUS)
ok(not out and any("需要目标" in p for p in probs), "A7 需要目标没给照样拒", probs)

_out, _probs, _auto = parse_fill(
    [{"unit": "E1", "actions": [{"id": "a1"}]}], MENUS)
ok(_out == [("E1", "a1", "布兰")] and not _probs and _auto == [True],
   "A7b ⭐ AI 座位可补菜单首个目标；严格模式仍拒", (_out, _probs, _auto))

out, probs = parse([{"unit": "E1", "actions": [{"id": "x9", "target": "布兰"}]}],
                   MENUS)
ok(not out and any("菜单里没有" in p for p in probs), "A8 发明选项照样拒", probs)

out, probs = parse({"E1": {"action": {"id": "a12"}}}, MENUS)
ok(out == [("E1", "a12", "")] and not probs, "A9 无目标的移动能过", (out, probs))

out, probs = parse([{"unit": "E1", "actions": [
    {"id": "a2", "targets": ["E1", "E2"]}]}], MENUS)
ok(out == [("E1", "a2", "E1")] and not probs, "A10 targets 数组能过", (out, probs))

out, probs = parse([{"unit": "E1", "actions": [
    {"id": "a2", "targets": ["E1", "E2", "E3"]}]}], MENUS)
ok(not out and any("最多" in p for p in probs), "A11 目标超过 max_targets 拒绝", probs)

out, probs = parse([{"id": "a1", "target": "E1"}], ONE)
ok(out == [("布兰", "a1", "E1")] and not probs,
   "A12 ⭐ 单单位动作数组能落到唯一单位", (out, probs))

out, probs = parse({"actions": [
    {"unit": "E1", "action": {"id": "a1", "target": "布兰"}},
    {"action": {"id": "a1", "target": "布兰"}}]}, MENUS)
ok(out == [("E1", "a1", "布兰")] and any("缺少 unit" in p for p in probs),
   "A13 部分单位残缺：保留合法、报缺 unit", (out, probs))

out, probs = parse({"E1": {"action": {"id": "a1", "target": "布兰"},
                          "bonus_action": {"id": "a12"}}}, MENUS)
ok(out and any("bonus_action" in p for p in probs),
   "A14 ⭐ bonus_action 非空：行动照收，但必须报问题", (out, probs))

out, probs = parse([{"unit": "E1", "actions": [
    {"id": "a1", "target": "布兰", "reaction": {"id": "a12"}}]}], MENUS)
ok(out and any("reaction" in p for p in probs),
   "A15 行动里的 reaction 非空：必须报问题", (out, probs))

from engine.rules.loop import _first_intent_per_unit  # noqa: E402
from engine.models import Intent  # noqa: E402


class _Log:
    def __init__(self):
        self.problems = []


log = _Log()
dup = _first_intent_per_unit([
    Intent(unit="E1", option_id="a1"), Intent(unit="E1", option_id="a12")], log)
ok(list(dup) == ["E1"] and dup["E1"].option_id == "a1"
   and any("多条行动" in p for p in log.problems),
   "A16 ⭐ planner 一个单位多条：只取第一条并记问题", log.problems)

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")