# coding=utf-8
"""P5-B2 验收 demo：**长跑体检**（40 轮四人桌 + 存读档 + 折叠对比）。

三件事一起验：

    ① 收敛：台账折叠之后，单次提示词在第 8 轮附近封顶，不再随轮次线性上涨；
    ② 闸门：`budget_problems()` 对"超预算 / 没收敛"直接报问题（exit=1）；
    ③ 续跑：第 20 轮存档 → 读档 → 再跑 20 轮，轮次与包体接得上。

全程 **零网络、零花费**（假模型），固定种子，可反复跑。

跑法：
    python -m engine.demo_longrun
    python -m engine.demo_longrun --rounds 60     # 想跑更长就加
    python -m engine.demo_longrun --verbose       # 每轮都打一行
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

from engine.adventure.model import parse_adventure  # noqa: E402
from engine.adventure.runner import AdventureRunner  # noqa: E402
from engine.longrun import CaptureModel, LongRun, build_table_loop  # noqa: E402
from engine.memory import MemoryStore, parse_game_time, recall  # noqa: E402
from engine.persist import dump_game, load_from_dict  # noqa: E402
from engine.state.assembler import estimate_tokens  # noqa: E402

FAILS = 0


def _make_adv(n: int):
    """造一个 n 场的连续剧本（每场一条容易发现的痕迹）。"""
    scenes = []
    for i in range(1, n + 1):
        scenes.append({
            "id": f"s{i}",
            "title": f"第 {i} 场",
            "situation": (f"第 {i} 场的场景描写：你们沿着塔内走廊走到一处岔口，"
                          f"灰尘很厚，墙上的火把早就灭了。"),
            "brief": {
                "present": "四个人都在走廊里，各自想查不同的方向",
                "span": "紧接上一场，只过了几分钟",
                "offstage": "塔顶的强盗仍在挪动木箱，暂时没有下来",
            },
            "world": {"location": "废弃哨塔"},
            "clues": [{
                "id": f"c{i}",
                "text": f"你发现墙角有一道新的刮痕，指向第 {i} 个岔口。",
                "skill": "perception",
                "dc": 8,
            }],
        })
    adv = parse_adventure({"title": "长跑剧本", "scenes": scenes})
    adv.validate()
    return adv


def check(cond: bool, label: str) -> None:
    global FAILS
    print(f"  {'✅' if cond else '❌'} {label}")
    if not cond:
        FAILS += 1


def main() -> int:
    ap = argparse.ArgumentParser(description="长跑体检（零花费）")
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--verbose", action="store_true", help="每轮打一行")
    args = ap.parse_args()
    half = max(4, args.rounds // 2)

    print("=" * 92)
    print("P5-B2 —— 长跑体检：40 轮四人桌，量上下文有没有失控")
    print("=" * 92)
    print("  这张桌子：4 个 PC（假模型）＋ 1 次 DM 旁白/轮 ⇒ 每轮 5 次调用。")
    print("  台账口径：只渲染最近 6 轮（更早的折叠，靠「记忆」块回忆）。")

    # ---------------- ① 折叠后的长跑 ----------------
    print("\n【① 折叠台账：40 轮】")
    print("-" * 92)
    mem = MemoryStore(label="长跑记忆")
    state, ledger, loop = build_table_loop(memory=mem, seed=20261105)
    run = LongRun(loop)
    for i in range(1, args.rounds + 1):
        row = run.step()
        if args.verbose or i % 5 == 0 or i == 1:
            print("  " + row.line())
    rows = run.rows
    peak_row = max(rows, key=lambda r: r.max_prompt_tokens)
    peak = peak_row.max_prompt_tokens
    print(f"\n  峰值 {peak} tok（第 {peak_row.round} 轮）｜"
          f"首轮 {rows[0].max_prompt_tokens} tok｜"
          f"末轮 {rows[-1].max_prompt_tokens} tok")
    print(f"  预热后波动：{run.plateau()} tok")
    check(peak <= 3000, f"单次提示词峰值 {peak} tok ≤ 3,000")
    check(run.plateau() <= 200, f"第 8 轮之后不再增长（波动 {run.plateau()} tok）")
    check(rows[-1].ledger_entries >= 10 * args.rounds,
          f"台账确实在长（{len(ledger)} 条），只是不整本塞进包")
    check(not run.budget_problems(), "budget_problems 为空")

    # ---------------- ② 不折叠的对比 ----------------
    print("\n【② 对照：不折叠会怎样（同样 30 轮）】")
    print("-" * 92)
    _s2, _l2, loop2 = build_table_loop(seed=20261105, ledger_rounds=999)
    run2 = LongRun(loop2).run(30)
    peak2 = max(r.max_prompt_tokens for r in run2)
    print(f"  不折叠：峰值 {peak2} tok（第 30 轮 {run2[-1].max_prompt_tokens} tok）")
    folded_at_30 = rows[min(29, len(rows) - 1)]
    print(f"  折叠后：第 {folded_at_30.round} 轮 {folded_at_30.max_prompt_tokens} tok")
    check(folded_at_30.max_prompt_tokens * 2 < peak2,
          f"折叠后的第 30 轮不到不折叠的一半"
          f"（{folded_at_30.max_prompt_tokens} vs {peak2}）")
    check(run2[-1].max_prompt_tokens > run2[0].max_prompt_tokens,
          "不折叠确实线性上涨（这是被修掉的那个问题）")

    # ---------------- ③ 存读档续跑 ----------------
    print(f"\n【③ 第 {half} 轮存档 → 读档 → 再跑 {args.rounds - half} 轮】")
    print("-" * 92)
    mem2 = MemoryStore(label="续跑记忆")
    mem2.record("在废弃哨塔门口发现过一串新鲜脚印。",
                start=parse_game_time("1492-09-17", "午后"),
                places=("废弃哨塔",), people=("艾尔玛",),
                source="demo", key="demo-1")
    st3, led3, loop3 = build_table_loop(memory=mem2, seed=7)
    run3 = LongRun(loop3).run(half)
    before_round = loop3.round
    before_peak = max(r.max_prompt_tokens for r in run3)
    doc = dump_game(state=st3, ledger=led3, loop=loop3, note="长跑续跑")
    sess = load_from_dict(doc, client=CaptureModel())
    check(sess.loop.round == before_round,
          f"读档后轮次接上（{sess.loop.round}）")
    check(sess.loop.ledger_rounds == loop3.ledger_rounds,
          "折叠口径随档走（不会读档后突然整本渲染）")
    check(sess.loop.memory is not None and len(sess.loop.memory) == 1,
          "记忆库随档往返")
    run4 = LongRun(sess.loop)
    rows4 = run4.run(args.rounds - half)
    check(sess.loop.round == args.rounds,
          f"续跑后轮次到 {sess.loop.round}")
    check(max(r.max_prompt_tokens for r in rows4) <= before_peak + 300,
          f"续跑包体没有跳变（{max(r.max_prompt_tokens for r in rows4)} vs "
          f"{before_peak}）")

    # ---------------- ④ 多场连续：剧本 + 记忆 + 痕迹 ----------------
    print("\n【④ 多场连续：12 场剧本 + 记忆 + 痕迹】")
    print("-" * 92)
    adv = _make_adv(12)
    mem9 = MemoryStore(label="多场记忆")
    st9, led9, loop9 = build_table_loop(memory=mem9, seed=41)
    runner = AdventureRunner(loop9, adv)
    rows9 = []
    for sc in adv.scenes:
        before = len(loop9.client.reqs)
        runner.run_scene(sc)
        prompts = [r.prompt for r in loop9.client.reqs[before:]]
        rows9.append((sc.id, len(prompts),
                      max(len(p) for p in prompts),
                      max(estimate_tokens(p) for p in prompts)))
    for sid, n, chars, toks in rows9:
        print(f"  {sid:>3}  调用 {n:>2}  最大 {chars:>5} 字  ≈ {toks:>5} tok")
    found = len(loop9.world.found_clues())
    recall_n = len(recall(mem9, loop9.memory_query("dm"),
                          count=loop9.recall_count))
    print(f"\n  12 场跑完：台账 {len(led9)} 条｜记忆 {len(mem9)} 条"
          f"（{len(adv.scenes)} 场 + {found} 条痕迹）｜"
          f"记忆召回上限 {recall_n} 条")
    check(len(mem9) == len(adv.scenes) + found,
          f"记忆 = 场数 + 发现数（{len(mem9)} = {len(adv.scenes)} + {found}）")
    check(found >= 1, f"12 场里真的有痕迹被检定发现（{found} 条）")
    peak9 = max(t for _s, _n, _c, t in rows9)
    tail9 = [t for _s, _n, _c, t in rows9[5:]]
    print(f"  峰值 {peak9} tok｜第 6 场之后波动 {max(tail9) - min(tail9)} tok")
    check(peak9 <= 3600, f"多场连续的包体在预算内（{peak9} ≤ 3,600 tok）")
    check(max(tail9) - min(tail9) <= 300,
          f"第 6 场之后不再增长（波动 {max(tail9) - min(tail9)} tok）")
    check(recall_n <= 6, f"记忆召回被上限挡住（{recall_n} ≤ 6，不是把几十条都塞进去）")
    last_dm9 = [r.prompt for r in loop9.client.reqs if r.role == "dm"][-1]
    check(all(f"[第{i}轮]" not in last_dm9 for i in range(1, 4)),
          "最早几轮已折叠（进的是记忆，不是台账）")

    print("\n" + "=" * 92)
    print("【结论】")
    print("  ① 台账只渲染最近 6 轮：40 轮峰值 ~2.2k tok，第 8 轮后封顶；")
    print("  ② 不折叠的话 30 轮就到 ~7.9k tok —— 60 轮会吃掉半个上下文；")
    print("  ③ 折叠写进台账首行（「更早的 N 条已折叠」），模型不会以为前面无事发生；")
    print("  ④ 存档带上折叠口径，读档续跑包体不跳变；")
    print("  ⑤ 12 场连续跑：记忆按「场 + 痕迹」长，召回有上限；")
    print("  ⑥ 全程 0 次网络、0 元。")
    print("=" * 92)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())