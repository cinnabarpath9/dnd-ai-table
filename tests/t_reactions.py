# coding=utf-8
"""U5 回归：Ready / 反应法术的触发。"""

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
from engine.models import FakeModel, Intent, ModelReply  # noqa: E402
from engine.models.client import _pick_option  # noqa: E402
from engine.persist import (  # noqa: E402
    SaveError, load_game, save_game, unit_from_dict, unit_to_dict,
)
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.reactions import (  # noqa: E402
    ReadyTrigger, consume_ready, get_ready, ready_weapon, shield_spell,
)
from engine.rules.spells import get_spell  # noqa: E402
from engine.rules.turn import (  # noqa: E402
    execute_intent, move_unit, _maybe_shield,
)
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


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


AB = {"力量": 16, "敏捷": 14, "体质": 15,
      "智力": 10, "感知": 12, "魅力": 10}


def fighter(uid="A", name="战士", pos=(0.0, 0.0), *, team="pc",
            bonus=5, hp=60, **kw) -> Unit:
    u = Unit(id=uid, name=name, team=team, level=3, ac=16, hp_max=hp, hp=hp,
             abilities=AB, speeds={"walk": 30}, position=pos)
    u.actions.append(Action(
        name="攻击（长剑）", slot="action",
        attacks=[Attack(name="Longsword", attack_bonus=bonus,
                        damage_notation="1d8+3", damage_type="slashing",
                        reach=5, is_melee=True)]))
    for k, v in kw.items():
        setattr(u, k, v)
    return u


def shield_wizard(uid="W1", pos=(0.0, 0.0), *, slots=1, **kw) -> Unit:
    u = Unit(id=uid, name="罗恩", team="pc", level=3, ac=12, hp_max=40, hp=40,
             abilities={**AB, "智力": 16}, speeds={"walk": 30}, position=pos,
             cast_ability="intelligence",
             spell_slots={1: {"current": slots, "max": max(slots, 1)}})
    u.spells.append(get_spell("Shield"))
    u.actions.append(Action(
        name="攻击（匕首）", slot="action",
        attacks=[Attack(name="Dagger", attack_bonus=5,
                        damage_notation="1d4+3", damage_type="piercing",
                        reach=5, is_melee=True)]))
    for k, v in kw.items():
        setattr(u, k, v)
    return u


def ready(u: Unit, condition="enters_reach", action_index=0, attack_index=0):
    u.reactions.append(ReadyTrigger(condition=condition,
                                    action_index=action_index,
                                    attack_index=attack_index,
                                    label="Longsword"))


def attack_option(menu):
    return next(o for o in menu.options if o.kind == "attack")


# --------------------------------------------------------------------------- #
section("A. 准备动作的菜单与声明")

a = fighter("A", "甲", (0, 0))
b = fighter("B", "乙", (25, 0), team="enemy")
menu = build_menu(a, [a, b])
rd_opts = [o for o in menu.options if o.kind == "ready_attack"]
ok(len(rd_opts) == 2, "A1 一把武器 → 两个触发条件各一条", len(rd_opts))
ok({o.ready_condition for o in rd_opts} == {"enters_reach", "attacked"},
   "A2 条件枚举", [o.ready_condition for o in rd_opts])
ok(all(o.requires_target is False for o in rd_opts), "A3 声明时不需要目标")
ok(any("进入触及" in o.summary for o in rd_opts), "A4 近战写「进入触及」",
   [o.summary for o in rd_opts])
ok(all(o.to_dict().get("condition") for o in rd_opts), "A5 to_dict 带条件")

r_opt = next(o for o in rd_opts if o.ready_condition == "enters_reach")
out = execute_intent(a, Intent(unit="A", option_id=r_opt.id), menu, [a, b], Random(1))
ok(out.ok and len(a.reactions) == 1, "A6 声明成功并登记触发", (out.ok, a.reactions))
trig = a.reactions[0]
ok(trig.condition == "enters_reach" and trig.action_index == 0,
   "A7 触发记录字段正确")
ok(not a.can_take("action")[0], "A8 准备占掉了动作槽")
ok(get_ready(a, "enters_reach") is trig and get_ready(a, "attacked") is None,
   "A9 按条件查得到")

# 下个回合开始清空
a.reset_turn()
ok(a.reactions == [] and get_ready(a, "enters_reach") is None,
   "A10 ⭐ 到自己的下个回合开始，准备动作失效")

# 没有攻击的单位 → 保留通用 Ready（交 DM）
empty = Unit(id="Z", name="没法术", team="pc", hp_max=10, hp=10)
menu0 = build_menu(empty, [empty, b])
ok(any(o.kind == "ready" for o in menu0.options), "A11 没有攻击 → 通用 Ready")


# --------------------------------------------------------------------------- #
section("B. 触发：敌人移动进入射程")

ready_unit = fighter("R1", "守备者", (0, 0), bonus=50)
mover = fighter("M1", "入侵者", (30, 0), team="enemy", hp=60)
ready(ready_unit, "enters_reach")
mv, opps = move_unit(mover, (5, 0), [ready_unit, mover], Random(3))
ok(len(mv.readied) == 1, "B1 ⭐ 进入触及 → 准备攻击触发", mv.readied)
ok(mover.hp < 60, "B2 触发攻击真的造成伤害", mover.hp)
ok(ready_unit.reaction_used, "B3 反应被消耗")
ok(ready_unit.reactions == [], "B4 触发后记录被消耗")
ok(mv.readied[0].kind == "ready" and "准备动作" in mv.readied[0].line(),
   "B5 结果标成准备动作", mv.readied[0].line())

# 停在 10 尺外 → 不触发
ready_unit2 = fighter("R2", "守备者2", (0, 0), bonus=50)
mover2 = fighter("M2", "入侵者2", (30, 0), team="enemy", hp=60)
ready(ready_unit2, "enters_reach")
mv2, _ = move_unit(mover2, (10, 0), [ready_unit2, mover2], Random(3))
ok(mv2.readied == [] and mover2.hp == 60, "B6 没进触及 → 不触发")
ok(len(ready_unit2.reactions) == 1, "B7 没触发就保留记录")

# 反应已用 → 不触发，但记录保留
ready_unit3 = fighter("R3", "守备者3", (0, 0), bonus=50)
mover3 = fighter("M3", "入侵者3", (30, 0), team="enemy", hp=60)
ready(ready_unit3, "enters_reach")
ready_unit3.spend("reaction")
mv3, _ = move_unit(mover3, (5, 0), [ready_unit3, mover3], Random(3))
ok(mv3.readied and mv3.readied[0].result is None, "B8 没反应 → 记一条说明不攻击",
   mv3.readied[0].note if mv3.readied else "")
ok(len(ready_unit3.reactions) == 1, "B9 没反应不消耗准备记录")


# --------------------------------------------------------------------------- #
section("C. 触发：敌人攻击我")

defender = fighter("D1", "防守者", (0, 0), bonus=50, hp=80)
attacker = fighter("A1", "攻击者", (5, 0), team="enemy", bonus=5, hp=80)
ready(defender, "attacked")
am = build_menu(attacker, [attacker, defender])
aopt = attack_option(am)
out = execute_intent(attacker, Intent(unit="A1", option_id=aopt.id, target="D1"),
                     am, [attacker, defender], Random(7))
ok(out.ok and defender.hp < 80, "C1 攻击正常结算", (out.ok, defender.hp))
ok(len(out.readied) == 1, "C2 ⭐ 被攻击触发准备反击", out.readied)
ok(attacker.hp < 80, "C3 反击造成伤害", attacker.hp)
ok(defender.reaction_used and defender.reactions == [],
   "C4 反应消耗、准备记录清掉")
ok(out.readied[0].kind == "ready", "C5 结果标成准备动作")

# 反应已用 → 不反击
d2 = fighter("D2", "防守者2", (0, 0), bonus=50, hp=80)
a2 = fighter("A2", "攻击者2", (5, 0), team="enemy", bonus=5, hp=80)
ready(d2, "attacked")
d2.spend("reaction")
am2 = build_menu(a2, [a2, d2])
out2 = execute_intent(a2, Intent(unit="A2", option_id=attack_option(am2).id,
                                 target="D2"),
                      am2, [a2, d2], Random(7))
ok(out2.readied == [] and a2.hp == 80, "C6 没反应 → 不反击")
ok(len(d2.reactions) == 1, "C7 没反应不消耗记录")

# 准备好的攻击够不着 → 不触发、不消耗
d3 = fighter("D3", "防守者3", (0, 0), bonus=50, hp=80)
a3 = fighter("A3", "远程攻击者", (40, 0), team="enemy", bonus=5, hp=80)
a3.actions[0].attacks[0].is_melee = False
a3.actions[0].attacks[0].reach = None
a3.actions[0].attacks[0].range_normal = 100
ready(d3, "attacked")
am3 = build_menu(a3, [a3, d3])
out3 = execute_intent(a3, Intent(unit="A3", option_id=attack_option(am3).id,
                                 target="D3"),
                      am3, [a3, d3], Random(7))
ok(out3.readied == [] and len(d3.reactions) == 1,
   "C8 准备好的是近战、但攻击者在 40 尺外 → 不触发也不消耗")


# --------------------------------------------------------------------------- #
section("D. 护盾术：只在能翻掉命中时才花法术位")

wiz = shield_wizard(slots=2)
ok(shield_spell(wiz) is not None, "D1 法术书里找得到 Shield")
wm = build_menu(wiz, [wiz])
ok(not any(o.kind == "spell" for o in wm.options),
   "D2 ⭐ 护盾术不在可选行动里")
ok(any("Shield" in x and "自动" in x for x in wm.reaction_notes),
   "D3 写进反应说明", wm.reaction_notes)

# 找一个"本来命中、加 5 AC 后变成未命中"的种子
found_flip = False
for seed in range(300):
    w = shield_wizard(slots=2)
    atk = fighter("H1", "攻击者", (5, 0), team="enemy", bonus=5, hp=60)
    field = [atk, w]
    m = build_menu(atk, field)
    o = attack_option(m)
    rr = execute_intent(atk, Intent(unit="H1", option_id=o.id, target="W1"),
                        m, field, Random(seed))
    if rr.attacks and rr.attacks[0].reaction_cast is not None:
        found_flip = True
        ar = rr.attacks[0]
        ok(ar.hit is False and ar.damage_total == 0,
           "D4 ⭐ 护盾术把这次命中翻成未命中")
        ok(w.hp == 40, "D5 没掉血", w.hp)
        ok(w.spell_slots[1]["current"] == 1, "D6 扣掉 1 环位",
           w.spell_slots[1])
        ok(w.reaction_used, "D7 消耗反应")
        ok(ar.reaction_cast is not None and "护盾术" in ar.reaction_note,
           "D8 留痕写明护盾术", ar.reaction_note)
        break
ok(found_flip, "D9 300 个种子里找到一次可翻掉的命中")

# 加 5 AC 也翻不掉 → 不浪费法术位
w2 = shield_wizard(slots=2)
atk2 = fighter("H2", "高手", (5, 0), team="enemy", bonus=30, hp=60)
m2 = build_menu(atk2, [atk2, w2])
rr2 = execute_intent(atk2, Intent(unit="H2", option_id=attack_option(m2).id,
                                 target="W1"), m2, [atk2, w2], Random(1))
ok(rr2.attacks[0].hit and rr2.attacks[0].reaction_cast is None,
   "D10 ⭐ 加了还是命中 → 不放护盾术")
ok(w2.spell_slots[1]["current"] == 2 and not w2.reaction_used,
   "D11 法术位与反应都没动")

# 天然 20 必中，护盾术翻不掉
found_nat20 = False
for seed in range(200):
    w3 = shield_wizard(slots=2)
    atk3 = fighter("H3", "攻击者", (5, 0), team="enemy", bonus=5, hp=60)
    m3 = build_menu(atk3, [atk3, w3])
    rr3 = execute_intent(atk3, Intent(unit="H3", option_id=attack_option(m3).id,
                                     target="W1"), m3, [atk3, w3], Random(seed))
    ar3 = rr3.attacks[0]
    if ar3.check.a.is_crit:
        found_nat20 = True
        ok(ar3.hit and ar3.reaction_cast is None,
           "D12 ⭐ 天然 20 不被护盾术翻掉")
        ok(w3.spell_slots[1]["current"] == 2, "D13 也不浪费法术位")
        break
ok(found_nat20, "D14 200 个种子里出现天然 20")

# 没有法术位 → 不放
w4 = shield_wizard(slots=0)
atk4 = fighter("H4", "攻击者", (5, 0), team="enemy", bonus=5, hp=60)
m4 = build_menu(atk4, [atk4, w4])
rr4 = execute_intent(atk4, Intent(unit="H4", option_id=attack_option(m4).id,
                                 target="W1"), m4, [atk4, w4], Random(3))
ok(rr4.attacks[0].hit and rr4.attacks[0].reaction_cast is None,
   "D15 没 1 环位 → 不放护盾术")
ok(any("没有 1 环位" in x for x in build_menu(w4, [w4]).reaction_notes),
   "D15b 菜单里写明没有 1 环位")

# 直接调用底层 resolve_attack 不挂反应（默认不加戏）
from engine.rules.adjudicate import resolve_attack  # noqa: E402
w5 = shield_wizard(slots=2)
atk5 = fighter("H5", "攻击者", (5, 0), team="enemy", bonus=30, hp=60)
ar5 = resolve_attack(atk5, w5, atk5.actions[0].attacks[0], Random(1), distance=5)
ok(ar5.reaction_cast is None and w5.spell_slots[1]["current"] == 2,
   "D16 底层 resolve_attack 默认不触发反应")

# 借机攻击也能被护盾术反应
found_opp = False
for seed in range(300):
    w6 = shield_wizard(slots=2, pos=(5, 0))
    foe6 = fighter("O1", "借机者", (0, 0), team="enemy", bonus=5, hp=60)
    mv6, opps6 = move_unit(w6, (30, 0), [w6, foe6], Random(seed))
    if opps6 and opps6[0].result is not None \
            and opps6[0].result.reaction_cast is not None:
        found_opp = True
        ar6 = opps6[0].result
        ok(ar6.hit is False and w6.hp == 40,
           "D17 ⭐ 借机攻击也能被护盾术翻掉")
        ok(w6.spell_slots[1]["current"] == 1 and w6.reaction_used,
           "D18 同样扣位 + 消耗反应")
        break
ok(found_opp, "D19 300 个种子里找到一次借机攻击被翻掉")


# --------------------------------------------------------------------------- #
section("E. 解析与选择：准备动作不是普通攻击")

m = None
p = fighter("P1", "甲", (0, 0))
e = fighter("E1", "乙", (5, 0), team="enemy")
m = build_menu(p, [p, e])
rd = next(o for o in m.options if o.kind == "ready_attack")
r = ModelReply(text="", data=[{"unit": "P1", "actions": [{"id": rd.id}]}])
from engine.models import parse_intent_reply  # noqa: E402
ints, probs = parse_intent_reply(r, {"P1": m})
ok(len(ints) == 1 and not probs, "E1 准备动作意图可解析", probs)

opts = [attack_option(m).to_dict(), rd.to_dict()]
pick = _pick_option(opts)
ok(pick.get("id") == attack_option(m).id, "E2 ⭐ 有攻击时不优先挑准备动作", pick)

ok("ready_attack" in {o.kind for o in m.options}, "E3 kind 是 ready_attack")


# --------------------------------------------------------------------------- #
section("F. 准备动作跟存档走")

u = fighter("S1", "存档战士", (0, 0))
ready(u, "attacked")
d = unit_to_dict(u)
u2 = unit_from_dict(json.loads(json.dumps(d, ensure_ascii=False)))
ok(len(u2.reactions) == 1 and u2.reactions[0].condition == "attacked",
   "F1 ⭐ 单位往返保留准备动作")
ok(unit_to_dict(u2) == d, "F2 再导出完全一致")
ok(raises(lambda: unit_from_dict({**d, "reactions": [{"condition": "看心情"}]}),
          SaveError), "F3 非法条件 → SaveError")
ok(raises(lambda: ReadyTrigger.from_dict({"condition": "attacked",
                                          "action_index": -1,
                                          "attack_index": 0}),
          ValueError), "F3b 非法索引 → ValueError（不默认取最后一个动作）")

st = StateTree.from_template({
    "罗恩": new_character("罗恩", level=3, job="战士", abilities=AB,
                          hp_max=40, ac=16),
}, current="罗恩")
led = Ledger()
loop = GameLoop(st, led, FakeModel(), rng=Random(5), label="反应存档")
hero_u = make_hero_unit(st, "罗恩")
hero_u.reactions.append(ReadyTrigger("attacked", 0, 0, "Longsword"))
loop.set_field([hero_u])
with tempfile.TemporaryDirectory(prefix="dnd-react-save-") as tmp:
    pth = os.path.join(tmp, "r.json")
    save_game(pth, state=st, ledger=led, loop=loop)
    sess = load_game(pth)
    got = [x for x in sess.loop.field if x.id == hero_u.id][0]
    ok(len(got.reactions) == 1 and got.reactions[0].condition == "attacked",
       "F4 ⭐ 整局读档后准备动作还在")
    ok(get_ready(got, "attacked") is not None, "F5 按条件仍查得到")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")
