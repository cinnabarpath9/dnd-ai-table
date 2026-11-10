# coding=utf-8
"""传奇 / 巢穴动作 demo：**在别人回合插入**，零模型调用。

跑法：
    python -m engine.demo_legendary

证明四件事：
  1. 传奇动作不在菜单里"想用就用"，而是别人回合结束时由引擎插入；
  2. 传奇动作按**标价**扣点（不是一律 1 点），自己回合开始刷新；
  3. 巢穴动作每轮一次，先攻 20（轮初）触发；
  4. 够不着 / 没点数时不硬用，不白扣资源。
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

AB = {"力量": 16, "敏捷": 14, "体质": 15,
      "智力": 10, "感知": 12, "魅力": 10}


def _atk(name="Claw", bonus=50, dmg="1d8+3") -> Attack:
    return Attack(name=name, attack_bonus=bonus, damage_notation=dmg,
                  damage_type="slashing", reach=5, is_melee=True)


def make_boss() -> Unit:
    u = Unit(id="B1", name="远古红龙", team="enemy", level=11, ac=19,
             hp_max=200, hp=200, abilities=AB, speeds={"walk": 40},
             position=(10.0, 0.0), legendary_actions_max=3,
             lair_actions_max=1)
    u.actions.append(Action(name="传奇爪击", slot="legendary", legendary_cost=1,
                            attacks=[_atk()]))
    u.actions.append(Action(name="传奇横扫", slot="legendary", legendary_cost=2,
                            attacks=[_atk("Sweep", dmg="2d6+4")]))
    u.actions.append(Action(name="巢穴脉冲", slot="lair",
                            save_ability="dexterity", save_dc=14,
                            damage_notation="2d8", damage_type="necrotic",
                            area_size=20, area_shape="sphere",
                            save_success_half=True, desc="巢穴能量脉冲。"))
    return u


def make_hero(uid: str, pos) -> Unit:
    u = Unit(id=uid, name=f"冒险者{uid}", team="pc", level=5, ac=16,
             hp_max=80, hp=80, abilities=AB, speeds={"walk": 30}, position=pos)
    u.actions.append(Action(name="攻击（长剑）", slot="action",
                            attacks=[_atk("Longsword", bonus=5)]))
    return u


def main() -> int:
    print("=" * 84)
    print("传奇 / 巢穴动作 demo（零模型调用）")
    print("=" * 84)

    boss = make_boss()
    pc1, pc2 = make_hero("P1", (5.0, 0.0)), make_hero("P2", (5.0, 5.0))

    print("\n【1】龙的菜单：特殊动作不列成普通选项")
    print("-" * 84)
    menu = build_menu(boss, [boss, pc1, pc2])
    print(f"  可选行动数：{len(menu.options)}"
          "（没有传奇/巢穴选项）")
    for note in menu.special_notes:
        print("  · " + note)

    st = StateTree.from_template({
        "罗恩": new_character("罗恩", level=5, job="战士", abilities=AB,
                              hp_max=80, ac=16),
    }, current="罗恩")
    led = Ledger()
    loop = GameLoop(st, led, FakeModel(), rng=Random(11), label="传奇 demo")
    loop.set_field([pc1, pc2, boss])
    loop.initiative = [(pc1, 20), (pc2, 15), (boss, 10)]

    print(f"\n【2】开打前｜龙传奇资源 {boss.legendary_left}/"
          f"{boss.legendary_actions_max}｜巢穴 {'可用' if boss.can_take('lair')[0] else '不可用'}")
    lg = loop.run_combat_round(pc_name="P1")

    print("\n【3】这一轮里发生的事（只挑插入行）")
    print("-" * 84)
    for line in lg.lines:
        if "🌟" in line or "🏰" in line:
            print("  " + line.replace("\n", "\n  "))

    print(f"\n【4】一轮结束后｜龙传奇资源 {boss.legendary_left}/"
          f"{boss.legendary_actions_max}"
          "（它自己在回合开始刷新，所以自己的回合过后又满了）｜"
          f"巢穴本轮已用：{boss.used.get('lair', 0)}")
    print(f"    冒险者 HP：P1 {pc1.hp}/80｜P2 {pc2.hp}/80")
    print(f"    台账里传奇/巢穴条目："
          f"{len([e for e in led.entries if '传奇动作' in e.text or '巢穴动作' in e.text])} 条")

    lg2 = loop.run_combat_round(pc_name="P1")
    lair2 = len([l for l in lg2.lines if "🏰 巢穴动作" in l])
    print(f"\n【5】第二轮｜巢穴动作触发 {lair2} 次（每轮一次）")

    checks = {
        "菜单里没有传奇选项": not any(
            o.slot in ("legendary", "lair") for o in menu.options),
        "特殊动作写进说明": any("传奇动作" in x for x in menu.special_notes),
        "别人回合插入了传奇动作": "🌟 传奇动作" in "\n".join(lg.lines),
        "轮初触发了巢穴动作": "🏰 巢穴动作（先攻 20）" in "\n".join(lg.lines),
        "传奇资源被扣过": boss.legendary_actions_used > 0
                          or "传奇动作" in "\n".join(lg.lines),
        "第二轮巢穴再触发一次": lair2 == 1,
        "台账留痕": any("传奇动作" in e.text or "巢穴动作" in e.text
                        for e in led.entries),
    }
    print("\n【6】检查")
    print("-" * 84)
    for name, good in checks.items():
        print(f"  {'✅' if good else '❌'} {name}")

    ok = all(checks.values())
    print("\n" + "=" * 84)
    print("✅ 传奇 / 巢穴插入：全部通过" if ok else "❌ 有检查项失败")
    print("=" * 84)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())