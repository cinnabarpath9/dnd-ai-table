# coding=utf-8
"""H2a 回归：DM 造人物（数值块）→ 引擎 Unit → 能进战斗。

守四件事：
    · 写入口严格：缺生命值/属性名错/动作坏字段 → 报错，不写半截；
    · 读入口忠实：AC/HP/属性/豁免熟练/技能加值/先攻覆盖都进 Unit；
    · 参战：战斗中把人物插进当前先攻序，模型/引擎用它自己的攻击结算；
    · 边界：没数值的人物不能上场；同名只能在场一次；存档带先攻覆盖。
"""

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
from engine.models import FakeModel, Intent  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.npc_units import NpcError, build_npc_unit  # noqa: E402
from engine.rules.turn import execute_intent  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.web_actions import ActionError, apply_action  # noqa: E402

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


def fresh(name="甲"):
    st = StateTree.from_template({name: new_character(name, level=3, hp_max=24,
                                                      ac=15)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, name)])
    return st, loop


STATS = {
    "name": "疤脸", "身份": "哨塔守卫", "等级": "3",
    "护甲等级": "14", "生命值": "22", "先攻加值": "5", "速度": "30",
    "属性": {"力量": 14, "敏捷": 12, "体质": 14,
             "智力": 10, "感知": 12, "魅力": 9},
    "技能加值": {"察觉": 4}, "豁免熟练": "敏捷,体质",
    "动作": {"砍刀": {"类型": "attack", "攻击加值": "5",
                      "伤害骰": "1d8+2", "伤害类型": "挥砍"}},
}


print("=== A. 写入口：坏数值不许落库 ===")
st, loop = fresh()
bad = [
    ({"name": "无命", "等级": "2"}, "缺生命值"),
    ({"name": "错属性", "生命值": "10", "属性": {"运气": 12}}, "属性名不认识"),
    ({**STATS, "name": "坏动作",
      "动作": {"砍刀": {"类型": "attack"}}}, "攻击动作缺伤害骰"),
    ({**STATS, "name": "零血", "生命值": "0"}, "生命值越界"),
    ({**STATS, "name": "坏豁免", "豁免熟练": "幸运"}, "豁免属性不认识"),
]
for payload, label in bad:
    try:
        apply_action(st, loop, "甲", {"kind": "dm_set_npc_stats", **payload})
        ok(False, f"A·{label} 必须被拒")
    except ActionError as exc:
        ok(True, f"A·{label} → ActionError", str(exc))
ok((st.read("/世界/角色") or {}) == {},
   "A6 被拒的写入没有登记出半个人", st.read("/世界/角色"))


print("\n=== B. 读入口：数值块忠实变成 Unit ===")
r = apply_action(st, loop, "甲", {"kind": "dm_set_npc_stats", **STATS})
ok(r.get("ok") is True and "AC 14" in r.get("message", ""),
   "B1 人物数值写入当局", r.get("message"))
node = st.read("/世界/角色/疤脸") or {}
ok(node.get("身份") == "哨塔守卫" and "数值" in node,
   "B2 身份进世界树、数值单独一块", node.keys())
u = build_npc_unit(st, "疤脸", team="enemy", uid="N1")
ok(u.ac == 14 and u.hp_max == 22 and u.level == 3,
   "B3 AC / HP / 等级按 DM 写的算", (u.ac, u.hp_max, u.level))
ok(u.initiative_bonus == 5, "B4 ⭐ 先攻加值用数值块的总值，不叠敏捷",
   (u.initiative_bonus, u.mod("dexterity")))
ok(u.passive_perception == 14, "B5 被动察觉 = 10 + 察觉加值",
   u.passive_perception)
ok("dexterity" in u.save_proficiencies and "constitution" in u.save_proficiencies,
   "B6 豁免熟练按 DM 写的进引擎", u.save_proficiencies)
atk = u.actions[0].attacks[0] if u.actions and u.actions[0].attacks else None
ok(atk is not None and atk.attack_bonus == 5 and atk.damage_notation == "1d8+2",
   "B7 ⭐ 人物攻击走它自己的数值", atk)

st2 = StateTree.from_template({"乙": new_character("乙", level=1)})
loop2 = GameLoop(st2, Ledger(), FakeModel())
loop2.set_field([make_hero_unit(st2, "乙")])
apply_action(st2, loop2, "乙", {"kind": "dm_set_npc_stats", "name": "路人",
                                 "生命值": 9})
u2 = build_npc_unit(st2, "路人", team="enemy", uid="N2")
ok(bool(u2.actions) is False and any("没有写任何动作" in n for n in u2.notes),
   "B8 没写动作 → 明确说不能攻击", u2.notes)

print("\n=== C. 参战：插进当前先攻序，用自己的攻击结算 ===")
st, loop = fresh()
apply_action(st, loop, "甲", {"kind": "dm_set_npc_stats", **STATS})
try:
    apply_action(st, loop, "甲", {"kind": "dm_add_npc_to_combat",
                                  "name": "疤脸", "team": "enemy"})
    ok(False, "C1 战斗外不许直接放敌人上场")
except ActionError as exc:
    ok(True, "C1 战斗外放人上场 → ActionError", str(exc))

loop.start_combat(enemies=[("Bandit", 1)], distance=20)
r = apply_action(st, loop, "甲", {"kind": "dm_add_npc_to_combat",
                                  "name": "疤脸", "team": "enemy"})
ok(r.get("ok") is True and "加入战斗" in r.get("message", ""),
   "C2 战斗中放人物上场", r.get("message"))
npc = next((x for x in loop.field if x.name == "疤脸"), None)
ok(npc is not None and npc.id.startswith("N"), "C3 人物拿到独立单位 id", npc.id if npc else None)
ok(any(x.id == npc.id for x, _i in loop.initiative),
   "C4 ⭐ 人物进了当前先攻序（不是只在 field 里）", [x.name for x, _ in loop.initiative])
_old_init = loop.initiative
apply_action(st, loop, "甲", {"kind": "dm_set_npc_stats",
                              "name": "第二个", "生命值": 9,
                              "动作": {"拳": {"类型": "attack",
                                              "伤害骰": "1d4"}}})
apply_action(st, loop, "甲", {"kind": "dm_add_npc_to_combat",
                              "name": "第二个", "team": "enemy"})
ok(loop.initiative is not _old_init,
   "C4b ⭐ 战斗中加入换新列表（不搅乱本轮正在遍历的旧列表）")
try:
    apply_action(st, loop, "甲", {"kind": "dm_add_npc_to_combat",
                                  "name": "疤脸", "team": "enemy"})
    ok(False, "C5 同名人物不能在场两次")
except ActionError as exc:
    ok(True, "C5 同名再上场 → ActionError", str(exc))

pc = loop.find_unit("甲")
npc.position, pc.position = (5.0, 0.0), (0.0, 0.0)
menu = build_menu(npc, loop.field)
opt = next((o for o in menu.options if o.kind == "attack" and "砍刀" in o.name), None)
ok(opt is not None, "C6 人物的攻击出现在菜单里",
   [o.name for o in menu.options if o.kind == "attack"])
hp0 = pc.hp
res = execute_intent(npc, Intent(unit=npc.id, option_id=opt.id, target=pc.id),
                     menu, loop.field, Random(11))
ok(res.ok and res.attacks and res.attacks[0].hit,
   "C7 ⭐ 人物自己的攻击加值参与掷骰", (res.ok, res.blocked, res.attacks))
dmg = res.attacks[0].damage_total if res.attacks else 0
ok(3 <= dmg <= 10 and pc.hp == max(0, hp0 - dmg),
   "C8 伤害按 1d8+2 结算", (dmg, hp0, pc.hp))

print("\n=== D. 边界：没数值不能上场 / 存档带先攻覆盖 ===")
st3 = StateTree.from_template({"丙": new_character("丙", level=3)})
loop3 = GameLoop(st3, Ledger(), FakeModel())
loop3.set_field([make_hero_unit(st3, "丙")])
loop3.world.register_npc("只有名字", role="路人", actor="DM")
loop3.start_combat(enemies=[("Bandit", 1)], distance=20)
try:
    apply_action(st3, loop3, "丙", {"kind": "dm_add_npc_to_combat",
                                    "name": "只有名字", "team": "enemy"})
    ok(False, "D1 没有数值块的人物不能上场")
except ActionError as exc:
    ok(True, "D1 没数值 → ActionError（不偷偷给通用怪）", str(exc))

from engine.persist import unit_from_dict, unit_to_dict  # noqa: E402
back = unit_from_dict(unit_to_dict(u))
ok(back.init_override == 5 and back.ac == 14,
   "D2 ⭐ 存读档保留先攻覆盖与 AC", (back.init_override, back.ac))

from engine.demo_web import npc_snapshot  # noqa: E402
rows = npc_snapshot(loop)
row = next((x for x in rows if x["name"] == "疤脸"), None)
ok(row is not None and row["stats"].get("生命值") == 22 and row["in_field"],
   "D3 网页快照带人物数值与在场状态", row)

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")