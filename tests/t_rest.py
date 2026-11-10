# coding=utf-8
"""长休（Q35）：满血、清状态、法术位回满、生命骰回一半、死者不复生。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import (  # noqa: E402
    AdventureRunner, CampaignProgress, load_adventure)
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.turn import DeathState  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

NAMES = ("布兰", "艾尔玛", "罗恩")
FAILS: list[str] = []
COUNT = 0


def ok(cond, label, extra=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{label}  extra={extra!r}")
        print(f"  ✗ {label}  extra={extra!r}")


def make_loop():
    st = StateTree.from_template(
        {n: new_character(n, level=3, job="战士", hp_max=24, ac=15)
         for n in NAMES}, current=NAMES[0])
    # 艾尔玛给一个 1 环法术位（2/2），用来验"回满"
    st.commit([{"op": "insert",
                "path": "/角色列表/艾尔玛/施法",
                "value": {"法术位": {"1": {"当前": 0, "最大": 2}}}},
               {"op": "insert", "path": "/角色列表/艾尔玛/生命值/生命骰",
                "value": {"当前": 0, "总数": 3}}],
              actor="填表", reason="测试夹具")
    loop = GameLoop(st, Ledger(), TableClient(
        dm=FakeModel(), planner=FakeModel(),
        players={n: FakeModel() for n in NAMES}), rng=Random(20261109))
    loop.set_field([make_hero_unit(st, n) for n in NAMES])
    return st, loop


st, loop = make_loop()
for u in loop.field:
    u.hp = 3
    u.temp_hp = 2
    u.conditions["prone"] = 1
loop.field[1].spell_slots[1] = {"current": 0, "max": 2}
loop.death["布兰"] = DeathState(successes=1, failures=2)
out = loop.long_rest()

ok(out["rested"] == list(NAMES), "A1 三个人都休整了", out)
ok(all(u.hp == u.hp_max for u in loop.field), "A2 ⭐ 满血", [u.hp for u in loop.field])
ok(all(u.temp_hp == 0 for u in loop.field), "A3 临时生命清零")
ok(all(not u.conditions for u in loop.field), "A4 ⭐ 状态清空", [u.conditions for u in loop.field])
ok(loop.field[1].spell_slots[1]["current"] == 2, "A5 ⭐ 法术位回满",
   loop.field[1].spell_slots)
st_dice = st.read("/角色列表/艾尔玛/生命值/生命骰", {})
ok(int(st_dice.get("当前") or 0) == 1,
   "A6 ⭐ 生命骰回一半（3 总数 → 1 颗，向下取整）", st_dice)
st_hp = st.read("/角色列表/布兰/生命值/当前")
ok(int(st_hp) == 24, "A7 ⭐ HP 回写 state（角色卡不再显示旧血）", st_hp)
st_slots = st.read("/角色列表/艾尔玛/施法/法术位/1/当前")
ok(int(st_slots) == 2, "A8 法术位也回写 state", st_slots)
ok(loop.death["布兰"].successes == 0 and loop.death["布兰"].failures == 0,
   "A9 濒死计数清零")
ok(any("长休" in e.text for e in loop.ledger.entries), "A10 台账里有一条长休公告",
   [e.text for e in loop.ledger.entries][-2:])

# 死者不复生
st2, loop2 = make_loop()
loop2.field[2].hp = 0
loop2.death["罗恩"] = DeathState(successes=0, failures=3, dead=True)
out2 = loop2.long_rest()
ok(out2["stayed_dead"] == ["罗恩"], "B1 ⭐ 已死的人长休也不复活", out2)
ok(loop2.field[2].hp == 0, "B2 死者 HP 不动", loop2.field[2].hp)

# 稳定但 0 HP 的人会醒来
st3, loop3 = make_loop()
loop3.field[0].hp = 0
loop3.death["布兰"] = DeathState(successes=3, stable=True)
out3 = loop3.long_rest()
ok(out3["revived"] == ["布兰"], "B3 ⭐ 稳定的人长休后醒来", out3)
ok(loop3.field[0].hp == 24, "B4 醒来是满血（长休本来就回满）", loop3.field[0].hp)

# 章节休整：rest="long" 开场休一次；写错直接报错
adv = load_adventure(os.path.join(ROOT, "adventures",
                                  "M2测试本-续-上游的税关.json"))
st4, loop4 = make_loop()
for u in loop4.field:
    u.hp = 1
run = AdventureRunner(loop4, adv, rest="long")
ok(run.rest_summary.get("rested") == list(NAMES), "C1 ⭐ rest=long 开场长休",
   run.rest_summary)
ok(all(u.hp == u.hp_max for u in loop4.field), "C2 进章前满血")
try:
    AdventureRunner(loop4, adv, rest="short")
    raised = False
except ValueError:
    raised = True
ok(raised, "C3 ⭐ 不认识的 rest 直接报错（不猜）")

# 外审 B4：同一章重复构造 runner 不能反复回血（进度里记 rest_done）
import tempfile  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="rest-once-")
_pp = os.path.join(_tmp, "ch.progress.json")
st5, loop5 = make_loop()
prog5 = CampaignProgress(adventure=adv.title, path=_pp)
loop5.field[0].hp = 5
r5 = AdventureRunner(loop5, adv, progress=prog5, rest="long")
ok(loop5.field[0].hp == 24, "D1 ⭐ 第一次进章长休回满", loop5.field[0].hp)
ok(prog5.rest_done is True, "D2 rest_done 落到进度里", prog5.rest_done)
loop5.field[0].hp = 5
prog5b = CampaignProgress.load(_pp, adventure=adv.title)
r5b = AdventureRunner(loop5, adv, progress=prog5b, rest="long")
ok(loop5.field[0].hp == 5, "D3 ⭐ 重开同一章不再白回血", loop5.field[0].hp)
ok(r5b.rest_summary.get("skipped") == "已休过", "D4 摘要说明跳过了",
   r5b.rest_summary)

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")