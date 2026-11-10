# coding=utf-8
"""Batch 3 回归：技能熟练 / 戏法成长 / damage_roll 语境 / 升环加值 / 专注 DC。"""

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
from engine.rules.adjudicate import (  # noqa: E402
    concentration_check, damage_concentration_line, skill_check,
)
from engine.rules.loop import make_hero_unit  # noqa: E402
from engine.rules.spells import get_spell  # noqa: E402
from engine.rules.unit import DamagePacket, Unit  # noqa: E402
from engine.state import StateTree, new_character  # noqa: E402
from engine.state.world import new_world  # noqa: E402

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


AB = {"力量": 10, "敏捷": 16, "体质": 12, "智力": 14, "感知": 12, "魅力": 10}

# --------------------------------------------------------------------------- #
section("B3-1 技能熟练：卡上的「熟练/专精」要算成加值")

st = StateTree.from_template(
    {"游荡者": new_character("游荡者", level=5, job="游荡者", abilities=dict(AB),
                            hp_max=30, ac=14)},
    current="游荡者", world=new_world(date="1492-09-17", period="午后"))
st.commit([{"op": "replace", "path": "/角色列表/游荡者/熟练配置/技能",
            "value": {"隐匿": "熟练", "巧手": "专精"}}],
          actor="填表", reason="熟练配置")
unit = make_hero_unit(st, "游荡者")
ok(unit.skill_bonuses.get("stealth") == 6,
   "B3-1a ⭐ 隐匿 = 敏捷 +3 + 熟练 +3（5 级）= +6", unit.skill_bonuses)
ok(unit.skill_bonuses.get("sleight_of_hand") == 9,
   "B3-1b 巧手专精 = +3 + 2×3 = +9", unit.skill_bonuses)
chk = skill_check(unit, "stealth", 15, Random(3))
ok(chk.check.bonus == 6, "B3-1c 技能检定真的用上了熟练加值", chk.check.bonus)


# --------------------------------------------------------------------------- #
section("B3-2 戏法成长：5/11/17 级要变大")

fire = get_spell("Fire Bolt")
ok(fire.damage_at(0, 1) == "1d10", "B3-2a 1 级：1d10", fire.damage_at(0, 1))
ok(fire.damage_at(0, 5) == "2d10", "B3-2b ⭐ 5 级：2d10", fire.damage_at(0, 5))
ok(fire.damage_at(0, 11) == "3d10", "B3-2c 11 级：3d10", fire.damage_at(0, 11))


# --------------------------------------------------------------------------- #
section("B3-3 只有写着 damage 的骰子才是伤害")

ok(get_spell("Guidance").damage_roll == "",
   "B3-3a ⭐ 神导术不再是伤害法术", get_spell("Guidance").damage_roll)
false_life = get_spell("False Life")
ok(false_life.damage_roll == "" and false_life.temp_hp_roll == "2d4+4",
   "B3-3b ⭐ 虚假生命：临时生命 2d4+4，伤害为空",
   (false_life.damage_roll, false_life.temp_hp_roll))
regen = get_spell("Regenerate")
ok(regen.damage_roll == "" and regen.heal_roll == "4d8+15",
   "B3-3c 再生术：治疗 4d8+15，伤害为空",
   (regen.damage_roll, regen.heal_roll))
prayer = get_spell("Prayer of Healing")
ok(prayer.heal_roll == "2d8" and prayer.damage_roll == "",
   "B3-3d 治疗祷言：regain 2d8 的句式也认", prayer.heal_roll)


# --------------------------------------------------------------------------- #
section("B3-4 升环保留固定加值")

dis = get_spell("Disintegrate")
ok(dis.damage_at(7) == "13d6+40",
   "B3-4a ⭐ 解离术 7 环 = 13d6+40（不再丢 +40）", dis.damage_at(7))
ok(dis.damage_at(9) == "19d6+40", "B3-4b 9 环同理", dis.damage_at(9))


# --------------------------------------------------------------------------- #
section("B3-5 专注 DC：按减免后伤害、上限 30")

u = Unit(id="C", name="专注者", hp=100, hp_max=100, team="pc")
u.immunities = {"fire"}
u.concentrating_on = "祝福术"
dr = u.take_damage([DamagePacket(50, "fire")])
line = damage_concentration_line(u, 50, Random(3), dr)
ok(line == "" and u.concentrating_on == "祝福术",
   "B3-5a ⭐ 火焰免疫：0 伤害不掷专注", (line, u.concentrating_on))

u2 = Unit(id="C2", name="专注者二", hp=100, hp_max=100, team="pc")
ok(concentration_check(u2, 100, Random(3)).check.dc == 30,
   "B3-5b ⭐ 100 点伤害：DC 封顶 30",
   concentration_check(u2, 100, Random(3)).check.dc)
ok(concentration_check(u2, 20, Random(3)).check.dc == 10,
   "B3-5c 20 点伤害：DC = 10", concentration_check(u2, 20, Random(3)).check.dc)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")