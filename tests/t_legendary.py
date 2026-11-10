# coding=utf-8
"""U6 回归：传奇动作 / 巢穴动作的插入。"""

from __future__ import annotations

import json
import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.persist import load_game, save_game, unit_from_dict, unit_to_dict  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.turn import execute_special_action  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

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


AB = {"力量": 16, "敏捷": 14, "体质": 15,
      "智力": 10, "感知": 12, "魅力": 10}


def _atk(name="Claw", bonus=50, dmg="1d8+3", dtype="slashing") -> Attack:
    return Attack(name=name, attack_bonus=bonus, damage_notation=dmg,
                  damage_type=dtype, reach=5, is_melee=True)


def boss(uid="B1", pos=(10.0, 0.0), *, team="enemy", legendary=3,
         lair=0, hp=200) -> Unit:
    u = Unit(id=uid, name="远古红龙", team=team, level=11, ac=19,
             hp_max=hp, hp=hp, abilities=AB, speeds={"walk": 40},
             position=pos, legendary_actions_max=legendary,
             lair_actions_max=lair)
    u.actions.append(Action(name="传奇爪击", slot="legendary", legendary_cost=1,
                            attacks=[_atk()]))
    u.actions.append(Action(name="传奇横扫", slot="legendary", legendary_cost=2,
                            attacks=[_atk("Sweep", dmg="2d6+4")]))
    u.actions.append(Action(name="传奇咆哮", slot="legendary", legendary_cost=1,
                            save_ability="wisdom", save_dc=15,
                            damage_notation="2d6", damage_type="psychic",
                            area_size=30, area_shape="emanation",
                            save_success_half=True, desc="威吓咆哮。"))
    u.actions.append(Action(name="纯描述传奇", slot="legendary",
                            legendary_cost=1, desc="飞走一段距离（文本）。"))
    if lair:
        u.actions.append(Action(name="巢穴脉冲", slot="lair",
                                save_ability="dexterity", save_dc=14,
                                damage_notation="2d8", damage_type="necrotic",
                                area_size=20, area_shape="sphere",
                                save_success_half=True, desc="巢穴能量脉冲。"))
    return u


def hero(uid="P1", pos=(5.0, 0.0), hp=80) -> Unit:
    u = Unit(id=uid, name=f"冒险者{uid}", team="pc", level=5, ac=16,
             hp_max=hp, hp=hp, abilities=AB, speeds={"walk": 30},
             position=pos)
    u.actions.append(Action(name="攻击（长剑）", slot="action",
                            attacks=[_atk("Longsword", bonus=5)]))
    return u


# --------------------------------------------------------------------------- #
section("A. 资源口径：传奇按点、巢穴每轮一次")

b = boss()
ok(b.legendary_left == 3 and b.can_take("legendary")[0], "A1 3 点起手")
b.spend_legendary(2)
ok(b.legendary_left == 1 and b.can_take("legendary")[0], "A2 扣 2 点还剩 1 点")
b.spend_legendary(1)
ok(b.legendary_left == 0 and b.can_take("legendary")[0] is False,
   "A3 扣光后不可用")
b.reset_turn()
ok(b.legendary_left == 3, "A4 ⭐ 自己回合开始刷新传奇资源")

b2 = boss(lair=1)
ok(b2.can_take("lair")[0], "A5 有巢穴动作时可用")
b2.spend("lair")
ok(b2.can_take("lair")[0] is False, "A6 巢穴动作本轮用完")
b2.reset_turn()
ok(b2.can_take("lair")[0] is False,
   "A7 ⭐ reset_turn **不**刷新巢穴（它只在自己回合开始）")
b2.reset_round()
ok(b2.can_take("lair")[0], "A8 ⭐ reset_round 刷新巢穴")

ok(Unit(id="x", name="无巢穴").can_take("lair")[0] is False,
   "A9 没有巢穴动作的单位不可用")
ok(b2.spend("lair") is None and b2.can_take("lair")[0] is False,
   "A10 spend 后再查是假的")


# --------------------------------------------------------------------------- #
section("B. 菜单：特殊动作不列成普通选项")

m = build_menu(boss(), [boss(), hero()])
ok(not any(o.slot in ("legendary", "lair") for o in m.options),
   "B1 ⭐ 传奇/巢穴动作不在可选行动里",
   [(o.name, o.slot) for o in m.options])
ok(any("传奇动作 3/3" in x for x in m.special_notes),
   "B2 写进特殊动作说明", m.special_notes)
ok(m.to_dict().get("special_notes"), "B3 to_dict 带 special_notes")
ok("特殊动作（引擎按触发时机插入）" in m.render(), "B4 渲染有特殊动作行")

ml = build_menu(boss(lair=1), [boss(lair=1), hero()])
ok(any("巢穴动作" in x for x in ml.special_notes), "B5 巢穴动作也写进说明")
mh = build_menu(hero(), [hero(), boss()])
ok(mh.special_notes == [], "B6 普通冒险者没有特殊动作说明")


# --------------------------------------------------------------------------- #
section("C. 单次执行：攻击 / 豁免 / 描述型 / 资源")

field = [boss(), hero()]
bu, pc = field
act_claw = bu.actions[0]
out = execute_special_action(bu, act_claw, field, Random(1))
ok(out.ok and out.kind == "legendary", "C1 传奇攻击执行成功", out.blocked)
ok(pc.hp < 80 and len(out.attacks) == 1, "C2 造成伤害", pc.hp)
ok(bu.legendary_actions_used == 1, "C3 扣 1 点")

bu2, pc2 = boss(uid="B2"), hero(uid="P2")
out2 = execute_special_action(bu2, bu2.actions[1], [bu2, pc2], Random(1))
ok(out2.ok and bu2.legendary_actions_used == 2,
   "C4 ⭐ 标价 2 的动作扣 2 点", bu2.legendary_actions_used)

bu3, pc3 = boss(uid="B3", pos=(200.0, 0.0)), hero(uid="P3", pos=(0.0, 0.0))
out3 = execute_special_action(bu3, bu3.actions[0], [bu3, pc3], Random(1))
ok(not out3.ok and "没有目标" in out3.blocked
   and bu3.legendary_actions_used == 0,
   "C5 ⭐ 够不着 → 拒绝且不扣资源", (out3.blocked, bu3.legendary_actions_used))

bu4, pc4 = boss(uid="B4"), hero(uid="P4")
bu4.legendary_actions_used = 3
out4 = execute_special_action(bu4, bu4.actions[0], [bu4, pc4], Random(1))
ok(not out4.ok and "已用完" in out4.blocked, "C6 资源用完 → 拒绝", out4.blocked)

bu5, pc5 = boss(uid="B5"), hero(uid="P5", hp=200)
out5 = execute_special_action(bu5, bu5.actions[2], [bu5, pc5], Random(3))
ok(out5.ok and out5.save_results and pc5.hp < 200,
   "C7 传奇豁免型动作走豁免结算", (out5.ok, pc5.hp))
ok(bu5.legendary_actions_used == 1, "C8 豁免型也扣点")

bu6, pc6 = boss(uid="B6"), hero(uid="P6")
out6 = execute_special_action(bu6, bu6.actions[3], [bu6, pc6], Random(1))
ok(out6.ok and out6.needs_dm_ruling and bu6.legendary_actions_used == 1,
   "C9 ⭐ 纯描述型：声明成功、扣点、标「需 DM 裁定」")

bu7, pc7 = boss(uid="B7", lair=1), hero(uid="P7")
lair_act = bu7.actions[-1]
out7 = execute_special_action(bu7, lair_act, [bu7, pc7], Random(2))
ok(out7.ok and out7.kind == "lair" and bu7.used.get("lair") == 1,
   "C10 巢穴动作执行并记账")
out7b = execute_special_action(bu7, lair_act, [bu7, pc7], Random(2))
ok(not out7b.ok and "已用过" in out7b.blocked, "C11 同轮第二次被拒", out7b.blocked)

dead = boss(uid="B8", hp=0)
ok(not execute_special_action(dead, dead.actions[0], [dead], Random(1)).ok,
   "C12 倒地不能用特殊动作")


# --------------------------------------------------------------------------- #
section("D. GameLoop：在别人回合插入、每轮先攻 20 触发巢穴")


def make_state() -> StateTree:
    st = StateTree.from_template({
        "罗恩": new_character("罗恩", level=5, job="战士", abilities=AB,
                              hp_max=80, ac=16),
    }, current="罗恩")
    st.commit([{"op": "insert", "path": "/角色列表/罗恩/物品/长剑",
                "value": {"类别": "武器", "已装备": True}}],
              actor="填表", reason="装备长剑")
    return st


def make_loop(boss_unit: Unit, pcs: list[Unit]) -> tuple[GameLoop, Ledger]:
    st = make_state()
    led = Ledger()
    loop = GameLoop(st, led, FakeModel(), rng=Random(11), label="传奇测试")
    loop.set_field(pcs + [boss_unit])
    loop.initiative = [(u, 20 - i) for i, u in enumerate(pcs + [boss_unit])]
    return loop, led


b_loop, led = make_loop(boss(uid="B1", pos=(10.0, 0.0)), [hero("P1", (5.0, 0.0))])
lg = b_loop.run_combat_round(pc_name="P1")
text = "\n".join(lg.lines)
ok("🌟 传奇动作" in text, "D1 ⭐ 别人回合结束时插入传奇动作", text[-300:])
ok(b_loop.field[1].legendary_actions_used >= 1 or
   any("传奇动作" in e.text for e in led.entries),
   "D2 传奇资源被扣 / 写进台账")
ok(any("传奇动作" in e.text for e in led.entries), "D3 台账有传奇动作条目")

# 两个 PC → 两个插入窗口（资源 3 点，够用）
b2_loop, led2 = make_loop(boss(uid="B2", pos=(10.0, 0.0)),
                          [hero("P1", (5.0, 0.0)), hero("P2", (5.0, 5.0))])
lg2 = b2_loop.run_combat_round(pc_name="P1")
n_leg = sum(1 for l in lg2.lines if "🌟 传奇动作" in l)
ok(n_leg == 2, f"两个 PC 回合 → {n_leg} 次传奇插入（应为 2）")

# 只给 1 点 → 最多插入一次
b3_loop, _ = make_loop(boss(uid="B3", pos=(10.0, 0.0), legendary=1),
                       [hero("P1", (5.0, 0.0)), hero("P2", (5.0, 5.0))])
lg3 = b3_loop.run_combat_round(pc_name="P1")
n3 = sum(1 for l in lg3.lines if "🌟 传奇动作" in l)
ok(n3 == 1, f"1 点资源只插入 {n3} 次")

# 巢穴动作：每轮先攻 20 触发一次，第二轮还能再触发
b4_loop, led4 = make_loop(boss(uid="B4", pos=(10.0, 0.0), lair=1),
                          [hero("P1", (5.0, 0.0))])
lg4 = b4_loop.run_combat_round(pc_name="P1")
ok("🏰 巢穴动作（先攻 20）" in "\n".join(lg4.lines), "D4 ⭐ 巢穴动作在轮初触发")
lg5 = b4_loop.run_combat_round(pc_name="P1")
n_lair = sum(1 for l in lg5.lines if "🏰 巢穴动作" in l)
ok(n_lair == 1, "D5 ⭐ 第二轮巢穴动作又能触发一次（每轮一次）")
ok(any("巢穴动作" in e.text for e in led4.entries), "D6 台账有巢穴动作")

# 没有传奇/巢穴的单位：一轮里不出现任何插入
plain_loop, _ = make_loop(hero("E9", (10.0, 0.0), hp=50), [hero("P1", (5.0, 0.0))])
plain_loop.field[0].team = "enemy"
plain_loop.field[0].legendary_actions_max = 0
lg6 = plain_loop.run_combat_round(pc_name="P1")
ok("🌟" not in "\n".join(lg6.lines) and "🏰" not in "\n".join(lg6.lines),
   "D7 普通单位不产生插入")


# --------------------------------------------------------------------------- #
section("E. 本地策略与存档")

bb = boss(uid="B1")
strategy_loop = GameLoop(make_state(), Ledger(), FakeModel(), rng=Random(1))
strategy_loop.set_field([bb, hero("P1", (5.0, 0.0))])
pick = strategy_loop._local_special_action(bb, "legendary")
ok(pick is not None and pick.name in ("传奇爪击", "传奇横扫", "传奇咆哮",
                                      "纯描述传奇"),
   "E1 有目标时优先挑能打到的动作", getattr(pick, "name", None))
ok(strategy_loop._local_special_action(bb, "lair") is None,
   "E2 没有巢穴动作时挑不出来")

far_bb = boss(uid="B2", pos=(500.0, 0.0))
far_loop = GameLoop(make_state(), Ledger(), FakeModel(), rng=Random(1))
far_loop.set_field([far_bb, hero("P1", (0.0, 0.0))])
far_pick = far_loop._local_special_action(far_bb, "legendary")
ok(far_pick is not None and far_pick.save_ability == "wisdom",
   "E3 攻击够不着时退而挑豁免型（AoE 不需要贴脸）",
   getattr(far_pick, "name", None))

weak = boss(uid="B3", legendary=1)
weak.spend_legendary(1)
weak_loop = GameLoop(make_state(), Ledger(), FakeModel(), rng=Random(1))
weak_loop.set_field([weak, hero("P1", (5.0, 0.0))])
ok(weak_loop._local_special_action(weak, "legendary") is None,
   "E4 没点数时不挑标价 1 的动作")

d = unit_to_dict(boss(uid="B5", lair=1))
b5 = unit_from_dict(json.loads(json.dumps(d, ensure_ascii=False)))
ok(b5.lair_actions_max == 1 and unit_to_dict(b5) == d,
   "E5 ⭐ 巢穴字段随单位往返")

st = make_state()
led7 = Ledger()
loop7 = GameLoop(st, led7, FakeModel(), rng=Random(5))
u7 = make_hero_unit(st, "罗恩")
u7.legendary_actions_max = 3
u7.legendary_actions_used = 1
u7.lair_actions_max = 1
loop7.set_field([u7])
with tempfile.TemporaryDirectory(prefix="dnd-legendary-save-") as tmp:
    path = os.path.join(tmp, "l.json")
    save_game(path, state=st, ledger=led7, loop=loop7)
    sess = load_game(path)
    got = sess.loop.field[0]
    ok(got.legendary_actions_max == 3 and got.legendary_actions_used == 1
       and got.lair_actions_max == 1,
       "E6 ⭐ 整局读档后特殊动作资源还在",
       (got.legendary_actions_max, got.legendary_actions_used,
        got.lair_actions_max))


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")