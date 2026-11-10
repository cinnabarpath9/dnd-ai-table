# coding=utf-8
"""H2b 回归：临时修正（AC/攻击/豁免/技能/伤害）+ 到期 + 剧本 effects。

守五件事：
    · 写入口：坏类型/坏值/坏对象 → 报错；
    · 生效：修正真的进判定（命中/豁免/技能/伤害），但**不改基础 AC/属性**；
    · 到期：`round` 每到自己回合扣 1 轮，`scene` 换场清掉；
    · 剧本：`scenes[].effects` 能读、能校验、能在进场景/开战时挂上（含敌人）；
    · 存读档与网页快照不丢修正。
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

from engine.adventure import AdventureRunner, load_adventure  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.adjudicate import (  # noqa: E402
    attack_roll, resolve_attack, saving_throw, skill_check,
)
from engine.rules.effects import (  # noqa: E402
    add_mod, clear_mods, coerce_effect,
)
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.web_actions import ActionError, apply_action  # noqa: E402

FAILS: list[str] = []
COUNT = 0
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "effects_scout.json")


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def fresh(name="甲", hp_max=24, ac=15):
    st = StateTree.from_template({name: new_character(name, level=3,
                                                      hp_max=hp_max, ac=ac)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, name)])
    return st, loop


print("=== A. 写入口：坏修正不许挂 ===")
bad = [
    ({"name": "坏类型", "kind": "luck", "value": 1}, "类型不认识"),
    ({"name": "坏值", "kind": "attack", "value": "x"}, "值不是整数"),
    ({"name": "越界", "kind": "ac", "value": 99}, "值越界"),
    ({"name": "坏持续", "kind": "ac", "value": 1, "scope": "always"}, "持续不认识"),
    ({"name": "坏对象", "kind": "save", "value": 1, "object": "运气"}, "豁免属性不认识"),
    ({"name": "", "kind": "ac", "value": 1}, "缺名称"),
    ({"name": "轮数坏", "kind": "ac", "value": 1, "scope": "round",
      "rounds": 0}, "轮数越界"),
]
for spec, label in bad:
    try:
        coerce_effect(spec)
        ok(False, f"A·{label} 必须被拒")
    except ValueError as exc:
        ok(True, f"A·{label} → ValueError", str(exc))
eff = coerce_effect({"name": "高地", "kind": "攻击", "value": 1})
ok(eff["kind"] == "attack" and eff["scope"] == "scene",
   "A8 中文类型/默认持续能归一", eff)

print("\n=== B. 生效：进判定，不改基础值 ===")
st, loop = fresh()
pc = loop.find_unit("甲")
pc.ac = 15
add_mod(pc, {"name": "护盾", "kind": "ac", "value": 2,
             "scope": "scene", "rounds": 0, "target": ""})
ok(pc.ac == 15 and pc.ac_total == 17,
   "B1 ⭐ AC 修正只进有效值，基础 AC 不变", (pc.ac, pc.ac_total))

atk = Unit(id="A", name="弓手", team="enemy", hp_max=10, hp=10,
           ac=12, level=3)
pc2 = loop.find_unit("甲")
pc2.position = (5.0, 0.0)
atk.position = (0.0, 0.0)
attack = Attack(name="木棍", attack_bonus=0, damage_notation="1d6",
                damage_type="bludgeoning", reach=5)
hit0 = attack_roll(atk, pc2, attack.attack_bonus, pc2.ac_total, Random(9)).hit
add_mod(atk, {"name": "高地", "kind": "attack", "value": 10,
              "scope": "scene", "rounds": 0, "target": ""})
hit1 = attack_roll(atk, pc2, attack.attack_bonus + 10, pc2.ac_total,
                   Random(9)).hit
ok(hit0 is False and hit1 is True,
   "B2 ⭐ 攻击修正真的改命中（同种子）", (hit0, hit1))

s0 = saving_throw(pc2, "dexterity", 12, Random(16)).check.success
add_mod(pc2, {"name": "湿滑", "kind": "save", "value": -5,
              "scope": "scene", "rounds": 0, "target": "dexterity"})
s1 = saving_throw(pc2, "dexterity", 12, Random(16)).check.success
ok(s0 is True and s1 is False,
   "B3 ⭐ 豁免修正按属性生效", (s0, s1))

add_mod(pc2, {"name": "雾", "kind": "skill", "value": -20,
              "scope": "scene", "rounds": 0, "target": "perception"})
ok(skill_check(pc2, "perception", 5, Random(1)).check.success is False,
   "B4 ⭐ 技能修正生效")

atk.actions = [Action(name="木棍", slot="action", attacks=[attack])]
add_mod(atk, {"name": "钝器", "kind": "damage", "value": 3,
              "scope": "scene", "rounds": 0, "target": ""})
strong = Attack(name="重击", attack_bonus=10, damage_notation="1d6",
                    damage_type="bludgeoning", reach=5)
res = resolve_attack(atk, pc2, strong, Random(11), distance=5.0)
ok(res.hit and 4 <= res.damage_total <= 9,
   "B5 ⭐ 伤害修正是加在掷骰后的固定值", res.damage_total)

print("\n=== C. 到期：轮次扣减 / 换场清掉 ===")
st2, loop2 = fresh("乙")
u = loop2.find_unit("乙")
add_mod(u, {"name": "鼓劲", "kind": "attack", "value": 1,
            "scope": "round", "rounds": 2, "target": ""})
u.reset_turn()
left = next((m for m in u.mods if m["name"] == "鼓劲"), None)
ok(left is not None and left["rounds"] == 1, "C1 轮次修正第一回合剩 1 轮", left)
u.reset_turn()
ok(not any(m["name"] == "鼓劲" for m in u.mods), "C2 ⭐ 第二回合到期掉掉")
ok(any("修正到期" in n for n in u.notes), "C3 到期写进备注", u.notes)

add_mod(u, {"name": "场景光", "kind": "ac", "value": 1,
            "scope": "scene", "rounds": 0, "target": ""})
loop2.set_scene_effects([], source="s-next")
ok(not any(m["name"] == "场景光" for m in u.mods),
   "C4 ⭐ 换场清掉场景修正")

print("\n=== D. 剧本 effects：能读、能挂、敌人也有 ===")
adv = load_adventure(FIXTURE)
ok(adv.scenes[0].effects and adv.scenes[0].effects[0]["name"] == "坡顶视野",
   "D1 剧本 effects 读进对象", adv.scenes[0].effects)
st3 = StateTree.from_template({
    "甲": new_character("甲", level=3, hp_max=24, ac=15),
    "乙": new_character("乙", level=3, hp_max=24, ac=15)})
loop3 = GameLoop(st3, Ledger(), FakeModel())
loop3.set_field([make_hero_unit(st3, "甲"), make_hero_unit(st3, "乙")])
runner = AdventureRunner(loop3, adv)
runner.run_scene(adv.scene("s1"))
a = loop3.find_unit("甲")
b = loop3.find_unit("乙")
ok(any(m["name"] == "坡顶视野" for m in a.mods)
   and any(m["name"] == "雨后薄雾" for m in b.mods),
   "D2 ⭐ 进场景就把 effects 挂到全队", [m["name"] for m in a.mods])

runner.run_scene(adv.scene("s2"), combat_rounds=0)
a2 = loop3.find_unit("甲")
enemies = [u for u in loop3.field if u.team == "enemy"]
ok(not any(m["name"] == "坡顶视野" for m in a2.mods),
   "D3 ⭐ 换场后上一场的场景修正没了", [m["name"] for m in a2.mods])
ok(any(m["name"] == "湿滑桥面" for m in a2.mods),
   "D4 新场的效果挂上了", [m["name"] for m in a2.mods])
ok(enemies and all(any(m["name"] == "守夜人熟悉桥面" for m in e.mods)
                   for e in enemies),
   "D5 ⭐ 针对敌人的效果在开战后挂到敌人身上", len(enemies))

print("\n=== E. 网页动作 / 存读档 / 快照 ===")
st4, loop4 = fresh("丙")
r = apply_action(st4, loop4, "丙", {
    "kind": "dm_apply_effect", "target": "全队",
    "name": "高地", "类型": "attack", "值": 2})
ok(r.get("ok") is True and "高地" in r.get("message", ""),
   "E1 DM 临场挂修正", r.get("message"))
ok(loop4.find_unit("丙").mods, "E2 修正挂到人身上")
r = apply_action(st4, loop4, "丙", {"kind": "dm_clear_effects",
                                     "target": "全队"})
ok(r.get("ok") is True and "清掉 1 条" in r.get("message", ""),
   "E3 DM 清修正", r.get("message"))
try:
    apply_action(st4, loop4, "丙", {"kind": "dm_apply_effect",
                                     "target": "全队", "类型": "attack",
                                     "值": 99})
    ok(False, "E4 坏修正必须被拒")
except ActionError as exc:
    ok(True, "E4 坏修正 → ActionError", str(exc))

from engine.persist import unit_from_dict, unit_to_dict  # noqa: E402
add_mod(loop4.find_unit("丙"), {"name": "高地", "kind": "attack", "value": 1,
                                "scope": "round", "rounds": 2, "target": ""})
back = unit_from_dict(unit_to_dict(loop4.find_unit("丙")))
ok(back.mods and back.mods[0]["rounds"] == 2,
   "E5 ⭐ 存读档保留临时修正", back.mods)

from engine.demo_web import sheet_snapshot  # noqa: E402
lines = (sheet_snapshot(loop4, "丙").get("sheet") or {}).get("mod_lines")
ok(lines and "高地" in lines[0], "E6 网页角色卡快照带修正说明", lines)

# 清掉临时文件里挂过的东西，避免影响别的脚本（本脚本独立进程，仅示意）
clear_mods(loop4.find_unit("丙"))

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
