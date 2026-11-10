# coding=utf-8
"""Batch F 回归：武器特性（双手 Versatile）与法术攻击近/远程。"""

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

from engine.rules.loop import make_hero_unit  # noqa: E402
from engine.rules.spells import get_spell  # noqa: E402
from engine.state import StateTree, new_character  # noqa: E402

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


AB = {"力量": 16, "敏捷": 14, "体质": 14, "智力": 16, "感知": 10, "魅力": 8}


def state_with(items: dict) -> StateTree:
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士", abilities=dict(AB),
                             hp_max=30, ac=10)}, current="甲")
    for name, item in items.items():
        st.commit([{"op": "insert", "path": f"/角色列表/甲/物品/{name}",
                    "value": item}], actor="测试", reason="造装备")
    return st


def attack_of(items: dict):
    u = make_hero_unit(state_with(items), "甲")
    return u.actions[0].attacks[0]


# --------------------------------------------------------------------------- #
section("F-1 Versatile：没盾用双手骰，有盾用单手骰")

a = attack_of({"长剑": {"类别": "武器", "已装备": True}})
ok("1d10" in a.damage_notation, "F-1a ⭐ 无盾长剑 = 1d10", a.damage_notation)
ok("versatile" in a.properties, "F-1b 属性里有 versatile", a.properties)

a = attack_of({"长剑": {"类别": "武器", "已装备": True},
               "盾牌": {"类别": "盾牌", "已装备": True}})
ok("1d8" in a.damage_notation and "1d10" not in a.damage_notation,
   "F-1c ⭐ 有盾长剑回 1d8", a.damage_notation)

a = attack_of({"巨剑": {"类别": "武器", "已装备": True}})
ok("2d6" in a.damage_notation, "F-1d 巨剑 2d6", a.damage_notation)

u = make_hero_unit(state_with(
    {"长剑": {"类别": "武器", "已装备": True},
     "盾牌": {"类别": "盾牌", "已装备": True}}), "甲")
ok(any("双手握持" in n for n in u.notes) is False,
   "F-1e 有盾时不提示双手握持", u.notes)


# --------------------------------------------------------------------------- #
section("F-2 法术攻击：近战/远程判定")

fb = get_spell("Fire Bolt")
ok(fb is not None and fb.attack_roll and not fb.is_melee_attack,
   "F-2a Fire Bolt 是远程法术攻击", (fb.range_text, fb.is_melee_attack))

iw = get_spell("Chill Touch")
ok(iw is not None and iw.attack_roll and iw.is_melee_attack,
   "F-2b ⭐ Chill Touch（Touch）是近战法术攻击",
   (iw.range_text, iw.is_melee_attack))

sc = get_spell("Shocking Grasp")
if sc is not None:
    ok(sc.attack_roll and sc.is_melee_attack, "F-2c Shocking Grasp 近战", sc.range_text)

save_spell = get_spell("Sacred Flame")
if save_spell is not None:
    ok(not save_spell.is_melee_attack and not save_spell.attack_roll,
       "F-2d 豁免法术不算法术攻击", save_spell.save_ability)


# --------------------------------------------------------------------------- #
section("F-3 属性/骰式没被写死")

a = attack_of({"匕首": {"类别": "武器", "已装备": True}})
ok(all(x in a.properties for x in ("finesse",)), "F-3a 匕首有 finesse", a.properties)
a = attack_of({"短弓": {"类别": "武器", "已装备": True}})
ok(not a.is_melee and a.range_normal, "F-3b 短弓是远程且有射程",
   (a.is_melee, a.range_normal))


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")