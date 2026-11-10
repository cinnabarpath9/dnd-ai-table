# coding=utf-8
"""P5-F2 回归：NPC 事件驱动（挨打/被欺骗/被帮助/同伴倒下）。

守四件事：

    A. **事件规则**：挨打按血量转恐惧/愤怒、被骗转猜忌且谎言进「误解」、
       被帮助转感激、同伴倒下转愤怒/恐惧；不认识的 kind/缺字段直接报错；
    B. **写回世界树**：事件改的是已登记 NPC 的后台状态，走 commit（有审计）；
    C. **防全知**：状态变化只进 DM 台账，玩家看不到；
    D. **战斗接线**：攻击命中已登记 NPC 会自动触发挨打事件。
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, Intent  # noqa: E402
from engine.rules.loop import GameLoop, RoundLog, make_hero_unit  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.npc_events import apply_event  # noqa: E402
from engine.rules.persona import NpcState, roll_persona  # noqa: E402
from engine.rules.turn import execute_intent  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402
from engine.state import Ledger, StateTree, World, new_character  # noqa: E402

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


AB = {"力量": 16, "敏捷": 14, "体质": 14, "智力": 10, "感知": 12, "魅力": 10}


def make_state():
    return StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士", abilities=AB,
                             hp_max=24, ac=15)}, current="甲")


# --------------------------------------------------------------------------- #
section("A. 事件规则：四种 kind 的状态变化")

st, notes = apply_event({}, {"kind": "damage", "amount": 10,
                             "source": "艾尔玛", "hp_ratio": 0.5})
ok(st["情绪"] == "愤怒" and st["强度"] == 50,
   "A1 半血挨打 → 愤怒(50)", (st["情绪"], st["强度"]))
ok(st["印象"]["艾尔玛"] == "敌意", "A2 对打人者记敌意", st["印象"])
ok(any(x["fact"] == "艾尔玛 攻击了我" and x["state"] == "已知"
       for x in st["知情"]), "A3 知情记了「谁打了我」")
ok(st["短期目标"] and st["记忆"], "A4 默认短期目标与记忆")

st2, _ = apply_event({}, {"kind": "damage", "amount": 20,
                          "source": "罗恩", "hp_ratio": 0.2})
ok(st2["情绪"] == "恐惧", "A5 ⭐ 残血挨打 → 恐惧", st2["情绪"])

st3, _ = apply_event({}, {"kind": "deceived", "by": "小翠",
                          "lie": "箱子是空的", "truth": "箱子里有账本"})
ok(st3["情绪"] == "猜忌" and st3["强度"] >= 60, "A6 被骗 → 猜忌(>=60)")
ok(st3["印象"]["小翠"] == "不可信", "A7 对说谎者记不可信")
lie = [x for x in st3["知情"] if x["fact"] == "箱子是空的"]
truth = [x for x in st3["知情"] if x["fact"] == "箱子里有账本"]
ok(lie and lie[0]["state"] == "误解", "A8 ⭐ 谎言进「误解」", lie)
ok(truth and truth[0]["state"] == "已知", "A9 真相进「已知」", truth)

st4, _ = apply_event({}, {"kind": "helped", "by": "布兰", "note": "治疗了我"})
ok(st4["情绪"] == "感激" and st4["印象"]["布兰"] == "友善",
   "A10 ⭐ 被帮助 → 感激/友善")
ok(st4["记忆"], "A11 帮助也进记忆")

st5, _ = apply_event({"强度": 80}, {"kind": "ally_down", "who": "疤脸",
                                    "by": "艾尔玛"})
ok(st5["情绪"] == "愤怒" and st5["强度"] == 80,
   "A12 高情绪时同伴倒下 → 愤怒", (st5["情绪"], st5["强度"]))
ok(st5["印象"]["艾尔玛"] == "敌意", "A13 对凶手记敌意")

for bad in ({"kind": "unknown"}, {"kind": "damage", "amount": 3},
            {"kind": "deceived", "by": "甲"}):
    try:
        apply_event({}, bad)
        ok(False, f"A14 非法事件被拒：{bad}")
    except ValueError:
        ok(True, f"A14 非法事件被拒：{bad.get('kind')}")


# --------------------------------------------------------------------------- #
section("B. 写回世界树：已登记才改，且留审计")

state = make_state()
loop = GameLoop(state, Ledger(), FakeModel(), rng=Random(20261104))
w = loop.world
w.ensure()
persona = roll_persona(Random(1), name="疤脸", role="强盗头目")
w.register_npc("疤脸", role="强盗头目", persona=persona.to_dict(),
               state=NpcState(goal_short="守住塔顶").to_dict(),
               reason="测试登记")

line = loop.notify_npc_event("疤脸", {"kind": "damage", "amount": 12,
                                      "source": "艾尔玛", "hp_ratio": 0.4})
ok(bool(line) and "挨打" not in line and "愤怒" in line, "B1 事件返回说明", line)
node = w.npc("疤脸")
ok(node["状态"]["情绪"] == "愤怒", "B2 状态写回世界树", node["状态"]["情绪"])
ok(node["状态"]["印象"]["艾尔玛"] == "敌意", "B3 印象写回")
ok(any("NPC 事件：damage" in e.reason for e in state.audit),
   "B4 事件有审计", [e.reason for e in state.audit][-3:])

ok(loop.notify_npc_event("没登记的路人", {"kind": "damage",
                                          "amount": 5, "source": "甲",
                                          "hp_ratio": 0.5}) == "",
   "B5 ⭐ 没登记的 NPC 不自动编人格")

# 防全知：状态变化只在 DM 台账
secret_texts = [e.text for e in loop.ledger.visible_to("dm")
                if "NPC 事件" in e.text]
player_texts = [e.text for e in loop.ledger.visible_to("甲")
                if "NPC 事件" in e.text]
ok(secret_texts and not player_texts,
   "B6 ⭐ 后台状态只给 DM，玩家看不到", (secret_texts, player_texts))

clone = World(StateTree.from_dict(state.to_dict(include_audit=True)))
ok(clone.npc("疤脸")["状态"]["情绪"] == "愤怒", "B7 存档往返后事件状态还在")


# --------------------------------------------------------------------------- #
section("C. 被骗：谎言/真相与情绪")

line = loop.deceive("疤脸", by="小翠", lie="箱子是空的",
                    truth="箱子里有账本")
ok(line and "误解" in line, "C1 骗局说明里有误解", line)
node = w.npc("疤脸")
known = {x["fact"]: x["state"] for x in node["状态"]["知情"]}
ok(known.get("箱子是空的") == "误解", "C2 谎言进误解", known)
ok(known.get("箱子里有账本") == "已知", "C3 真相进已知", known)
ok(node["状态"]["印象"]["小翠"] == "不可信", "C4 对骗子记不可信")


# --------------------------------------------------------------------------- #
section("D. 战斗接线：攻击命中已登记 NPC 自动挨打")

state2 = make_state()
led2 = Ledger()
loop2 = GameLoop(state2, led2, FakeModel(), rng=Random(5))
w2 = loop2.world
w2.ensure()
w2.register_npc("疤脸", role="强盗头目",
                persona=roll_persona(Random(2), name="疤脸").to_dict(),
                state=NpcState().to_dict(), reason="测试登记")

pc = make_hero_unit(state2, "甲")
npc = Unit(id="N1", name="疤脸", hp=40, hp_max=40, ac=10, team="enemy",
           position=(5.0, 0.0))
pc.position = (0.0, 0.0)
pc.actions.append(Action(name="砍", slot="action",
                         attacks=[Attack(name="剑", attack_bonus=10,
                                         damage_notation="1d8",
                                         damage_type="slashing", reach=5)]))
loop2.set_field([pc, npc])
menu = build_menu(pc, [pc, npc])
atk = next(o for o in menu.options if o.kind == "attack")
out = execute_intent(pc, Intent(unit="甲", option_id=atk.id, target="N1"),
                     menu, [pc, npc], Random(6))
ok(out.ok and out.attacks and out.attacks[0].hit,
   "D1 攻击命中了 NPC", (out.ok, [a.hit for a in out.attacks]))
loop2._log_outcome(pc, out, RoundLog(n=1, phase="combat"))
st = w2.npc("疤脸")["状态"]
ok(st["情绪"] in ("愤怒", "恐惧"), "D2 ⭐ 命中后 NPC 情绪变了", st["情绪"])
ok(st["印象"].get("甲") == "敌意", "D3 对攻击者记敌意", st["印象"])
ok(any("damage" in e.reason for e in state2.audit),
   "D4 战斗事件也有审计", [e.reason for e in state2.audit][-3:])


print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")