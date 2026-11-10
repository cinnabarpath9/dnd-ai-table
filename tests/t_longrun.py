# coding=utf-8
"""P5-B2 回归：长跑（台账折叠 / 预算收敛 / 存读档续跑）。

守的三件事：
  A. **渲染折叠**：只给最近 N 轮，并写明折叠了多少 —— 但台账本身一条不丢；
  B. **包体收敛**：40 轮之后单次提示词不再上涨（不再线性吃上下文）；
  C. **续跑不变形**：存档带上折叠口径，读档后包体不跳变。
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

from engine.dice import Random  # noqa: E402
from engine.longrun import (  # noqa: E402
    CaptureModel, LongRun, RoundRow, build_table_loop,
)
from engine.memory import MemoryStore, parse_game_time  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.persist import dump_game, load_from_dict  # noqa: E402
from engine.rules.loop import GameLoop, RoundLog  # noqa: E402
from engine.state import DM, Ledger, StateTree, new_character  # noqa: E402
from engine.state.visibility import lint_leaks  # noqa: E402
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


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


# --------------------------------------------------------------------------- #
section("A. 台账渲染：折叠但一条不丢")

led = Ledger()
for r in range(1, 21):
    led.public("公告", f"第 {r} 轮的公开记录，写得长一点以免太短。",
               section="公告", round=r)
    led.private("检定", f"第 {r} 轮只有甲看到的私有记录。", "甲",
                section="你的私有", round=r)

full = led.render("甲")
ok("第 1 轮" in full and "第 20 轮" in full, "A1 不折叠时从第 1 轮到第 20 轮都在")

cut = led.render("甲", last_rounds=6)
ok("第 15 轮" in cut and "第 20 轮" in cut, "A2 只保留最近 6 轮（15–20）")
ok("第 14 轮" not in cut, "A3 更早的第 14 轮被折叠掉")
ok("已折叠" in cut, "A4 ⭐ 折叠时写明「更早的 N 条已折叠」", cut[:60])
ok("更早的 28 条" in cut, "A5 折叠条数算得对（14 轮 × 2 条）", cut[:80])
ok(len(led) == 40, "A6 ⭐ 台账本身还是 40 条（折叠只发生在渲染）", len(led))
ok(len(led.visible_to("甲")) == 40, "A7 查询接口不受折叠影响")

one = led.render("甲", last_rounds=1)
ok("第 20 轮" in one and "第 19 轮" not in one, "A8 last_rounds=1 只留最后一轮")

few = Ledger()
few.public("公告", "只有一条。", round=1)
ok("已折叠" not in few.render("甲", last_rounds=6), "A9 没截断就不加折叠说明")
ok(few.render("甲", last_rounds=6).count("只有一条") == 1,
   "A10 没截断时内容照旧")

priv = led.render("乙", last_rounds=6)
ok("只有甲看到的" not in priv, "A11 ⭐ 折叠不会让别人的私有漏出来")


# --------------------------------------------------------------------------- #
section("B. 循环接线：探索 6 轮 / 战斗 4 轮")

state, ledger, loop = build_table_loop(seed=11)
ok(loop.ledger_rounds == 6 and loop.combat_ledger_rounds == 4,
   "B1 默认口径：探索 6 轮、战斗 4 轮")
st2, led2, loop2 = build_table_loop(seed=11, ledger_rounds=0)
ok(loop2.ledger_rounds == 1, "B2 ⭐ ledger_rounds 最小夹到 1", loop2.ledger_rounds)
ok(GameLoop(st2, led2, FakeModel(), combat_ledger_rounds=0)
   .combat_ledger_rounds == 1, "B3 战斗口径同样夹到 1")

run = LongRun(loop)
run.run(10)
rows = run.rows
ok(len(rows) == 10 and rows[-1].calls == 5, "B4 每轮 1 DM + 4 PC = 5 次调用",
   (len(rows), rows[-1].calls))
ok(rows[-1].ledger_entries >= 130, "B5 10 轮后台账 130+ 条", rows[-1].ledger_entries)
ok(rows[-1].folded, "B6 ⭐ 第 10 轮渲染已折叠")
later = [r.max_prompt_tokens for r in rows if r.round >= 8]
ok(later and max(later) - min(later) <= 200,
   "B7 第 8 轮起包体基本封顶", later)

# 提示词正文：折叠说明真的进了包，更早的轮次没进
prompts = [r.prompt for r in loop.client.reqs]
players = [p for p in prompts if "你是一名 D&D 冒险者" in p]
dms = [p for p in prompts if "地下城主" in p]
ok(players and all("已折叠" in p for p in players[-4:]),
   "B8 ⭐ 玩家包里有折叠说明")
ok(dms and "已折叠" in dms[-1], "B9 DM 包里有折叠说明")
ok(not any("[第1轮]" in p for p in players[-4:]),
   "B10 ⭐ 最早那一轮的原话已经不整段进包（折叠生效）")
ok(any("[第9轮]" in p or "[第10轮]" in p for p in players),
   "B11 最近几轮还在包里")

# 战斗：同样折叠（直接调规划，避免依赖战斗能不能打完）
st3, led3, loop3 = build_table_loop(seed=12)
loop3.start_combat(enemies=[("Bandit", 2)], distance=20)
for rr in range(1, 12):
    led3.public("战斗", f"第 {rr} 轮的战斗记录，写得长一点。",
                section="战斗", round=rr)
loop3.round = 12
loop3._plan_enemies(RoundLog(n=12, phase="combat"))
planner = [r.prompt for r in loop3.client.reqs if r.role == "planner"]
ok(bool(planner), "B12 敌方规划调用发生了", len(planner))
if planner:
    ok("已折叠" in planner[-1], "B13 ⭐ 战斗规划包也按 4 轮折叠",
       planner[-1][:80])


# --------------------------------------------------------------------------- #
section("C. 预算闸门")

r = LongRun(loop)
r.run(6)
ok(r.budget_problems(max_tokens=3000) == [], "C1 正常长跑没有预算问题")
ok(r.budget_problems(max_tokens=10), "C2 ⭐ 预算调到极小 → 立刻报「超预算」")
ok(any("超过预算" in p for p in r.budget_problems(max_tokens=10)),
   "C3 问题措辞点明超过预算")
empty = LongRun(loop)
ok(any("一轮都没跑" in p for p in empty.budget_problems()),
   "C4 空体检 → 直接判「失效」，不是通过")

# 不折叠的对照：确实在涨
_st, _led, loop4 = build_table_loop(seed=13, ledger_rounds=999)
run4 = LongRun(loop4).run(20)
ok(run4[-1].max_prompt_tokens > run4[0].max_prompt_tokens * 2,
   "C5 不折叠时 20 轮后包体翻倍（这就是被修掉的问题）",
   (run4[0].max_prompt_tokens, run4[-1].max_prompt_tokens))
ok(float(run4[-1].max_prompt_tokens) > float(rows[-1].max_prompt_tokens) * 1.5,
   "C6 ⭐ 折叠口径明显更省", (run4[-1].max_prompt_tokens,
                            rows[-1].max_prompt_tokens))


# --------------------------------------------------------------------------- #
section("D. 存读档续跑")

mem = MemoryStore(label="续跑")
mem.record("在哨塔门口发现过一串脚印。",
           start=parse_game_time("1492-09-17", "午后"),
           places=("废弃哨塔",), source="demo", key="k1")
st5, led5, loop5 = build_table_loop(memory=mem, seed=21)
run5 = LongRun(loop5).run(12)
base_round, base_peak = loop5.round, max(x.max_prompt_tokens for x in run5)
doc = dump_game(state=st5, ledger=led5, loop=loop5)
sess = load_from_dict(doc, client=CaptureModel())
ok(sess.loop.round == base_round, "D1 轮次接上", (sess.loop.round, base_round))
ok(sess.loop.ledger_rounds == 6 and sess.loop.combat_ledger_rounds == 4,
   "D2 折叠口径随档往返")
ok(sess.loop.memory is not None and len(sess.loop.memory) == 1,
   "D3 记忆库随档")
ok(len(sess.ledger) == len(led5), "D4 台账条数一致")

run6 = LongRun(sess.loop).run(8)
ok(sess.loop.round == base_round + 8, "D5 续跑到第 20 轮")
new_peak = max(x.max_prompt_tokens for x in run6)
ok(abs(new_peak - base_peak) <= 300,
   "D6 ⭐ 续跑包体不跳变", (new_peak, base_peak))
ok(run6[0].folded and run6[-1].folded, "D7 续跑后仍然折叠")

# 没有 CaptureModel 的客户端：LongRun 明确拒绝（不假装能测）
sess2 = load_from_dict(doc)          # client=None → FakeModel
ok(raises(lambda: LongRun(sess2.loop), TypeError),
   "D8 ⭐ 量不到提示词就明说（TypeError），不假装通过")

# RNG 续接：存档前后的骰子序列能对上
st6, led6, loop6 = build_table_loop(seed=99)
st_rng = loop6.rng.getstate()
doc6 = dump_game(state=st6, ledger=led6, loop=loop6)
sess6 = load_from_dict(doc6, client=CaptureModel())
ok(sess6.loop.rng.getstate() == st_rng, "D9 RNG 状态随档往返")


# --------------------------------------------------------------------------- #
section("E. 折叠不影响防全知")

st7 = StateTree.from_template(
    {"艾尔玛": new_character("艾尔玛", level=3, job="战士",
                             abilities={"力量": 16}, hp_max=24, ac=15),
     "罗恩": new_character("罗恩", level=3, job="法师",
                          abilities={"智力": 16}, hp_max=18, ac=12)},
    current="艾尔玛", world=new_world())
led7 = Ledger()
for rr in range(1, 15):
    led7.public("公告", f"第 {rr} 轮公开的事。", round=rr)
    led7.private("检定", f"第 {rr} 轮艾尔玛的私有发现。", "艾尔玛", round=rr)
    led7.secret("公告", f"第 {rr} 轮 DM 的秘密安排。", round=rr)
ok(lint_leaks(led7) == [], "E1 台账内部没有泄漏")
ron = led7.render("罗恩", last_rounds=4)
dm = led7.render(DM, last_rounds=4)
ok("艾尔玛的私有发现" not in ron, "E2 ⭐ 折叠后的玩家渲染里没有别人的私有")
ok("DM 的秘密安排" in dm, "E3 DM 仍然看得到秘密")
ok("艾尔玛的私有发现" in dm, "E4 DM 也看得到角色私有（它是主持人）")
ok(len(led7.visible_to("罗恩")) == 14, "E5 可见性统计不受渲染折叠影响")


# --------------------------------------------------------------------------- #
section("F. 与记忆层配合：老的进记忆，近的留台账")

mem8 = MemoryStore(label="折叠与记忆")
mem8.record("第 1 轮的事：在塔门外发现过一串脚印。",
            start=parse_game_time("1492-09-17", "午后"),
            places=("废弃哨塔",), people=("艾尔玛",), key="old-1")
st8, led8, loop8 = build_table_loop(memory=mem8, seed=31)
run8 = LongRun(loop8).run(12)
last_dm = [r.prompt for r in loop8.client.reqs if r.role == "dm"][-1]
ok("脚印" in last_dm, "F1 ⭐ 折叠掉的往事仍能从「记忆」块进 DM 包")
ok("已折叠" in last_dm, "F2 同一包里说明了台账被折叠")
ok("（第 2 轮）" not in last_dm, "F3 但更早的轮次原话已不在台账里")
ok(any("【记忆 · 时间召回】" in p for p in
       [r.prompt for r in loop8.client.reqs if r.role == "dm"]),
   "F4 记忆块确实挂上了")


# --------------------------------------------------------------------------- #
section("G. 多场连续：记忆按场长、召回有上限、包体封顶")

from engine.adventure.model import parse_adventure  # noqa: E402
from engine.adventure.runner import AdventureRunner  # noqa: E402
from engine.memory import recall  # noqa: E402
from engine.state.assembler import estimate_tokens  # noqa: E402


def make_adv(n: int):
    scenes = []
    for i in range(1, n + 1):
        scenes.append({
            "id": f"g{i}", "title": f"第 {i} 场",
            "situation": f"第 {i} 场的场景描写：走廊尽头有一处岔口，灰尘很厚。",
            "brief": {"present": "四个人都在走廊里，各自想查不同的方向",
                      "span": "紧接上一场，只过了几分钟",
                      "offstage": "塔顶的强盗仍在挪动木箱，暂时没有下来"},
            "world": {"location": "废弃哨塔"},
            "clues": [{"id": f"gc{i}",
                       "text": f"你发现墙角有一道新的刮痕，指向第 {i} 个岔口。",
                       "skill": "perception", "dc": 8}],
        })
    adv = parse_adventure({"title": "多场测试", "scenes": scenes})
    adv.validate()
    return adv


adv = make_adv(10)
mem9 = MemoryStore(label="多场")
st9, led9, loop9 = build_table_loop(memory=mem9, seed=41)
runner = AdventureRunner(loop9, adv)
prompt_rows = []
for sc in adv.scenes:
    before = len(loop9.client.reqs)
    runner.run_scene(sc)
    prompts = [r.prompt for r in loop9.client.reqs[before:] if r.prompt]
    if prompts:
        prompt_rows.append(max(estimate_tokens(p) for p in prompts))
found = len(loop9.world.found_clues())
ok(len(mem9) == len(adv.scenes) + found,
   f"G1 记忆 = 场数 + 发现数（{len(mem9)} = 10 + {found}）",
   (len(mem9), found))
ok(found >= 1, f"G2 痕迹真的被发现（{found} 条）", found)
ok(max(prompt_rows) <= 3600, f"G3 多场峰值 {max(prompt_rows)} tok 在预算内",
   max(prompt_rows))
ok(max(prompt_rows[5:]) - min(prompt_rows[5:]) <= 300,
   "G4 ⭐ 第 6 场之后包体不再增长",
   (max(prompt_rows[5:]), min(prompt_rows[5:])))
ok(len(recall(mem9, loop9.memory_query("dm"), count=6)) <= 6,
   "G5 记忆召回被上限挡住（不把几十条都塞进去）")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")