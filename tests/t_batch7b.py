# coding=utf-8
"""Batch 7B 回归：座位 role / 豁免 breakdown / 戏法菜单 / 多类敌人摆位。"""

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

from engine.dice import Random  # noqa: E402
from engine.longrun import build_table_loop  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.models.client import ModelError, ModelRequest  # noqa: E402
from engine.rules.adjudicate import saving_throw  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.spells import get_spell  # noqa: E402
from engine.rules.unit import Unit  # noqa: E402

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


# --------------------------------------------------------------------------- #
section("B7B-1 不认识的 role 不许静默兜给 DM")

table = TableClient(dm=FakeModel(name="dm"), planner=FakeModel(name="pl"))
req = ModelRequest(role="filler", actor="x", task="t", prompt="p")
try:
    table.seat_for(req)
    ok(False, "B7B-1a 未知 role 必须报错")
except ModelError:
    ok(True, "B7B-1a ⭐ 未知 role → ModelError")


# --------------------------------------------------------------------------- #
section("B7B-2 豁免 breakdown 含熟练（中文键）")

u = Unit(id="P", name="甲", hp=20, hp_max=20, team="pc", level=5,
         abilities={"力量": 16, "敏捷": 10, "体质": 10, "智力": 10,
                    "感知": 10, "魅力": 10})
u.save_proficiencies = {"力量"}
r = saving_throw(u, "strength", 10, Random(3))
ok(r.check.bonus == 6 and r.check.bonus_breakdown.get("熟练") == 3,
   "B7B-2a ⭐ 力量豁免 +6，明细里有「熟练 +3」",
   r.check.bonus_breakdown)


# --------------------------------------------------------------------------- #
section("B7B-3 戏法菜单摘要带角色等级")

mage = Unit(id="M", name="法师", hp=20, hp_max=20, team="pc", level=5,
            position=(0.0, 0.0))
mage.spells = [get_spell("Fire Bolt")]
menu = build_menu(mage, [mage])
line = next(o.summary for o in menu.options if o.kind == "spell")
ok("2d10" in line, "B7B-3a ⭐ 5 级 Fire Bolt 菜单显示 2d10", line)
ok(get_spell("Fire Bolt").menu_line(0, 1).endswith("1d10 fire"),
   "B7B-3b 1 级仍显示 1d10")


# --------------------------------------------------------------------------- #
section("B7B-4 多类敌人摆位不重叠")

_st, _led, loop = build_table_loop(seed=5, names=("A", "B", "C", "D"))
loop.start_combat(enemies=[("Bandit", 2), ("Wolf", 2)], distance=20)
enemies = [u for u in loop.field if u.team == "enemy"]
xs = [u.position[0] for u in enemies]
ok(len(enemies) == 4 and len(set(xs)) == 4,
   "B7B-4a ⭐ 四种敌人坐标各不相同", xs)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")