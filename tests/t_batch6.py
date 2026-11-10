# coding=utf-8
"""Batch 6 回归：网页轮询保选中 / patch move 子树 / auto 耗尽 / 战斗台词。"""

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

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.models.client import (  # noqa: E402
    ModelReply, ModelRequest, parse_intent_reply,
)
from engine.models.seats import HumanSeat  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.state.patch import PatchOpError, apply_patch  # noqa: E402
from engine.state.world import new_world  # noqa: E402
from engine.webui import PAGE_HTML  # noqa: E402

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


AB = {"力量": 14, "敏捷": 12, "体质": 12, "智力": 10, "感知": 10, "魅力": 10}

# --------------------------------------------------------------------------- #
section("B6-1 patch：move 不能移进自己的子树")

doc = {"a": {"b": 1}, "c": 2}
try:
    apply_patch(doc, [{"op": "move", "from": "/a", "path": "/a/b"}])
    ok(False, "B6-1a 移进子树必须被拒")
except PatchOpError:
    ok(True, "B6-1a ⭐ 移进子树 → PatchOpError")
ok(doc == {"a": {"b": 1}, "c": 2},
   "B6-1b ⭐ 失败后源整棵还在（不再静默丢 a）", doc)
moved = apply_patch({"a": {"b": 1}, "c": 2},
                    [{"op": "move", "from": "/a/b", "path": "/d"}])
ok(moved.get("d") == 1 and "b" not in moved["a"],
   "B6-1c 正常 move 仍可用", moved)


# --------------------------------------------------------------------------- #
section("B6-2 无人值守：auto 用尽不许等 stdin")

unit = Unit(id="P", name="甲", hp=10, hp_max=10, team="pc", ac=12)
unit.actions.append(Action(name="砍", slot="action", attacks=[
    Attack(name="剑", attack_bonus=5, damage_notation="1d8+2",
           damage_type="slashing", reach=5)]))
foe = Unit(id="E1", name="敌", hp=10, hp_max=10, team="enemy", ac=10,
           position=(5.0, 0.0))
menu = build_menu(unit, [unit, foe])
req = ModelRequest(role="player", actor="甲", task="combat_turn",
                   prompt="", expect="intent",
                   allowed={"options": [o.to_dict() for o in menu.options]})
seat = HumanSeat(auto=[])
reply = seat.complete(req)            # 不允许挂住；必须立刻失败
ok(reply.ok is False and "用尽" in (reply.error or ""),
   "B6-2a ⭐ auto 为空 → 立刻失败（不回落 input）", (reply.ok, reply.error))


# --------------------------------------------------------------------------- #
section("B6-3 战斗轮台词不再丢")

seat2 = HumanSeat(auto=["a1 E1 我劈他"])
reply2 = seat2.complete(req)
data = reply2.data or {}
act0 = (data.get("actions") or [{}])[0]
ok(act0.get("say") == "我劈他",
   "B6-3a ⭐ 战斗意图里带上了台词", data)

intents, problems = parse_intent_reply(reply2, {"P": menu})
ok(bool(intents) and intents[0].extra.get("say") == "我劈他",
   "B6-3b 解析层保留 say", problems)

# 端到端：战斗轮把台词写进台账
st = StateTree.from_template({"甲": new_character("甲", level=3, job="战士",
                                                 abilities=dict(AB),
                                                 hp_max=20, ac=14)},
                             current="甲", world=new_world(date="1492-09-17",
                                                          period="午后"))
led = Ledger()
player_model = FakeModel(script=[
    json.dumps({"actions": [{"id": "a1", "target": "E1", "say": "我劈他"}]},
               ensure_ascii=False)])
table = TableClient(dm=FakeModel(name="dm"), planner=FakeModel(name="pl"),
                    players={"甲": player_model})
loop = GameLoop(st, led, table, rng=Random(5), label="台词测试")
loop.set_field([make_hero_unit(st, "甲")])
loop.start_combat(enemies=[("Bandit", 1)], distance=5)
loop.run_combat_round(pc_name="甲")
says = [e for e in led.entries if e.kind == "对话" and "我劈他" in e.text]
ok(bool(says), "B6-3c ⭐ 战斗台词进了台账", [e.text for e in led.entries][:5])


# --------------------------------------------------------------------------- #
section("B6-4 网页：轮询只在换题时重绘 + 超时 + 响应式")

ok("renderedSeq" in PAGE_HTML and "s.pending.seq !== renderedSeq" in PAGE_HTML,
   "B6-4a ⭐ 同一问题不会重复 render（保住选中态）")
ok("AbortController" in PAGE_HTML and "ctrl.abort()" in PAGE_HTML,
   "B6-4b 轮询有 5 秒超时")
ok("prompt(" not in PAGE_HTML.split("<script>")[-1],
   "B6-4c 目标选择不再用 prompt()")
ok("@media (max-width:900px)" in PAGE_HTML,
   "B6-4d 有窄屏响应式布局")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")