# coding=utf-8
"""反应 demo：Ready（准备动作）与护盾术，**零模型调用**。

跑法：
    python -m engine.demo_reactions

证明三件事：
  1. Ready 不是"声明完就完"：敌人真的移动进入射程时，引擎会用反应打出去；
  2. 准备动作只活到自己的下个回合开始，触发一次就消耗；
  3. 护盾术不在可选行动里，只在"加了 5 AC 就能翻掉命中"时自动花 1 环位。
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
from engine.models import Intent  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.spells import get_spell  # noqa: E402
from engine.rules.turn import execute_intent, move_unit  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402

AB = {"力量": 16, "敏捷": 14, "体质": 15,
      "智力": 10, "感知": 12, "魅力": 10}


def fighter(uid: str, name: str, pos, *, team="pc", bonus=50, hp=60) -> Unit:
    u = Unit(id=uid, name=name, team=team, level=3, ac=16, hp_max=hp, hp=hp,
             abilities=AB, speeds={"walk": 30}, position=pos)
    u.actions.append(Action(
        name="攻击（长剑）", slot="action",
        attacks=[Attack(name="Longsword", attack_bonus=bonus,
                        damage_notation="1d8+3", damage_type="slashing",
                        reach=5, is_melee=True)]))
    return u


def wizard(uid: str, name: str, pos, *, slots=2) -> Unit:
    u = Unit(id=uid, name=name, team="pc", level=3, ac=12, hp_max=40, hp=40,
             abilities={**AB, "智力": 16}, speeds={"walk": 30}, position=pos,
             cast_ability="intelligence",
             spell_slots={1: {"current": slots, "max": max(slots, 1)}})
    u.spells.append(get_spell("Shield"))
    u.actions.append(Action(
        name="攻击（匕首）", slot="action",
        attacks=[Attack(name="Dagger", attack_bonus=5,
                        damage_notation="1d4+3", damage_type="piercing",
                        reach=5, is_melee=True)]))
    return u


def main() -> int:
    print("=" * 84)
    print("反应 demo（Ready / 护盾术，零模型调用）")
    print("=" * 84)

    # ---------- 1) Ready：敌人进入触及 ----------
    guard = fighter("G1", "守备者", (0, 0))
    raider = fighter("M1", "入侵者", (30, 0), team="enemy", hp=60)
    menu = build_menu(guard, [guard, raider])
    ready_opt = next(o for o in menu.options
                     if o.kind == "ready_attack"
                     and o.ready_condition == "enters_reach")
    out = execute_intent(guard, Intent(unit="G1", option_id=ready_opt.id),
                         menu, [guard, raider], Random(1))
    print("\n【1】Ready：声明")
    print("-" * 84)
    print("  " + out.summary().replace("\n", "\n  "))
    print(f"  触发记录：{guard.reactions[0].line()}")

    mv, opps = move_unit(raider, (5, 0), [guard, raider], Random(3))
    print("\n【2】入侵者移动进入触及")
    print("-" * 84)
    print("  " + mv.line())
    for rd in mv.readied:
        print("  " + rd.line().replace("\n", "\n  "))
    print(f"  入侵者 HP：{raider.hp}/60｜守备者反应已用：{guard.reaction_used}"
          f"｜准备记录剩余：{len(guard.reactions)}")

    # ---------- 2) 护盾术 ----------
    print("\n【3】护盾术：被命中时自动判断")
    print("-" * 84)
    flipped = None
    for seed in range(300):
        wiz = wizard("W1", "罗恩", (0, 0))
        foe = fighter("E1", "强盗", (5, 0), team="enemy", bonus=5, hp=60)
        field = [wiz, foe]
        m = build_menu(foe, field)
        atk = next(o for o in m.options if o.kind == "attack")
        r = execute_intent(foe, Intent(unit="E1", option_id=atk.id, target="W1"),
                           m, field, Random(seed))
        ar = r.attacks[0]
        if ar.reaction_cast is not None:
            flipped = (seed, ar, wiz)
            break
    if flipped is None:
        print("  ❌ 300 个种子都没找到可翻掉的命中（不正常）")
        return 1
    seed, ar, wiz = flipped
    print(f"  种子 {seed}｜" + ar.summary().replace("\n", "\n  "))
    print(f"  罗恩 HP：{wiz.hp}/40｜1 环位：{wiz.spell_slots[1]['current']}/"
          f"{wiz.spell_slots[1]['max']}｜反应已用：{wiz.reaction_used}")
    print("  （护盾术不在可选行动里，只在能把命中翻成未命中时才触发）")

    print("\n【4】口径")
    print("-" * 84)
    print("  · Ready 只活到自己的下个回合开始；触发一次就消耗；")
    print("  · 护盾术不保留持续的 +5 AC（第一版只翻掉触发它的那一次命中）；")
    print("  · 一次反应只能干一件事：护盾术用掉反应后，Ready 不再触发。")

    ok = (len(mv.readied) == 1 and raider.hp < 60
          and guard.reaction_used and guard.reactions == []
          and ar.hit is False and wiz.hp == 40
          and wiz.spell_slots[1]["current"] == 1 and wiz.reaction_used)
    print("\n" + "=" * 84)
    print("✅ 反应链路：全部通过" if ok else "❌ 有检查项失败")
    print("=" * 84)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
