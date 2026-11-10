# coding=utf-8
"""P5-G 验收 demo：**时间召回**（按内容挑记忆，挑完才按时间分组）。

四件事一起验：

    ① 时钟：日期 + 时段 → 分钟差 → 九档 → 展示串（边界含边界）；
    ② 召回：一分钟前的无关小事**不入选**，两年前的欠条**入选**；
    ③ 视角：DM 专属记忆不进玩家包（与台账同一条纪律）；
    ④ 接线：剧本跑完一场自动记一条 AM，第二场就能召回第一场。

全程 **零网络、零花费**（假模型），可反复跑，结果确定。

跑法：

    python -m engine.demo_memory
    python -m engine.demo_memory --verbose    # 把 match 对账表和记忆块原文都打出来
"""

from __future__ import annotations

import argparse
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.adventure.model import Adventure, Scene  # noqa: E402
from engine.adventure.runner import AdventureRunner  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.memory import (  # noqa: E402
    MemoryStore, Query, bucket_of, diff_minutes, display_of, match_text,
    parse_game_time, recall, render,
)
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import DM, Ledger, StateTree, new_character  # noqa: E402
from engine.state.world import World, new_world  # noqa: E402

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


def line(t: str = "") -> None:
    print(t)


def main() -> int:
    ap = argparse.ArgumentParser(description="时间召回 demo（零花费）")
    ap.add_argument("--verbose", action="store_true", help="打对账表与记忆块原文")
    args = ap.parse_args()

    line("=" * 84)
    line("P5-G —— 时间召回：先按内容挑，再按时间分组")
    line("=" * 84)

    # ---------------- ① 时钟 ----------------
    line("\n【① 时钟：日期 + 时段 → 档位】（边界含边界，跨日就是 days 起步）")
    line("-" * 84)
    now = parse_game_time("1492-09-17", "午后")
    line(f"  现在：{now.iso()}（剧本写的是 1492-09-17 午后）")
    probes = [
        ("几分钟前", parse_game_time("1492-09-17", "13:55")),
        ("今天早上", parse_game_time("1492-09-17", "早上")),
        ("昨天傍晚", parse_game_time("1492-09-16", "傍晚")),
        ("上个月", parse_game_time("1492-08-17", "午后")),
        ("两年前", parse_game_time("1490-09-17", "午后")),
        ("不知道", None),
    ]
    for label, gt in probes:
        if gt is None:
            line(f"  {label:<8} → 记不清时间（unknown）")
            continue
        diff = diff_minutes(now, gt)
        group = bucket_of(diff, same_date=(gt.date == now.date))
        line(f"  {label:<8} {gt.iso()} → diff={diff:>7} min → "
             f"{group:<7} → {display_of(diff, group)}")

    # ---------------- ② 召回 ----------------
    line("\n【② 召回：只按内容挑，绝不按时间远近】")
    line("-" * 84)
    store = MemoryStore(label="demo 记忆")
    near = store.record("一分钟前，路人甲打了个哈欠。",
                        start=parse_game_time("1492-09-17", "13:59"))
    far = store.record("两年前，磨坊主把欠条押给了你们，说秋收后还。",
                       start=parse_game_time("1490-09-17", "午后"),
                       people=("磨坊主",), places=("村子",), tags=("p2",))
    store.record("DM 才知道：那张欠条其实是假的，磨坊主在替人顶账。",
                 start=parse_game_time("1491-03-01", "夜晚"),
                 tags=("p2",), visible_to="dm")
    line(f"  登记 3 条：{near.id}（1 分钟前，无关）｜{far.id}（2 年前，欠条）"
         f"｜AM0003（DM 专属）")

    q = Query(text="欠条还在磨坊主手里吗？", tags=("p2",), viewer=DM, now=now)
    res = recall(store, q, count=6)
    line(f"\n  DM 的问题：「{q.text}」＋正在推进的线索 {list(q.tags)}")
    line(f"  召回 {len(res)} 条：" + "、".join(
        f"{r.entry.id}({r.kind})" for r in res.items))
    hit = [r for r in res.items if r.entry.id == far.id]
    line(f"  ⭐ {far.id} 入选 —— 它两年前，但它是那条线索"
         if hit else f"  ✗ {far.id} 没入选（不该）")
    line(f"  ⭐ {near.id} 落选 —— 它一分钟前，但与欠条无关"
         if not any(r.entry.id == near.id for r in res.items)
         else f"  ✗ {near.id} 入选了（不该）")

    if args.verbose:
        line("\n  —— 复盘对账表（match，只给人看，不进提示词）——")
        line("\n".join("  " + r for r in match_text(res).splitlines()))
        line("\n  —— 真正进 DM 提示词的记忆块 ——")
        line("\n".join("  " + r for r in render(res).splitlines()))

    # ---------------- ③ 视角 ----------------
    line("\n【③ 视角：DM 专属记忆不进玩家包】")
    line("-" * 84)
    q_pc = Query(text="欠条", tags=("p2",), viewer="艾尔玛", now=now)
    pc = recall(store, q_pc, count=6)
    pc_text = render(pc)
    line(f"  艾尔玛召回：{pc.ids() or '（无）'}")
    line("  ⭐ 艾尔玛的记忆块里没有「欠条其实是假的」"
         if "其实是假的" not in pc_text else "  ✗ 玩家看到了 DM 专属记忆")
    line(f"  DM 召回：{res.ids()}（含 DM 专属那条）")

    # ---------------- ④ 接线 ----------------
    line("\n【④ 接线：剧本跑完一场自动记一条，第二场能召回第一场】")
    line("-" * 84)
    state = StateTree.from_template({
        "艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=AB,
                                hp_max=28, ac=16),
        "罗恩": new_character("罗恩", level=3, job="法师", abilities=AB,
                              hp_max=18, ac=12),
    }, current="艾尔玛", world=new_world(date="1492-09-17", period="午后"))
    World(state).ensure()
    loop = GameLoop(state, Ledger(), FakeModel(name="demo-memory"),
                    rng=Random(20261104), label="demo",
                    memory=MemoryStore(label="剧本记忆"))
    loop.set_field([make_hero_unit(state, "艾尔玛", uid="艾尔玛"),
                    make_hero_unit(state, "罗恩", uid="罗恩")])

    adv = Adventure(title="demo 剧本", scenes=[
        Scene(id="s1", title="荒径与哨塔",
              situation="你们沿着荒径走到废弃哨塔前，塔门半开着。",
              threads=["t1"], promises=["p2"],
              world={"location": "废弃哨塔", "date": "1492-09-17",
                     "period": "午后"}),
        Scene(id="s2", title="塔内一层",
              situation="塔内一层积着灰，一道拖痕通向楼梯。",
              threads=["t1"], promises=["p2"],
              world={"location": "废弃哨塔", "date": "1492-09-17",
                     "period": "午后"}),
    ])
    runner = AdventureRunner(loop, adv)
    runner.run_scene(adv.scene("s1"))
    line(f"  跑完 s1：记忆库 {len(loop.memory)} 条 —— "
         f"{loop.memory.entries[0].id} {loop.memory.entries[0].summary[:34]}…")
    runner.run_scene(adv.scene("s2"))
    s2_hit = [r for r in recall(loop.memory, loop.memory_query(DM),
                               count=loop.recall_count).items
              if r.entry.key == "s1"]
    line(f"  跑 s2 时 DM 的召回：{'、'.join(r.entry.id for r in s2_hit) or '（无）'}"
         f" —— 共享线索 t1/p2 把它拉了回来")
    before = len(loop.memory)
    runner.run_scene(adv.scene("s1"))
    line(f"  重跑 s1 一次：{before} 条 → {len(loop.memory)} 条"
         f"（幂等键挡住重复登记）")

    line("\n" + "=" * 84)
    line("【结论】")
    line("  ① 分档是算出来的（含边界、跨日归 days、算不出就明说记不清）；")
    line("  ② 召回的排序键里**没有时间** —— 近的无关事不入选，远的线索必入选；")
    line("  ③ 玩家包里没有 DM 的记忆（engine 档连 DM 都不给）；")
    line("  ④ 剧本跑一场记一条、同场重跑不重复，第二场凭线索召回第一场。")
    line("  全程 0 次模型调用、0 元。")
    line("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())