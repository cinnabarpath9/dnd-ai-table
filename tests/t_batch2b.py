# coding=utf-8
"""Batch 2B 回归：规划第 2 轮菜单 / AoE 多目标 / 充能 / 范围正则 / Dash / 战斗外死亡豁免。"""

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
from engine.longrun import CaptureModel, build_table_loop  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.models.client import Intent  # noqa: E402
from engine.rules.bestiary import build_unit, parse_action  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.turn import execute_intent  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.state.world import new_world  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402

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


AB = {"力量": 12, "敏捷": 14, "体质": 13, "智力": 10, "感知": 12, "魅力": 10}


def hero(name: str, hp: int = 24, pos=(0.0, 0.0)) -> Unit:
    return Unit(id=name, name=name, hp=hp, hp_max=hp, team="pc",
                ac=14, abilities=dict(AB), position=pos)


# --------------------------------------------------------------------------- #
section("B2B-1 敌方规划第 2 轮仍有攻击选项")

st, led, loop = build_table_loop(seed=20261105, names=("艾尔玛", "罗恩", "小翠", "布兰"))
cap = loop.client                       # build_table_loop 用 CaptureModel 当总客户端
loop.start_combat(enemies=[("Bandit", 2)], distance=20)
loop.run_combat_round(pc_name="艾尔玛")
loop.run_combat_round(pc_name="艾尔玛")
plans = [r for r in cap.reqs if r.task == "plan_all_enemies"]
last = plans[-1].allowed["units"]
kinds = {o["kind"] for u in last for o in u["options"]}
ok("attack" in kinds, "B2B-1a ⭐ 第 2 轮规划菜单里仍有攻击选项", sorted(kinds))
ok(any(o["kind"] == "attack" and o.get("targets")
       for u in last for o in u["options"]),
   "B2B-1b ⭐ 第 2 轮的攻击选项带着合法目标（不是空菜单）")


# --------------------------------------------------------------------------- #
section("B2B-2 AoE 豁免动作打到全部候选")

dragon = Unit(id="D", name="龙", hp=200, hp_max=200, team="enemy",
              ac=18, position=(0.0, 0.0))
dragon.actions.append(Action(
    name="吐息", slot="action", save_ability="dexterity", save_dc=12,
    save_success_half=True, damage_notation="4d6", damage_type="fire",
    area_size=30, area_shape="cone", structured=True))
a = hero("甲", pos=(10.0, 0.0))
b = hero("乙", pos=(20.0, 0.0))
menu = build_menu(dragon, [dragon, a, b])
br = next(o for o in menu.options if o.kind == "save_action")
out = execute_intent(dragon, Intent(unit="D", option_id=br.id, target="甲"),
                     menu, [dragon, a, b], Random(7))
rows = out.save_results[0].targets if out.save_results else []
ok(len(rows) == 2, "B2B-2a ⭐ 吐息结算了两个目标（不再只打一个）",
   [r.get("name") for r in rows])
ok(a.hp < 24 or b.hp < 24, "B2B-2b 两个目标都真的掉血（或豁免半伤）",
   (a.hp, b.hp))


# --------------------------------------------------------------------------- #
section("B2B-3 充能：没充上就不许选，用了就清零")

wyrm = build_unit("Ancient Red Dragon")[0]
breath = next(x for x in wyrm.actions if x.name == "Fire Breath")
key = f"recharge:{breath.name}"
wyrm.resources[key] = False
menu_no = build_menu(wyrm, [wyrm, hero("丙")])
ok(not any(o.name == "Fire Breath" for o in menu_no.options),
   "B2B-3a ⭐ 充能失败时吐息不在菜单里")
wyrm.resources[key] = True
menu_yes = build_menu(wyrm, [wyrm, hero("丙")])
opt = next((o for o in menu_yes.options if o.name == "Fire Breath"), None)
ok(opt is not None, "B2B-3b 充能成功时吐息可选", [o.name for o in menu_yes.options])
if opt is not None:
    execute_intent(wyrm, Intent(unit=wyrm.id, option_id=opt.id), menu_yes,
                   [wyrm, hero("丙")], Random(3))
    ok(wyrm.resources.get(key) is False, "B2B-3c 用过之后本回合不可再用",
       wyrm.resources.get(key))


# --------------------------------------------------------------------------- #
section("B2B-4 范围正则：线状/半径写法")

line_act = parse_action({"name": "酸吐息", "action_type": "ACTION",
                         "desc": "The dragon exhales acid in a 60-foot-long, "
                                 "5-foot-wide Line. DC 18 Dexterity saving throw."})
ok(line_act.area_size == 60 and line_act.area_shape == "line",
   "B2B-4a ⭐ 60-foot-long, 5-foot-wide Line → 60/line",
   (line_act.area_size, line_act.area_shape))
sphere_act = parse_action({"name": "雷击", "action_type": "ACTION",
                           "desc": "Each creature in a 20-foot-radius Sphere. "
                                   "DC 15 Dexterity saving throw."})
ok(sphere_act.area_size == 20 and sphere_act.area_shape == "sphere",
   "B2B-4b ⭐ 20-foot-radius Sphere → 20/sphere",
   (sphere_act.area_size, sphere_act.area_shape))
black = build_unit("Adult Black Dragon")[0]
acid = next(x for x in black.actions if x.name == "Acid Breath")
ok(acid.area_size == 60 and acid.area_shape == "line",
   "B2B-4c 成年黑龙吐息不再是单体", (acid.area_size, acid.area_shape))


# --------------------------------------------------------------------------- #
section("B2B-5 Dash：先声明后移动也要拿到双倍预算")

dash_unit = hero("疾走者", pos=(0.0, 0.0))
target = Unit(id="E", name="靶", hp=50, hp_max=50, team="enemy",
              ac=10, position=(60.0, 0.0))
dash_unit.actions.append(Action(
    name="砍", slot="action",
    attacks=[Attack(name="剑", attack_bonus=0, damage_notation="1d4",
                    damage_type="slashing", reach=5)]))
menu3 = build_menu(dash_unit, [dash_unit, target])
dash_opt = next(o for o in menu3.options if o.kind == "dash")
r = execute_intent(dash_unit, Intent(unit="疾走者", option_id=dash_opt.id),
                   menu3, [dash_unit, target], Random(3))
ok(r.ok and dash_unit.movement_used == -dash_unit.speed,
   "B2B-5a 疾走后 movement_used = -speed（预留双倍预算）",
   dash_unit.movement_used)

move_unit = hero("走位", pos=(0.0, 0.0))
move_unit.actions = []
foe = Unit(id="F", name="敌", hp=50, hp_max=50, team="enemy", ac=10,
           position=(55.0, 0.0))
menu4 = build_menu(move_unit, [move_unit, foe])
dash2 = next(o for o in menu4.options if o.kind == "dash")
execute_intent(move_unit, Intent(unit="走位", option_id=dash2.id), menu4,
               [move_unit, foe], Random(3))
move_opt = next(o for o in menu4.options if o.kind == "move")
mv = execute_intent(move_unit, Intent(unit="走位", option_id=move_opt.id,
                                      target="F"), menu4, [move_unit, foe],
                    Random(3))
ok(mv.ok and mv.moved > move_unit.speed,
   "B2B-5b ⭐ 先疾走再移动：实际移动超过基础速度", mv.moved)


# --------------------------------------------------------------------------- #
section("B2B-6 濒死 PC：探索轮与开战都要掷死亡豁免")

st2 = StateTree.from_template(
    {"艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=dict(AB),
                             hp_max=24, ac=16)},
    current="艾尔玛", world=new_world(date="1492-09-17", period="午后"))
led2 = Ledger()
table = TableClient(
    dm=FakeModel(name="dm"), planner=FakeModel(name="planner"),
    players={"艾尔玛": FakeModel(name="pc")})
loop2 = GameLoop(st2, led2, table, rng=Random(11), label="濒死测试")
unit = make_hero_unit(st2, "艾尔玛")
unit.hp = 0
loop2.set_field([unit])
before = dict(loop2.death)
loop2.run_explore_round_all(pc_names=["艾尔玛"], situation="他倒在地上。")
d = loop2.death.get("艾尔玛")
ok(d is not None and (d.successes or d.failures or d.stable or d.dead),
   "B2B-6a ⭐ 探索轮里倒地 PC 掷了死亡豁免", None if d is None else d.line())
ok(any("死亡豁免" in e.text for e in led2.entries),
   "B2B-6b 死亡豁免进了台账")

loop2.start_combat(enemies=[("Bandit", 1)], distance=20)
ok(any(u.id == "艾尔玛" for u, _v in loop2.initiative),
   "B2B-6c ⭐ 濒死 PC 仍在先攻表里（只投死亡豁免）",
   [(u.id, u.hp) for u, _v in loop2.initiative])


# --------------------------------------------------------------------------- #
section("B2B-7 Multiattack：真正展开成多次攻击")

red = build_unit("Adult Red Dragon")[0]
dummy = Unit(id="T", name="木桩", hp=300, hp_max=300, team="pc",
             ac=10, position=(10.0, 0.0))
menu5 = build_menu(red, [red, dummy])
multi = next((o for o in menu5.options
              if o.kind == "special" and "Multiattack" in o.name), None)
ok(multi is not None, "B2B-7a Multiattack 在菜单里", [o.name for o in menu5.options])
if multi is not None:
    mo = execute_intent(red, Intent(unit=red.id, option_id=multi.id, target="T"),
                        menu5, [red, dummy], Random(11))
    ok(mo.ok and len(mo.attacks) == 3,
       "B2B-7b ⭐ 一回合打出 3 次 Rend（不再只打 1 次）",
       (mo.ok, len(mo.attacks)))


# --------------------------------------------------------------------------- #
section("B2B-8 traits 不再变成主动动作")

balor = build_unit("Balor")[0]
ok(not any("Death Throes" in a.name for a in balor.actions),
   "B2B-8a ⭐ Death Throes 不再是每回合可用的动作",
   [a.name for a in balor.actions])
baboon = build_unit("Baboon")[0]
ok(not any("Pack Tactics" in a.name for a in baboon.actions),
   "B2B-8b 被动特性（Pack Tactics）也不进动作")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")