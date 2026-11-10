# coding=utf-8
"""Batch 1（两个 P0）回归：治疗倒地队友 / 击杀专注者不炸轮。"""

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
from engine.models.client import Intent  # noqa: E402
from engine.rules.adjudicate import (  # noqa: E402
    cast_spell, damage_concentration_line,
)
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.spells import get_spell  # noqa: E402
from engine.rules.turn import execute_intent  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402

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


def adventurer(uid: str, name: str, hp: int, team: str = "pc") -> Unit:
    return Unit(id=uid, name=name, hp=hp, hp_max=20, team=team,
                ac=12, position=(0.0, 0.0))


# --------------------------------------------------------------------------- #
section("P0-1 治疗/救起 0 HP 的队友")

caster = adventurer("C", "牧师", 18)
caster.spells = [get_spell("Cure Wounds")]
caster.spell_slots[1] = {"current": 2, "max": 2}
dying = adventurer("A", "艾尔玛", 0)
foe = adventurer("E", "强盗", 11, team="enemy")

menu = build_menu(caster, [caster, dying, foe], include_friendly_targets=True)
heal_ops = [o for o in menu.options
            if o.kind == "spell" and "Cure Wounds" in o.name]
ok(bool(heal_ops), "B1a 治疗术出现在菜单里", [o.name for o in menu.options])
opts = heal_ops[0] if heal_ops else None
ok(opts is not None and "A" in opts.target_ids,
   "B1b ⭐ 0 HP 的队友在治疗候选里（原来被 alive 过滤掉）",
   None if opts is None else opts.target_ids)

res = cast_spell(caster, get_spell("Cure Wounds"), [dying], Random(7))
ok(res.ok and dying.hp > 0, "B1c ⭐ 对 0 HP 队友施治疗术：成功且回血",
   (res.ok, res.blocked_reason, dying.hp))
ok(caster.spell_slots[1]["current"] == 1, "B1d 法术位正常扣 1",
   caster.spell_slots[1])

# 满血队友照常可治（没有回归）
full = adventurer("F", "罗恩", 20)
healer2 = adventurer("C3", "牧师二号", 18)
healer2.spells = [get_spell("Cure Wounds")]
healer2.spell_slots[1] = {"current": 1, "max": 1}
res2 = cast_spell(healer2, get_spell("Cure Wounds"), [full], Random(7))
ok(res2.ok and full.hp == 20, "B1e 满血队友仍可施法（不超上限）",
   (res2.ok, res2.blocked_reason, full.hp))

# 伤害法术仍然不能把倒地者当目标
caster2 = adventurer("C2", "法师", 18)
caster2.spells = [get_spell("Magic Missile")]
caster2.spell_slots[1] = {"current": 1, "max": 1}
dead_target = adventurer("D", "倒下的敌人", 0, team="enemy")
res3 = cast_spell(caster2, get_spell("Magic Missile"), [dead_target], Random(7))
ok(not res3.ok, "B1f ⭐ 伤害法术仍拒绝 0 HP 目标（没有顺手放宽）",
   res3.blocked_reason)


# --------------------------------------------------------------------------- #
section("P0-2 击杀专注者：不抛异常、专注断掉")

killer = adventurer("K", "杀手", 30)
killer.actions.append(Action(
    name="砍", slot="action",
    attacks=[Attack(name="剑", attack_bonus=50, damage_notation="2d6+10",
                    damage_type="slashing", reach=5)]))
victim = adventurer("V", "专注者", 6, team="enemy")
victim.concentrating_on = "祝福术"
menu2 = build_menu(killer, [killer, victim])
atk = next(o for o in menu2.options if o.kind == "attack")
out = execute_intent(killer, Intent(unit="K", option_id=atk.id, target="V"),
                     menu2, [killer, victim], Random(7))
ok(out.ok, "B2a 致死一击正常结算（不抛异常）", out.blocked)
ok(bool(out.attacks), "B2b ⭐ 这次攻击留在结果里（没有被吞）", len(out.attacks))
ok(victim.hp == 0 and victim.concentrating_on == "",
   "B2c ⭐ 倒地即断专注（不再掷一个必抛的豁免）",
   (victim.hp, victim.concentrating_on))

# AoE/豁免路径：击杀专注者也不炸轮
mage = adventurer("M", "火法", 30)
mage.spells = [get_spell("Fireball")]
mage.spell_slots[3] = {"current": 1, "max": 1}
victim2 = adventurer("V2", "专注者二号", 3, team="enemy")
victim2.concentrating_on = "祝福术"
res = cast_spell(mage, get_spell("Fireball"), [victim2], Random(7))
ok(res.ok and victim2.hp == 0, "B2d ⭐ 法术击杀专注者：正常返回、HP 归零",
   (res.ok, res.blocked_reason, victim2.hp))
ok(victim2.concentrating_on == "", "B2e 专注也被清掉", victim2.concentrating_on)

# 直接调用：倒地目标不掷骰
dead = adventurer("X", "已倒地", 0)
dead.concentrating_on = "加速术"
line = damage_concentration_line(dead, 50, Random(3))
ok(line != "" and dead.concentrating_on == "",
   "B2f 受伤函数对倒地目标只断专注、不掷骰", line)

# 活着的专注者仍走正常检定（失败才断）
alive = adventurer("Y", "活着", 40)
alive.concentrating_on = "加速术"
line2 = damage_concentration_line(alive, 5, Random(3))
ok((line2 == "" and alive.concentrating_on == "加速术")
   or (line2 != "" and alive.concentrating_on == ""),
   "B2g 活着的目标仍按检定决定是否中断", line2)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")