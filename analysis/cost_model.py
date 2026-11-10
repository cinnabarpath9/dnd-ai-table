# coding=utf-8
"""D&D AI 跑团 —— 成本 / 时间模型 v3
阵型：1 个 DM（AI） + 3 个 AI 玩家 + 1 个真人玩家

v3 相对 v2 的改动：
  - 默认玩家数 3（AI），另有一个真人座位（不花任何模型钱，但占墙钟）
  - DM 支持两种渠道：走 Gemini（按 token 付钱）或走方舟套餐（按次数，与玩家共用额度池）
  - 新增「整场会话」汇总：把探索轮+战斗+施法加在一起给一个总数
  - 新增模型候选表（用狼人杀的真实实测数据兜底）

跑法：
  python cost_model.py                       # 默认：3 AI 玩家 + 1 真人，DM 走 Gemini
  python cost_model.py --dm ark              # DM 也走方舟套餐（零现金成本）
  python cost_model.py --cache 0.9           # 开前缀缓存
  python cost_model.py --players 3 --rounds 80
"""

from __future__ import annotations

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 价格与套餐
# --------------------------------------------------------------------------- #

# USER：DM = Gemini 3.8 Flash，用户拿到的渠道价
GEMINI_IN_PER_M = 2.07
GEMINI_OUT_PER_M = 10.35

# USER：48 元套餐 / 45000 次基准调用
PACKAGE = {"yuan": 48.0, "base_calls": 45000}

#: 玩家/DM 候选模型（倍率 = 每次调用吃掉几个基准额度）
MODELS = {
    "dsv4.1f":              {"mult": 1.25, "note": "关思考最快 3s；思考档越高反而越差"},
    "glm-5.3f":             {"mult": 0.5,  "note": "唯一档位越高越好；但不能关思考，默认档会跑飞"},
    "doubao-seed-2.1-lite": {"mult": 0.5,  "note": "131 道基础卷 98%（最高）；中档最好，高档反而崩"},
    "claude-haiku-5-5":     {"mult": 0.5,  "note": "高档 27s 很快；但基础卷有效分只有 60%"},
    "qwen3.8-flash":        {"mult": 0.5,  "note": "负责人为下桌；503 抖动 + 吐字极慢"},
    "mimo-v2.6-flash":      {"mult": 0.5,  "note": "⚠️ 最慢（中档 150s）——用户自己也说狼人杀里很慢"},
}

CACHE_RATIO = 0.1          # 前缀缓存命中价 ≈ 原价 1 折
ENG_OVERHEAD = 1.25        # 重试 / JSON 修复 / 摘要 / 工具二次解析


# --------------------------------------------------------------------------- #
# 上下文预算（token，MINE=我的估算，P2 跑通后应替换为实测）
# --------------------------------------------------------------------------- #

A = {
    "dm_system": 1500,
    "dm_rules_ref": 1200,
    "dm_lorebook": 800,
    "dm_state": 700,
    "dm_npc_sheets": 500,
    "pc_system": 700,
    "pc_sheet": 1200,
    "pc_knows": 600,
    "pc_rules": 600,
    "history_growth_per_round": 350,
    "history_cap": 20000,
    "history_after_compress": 4000,
    "tool_call_overhead": 250,
    "player_out": 700,
    "dm_out": 500,
}


def dm_in(hist: int, tool_rounds: int) -> int:
    return (A["dm_system"] + A["dm_rules_ref"] + A["dm_lorebook"]
            + A["dm_state"] + A["dm_npc_sheets"] + hist
            + tool_rounds * A["tool_call_overhead"])


def pc_in(hist: int, tool_rounds: int) -> int:
    return (A["pc_system"] + A["pc_sheet"] + A["pc_knows"] + A["pc_rules"]
            + hist + tool_rounds * A["tool_call_overhead"])


def hist_after(rounds_done: int) -> int:
    raw = A["history_growth_per_round"] * rounds_done
    if raw <= A["history_cap"]:
        return raw
    cycle = A["history_cap"] - A["history_after_compress"]
    return A["history_after_compress"] + (raw - A["history_cap"]) % cycle


def gemini_cost(in_tok: int, out_tok: int, cache_hit: float) -> float:
    miss = in_tok * (1 - cache_hit)
    hit = in_tok * cache_hit
    return ((miss + hit * CACHE_RATIO) / 1e6 * GEMINI_IN_PER_M
            + out_tok / 1e6 * GEMINI_OUT_PER_M)


# --------------------------------------------------------------------------- #
# 模拟
# --------------------------------------------------------------------------- #

def simulate(kind: str, n: int, n_pc: int, latency: float,
             decision_rounds: int, human_seat: bool) -> dict:
    """kind: turn / combat / spell；n 的含义见各分支。"""
    dm_calls = pc_calls = 0
    dm_in_tok = dm_out_tok = pc_in_tok = pc_out_tok = 0
    wall = 0.0
    # 真人座位：不调模型、不花钱，但占墙钟（他也在关键路径上）
    human_factor = 1 + (1 if human_seat else 0) * 0.5

    if kind in ("turn", "combat"):
        for r in range(n):
            h = hist_after(r)
            dm_calls += 1
            dm_in_tok += dm_in(h, 1)
            dm_out_tok += A["dm_out"]
            for _ in range(n_pc):
                pc_calls += decision_rounds
                pc_in_tok += pc_in(h, decision_rounds)
                pc_out_tok += A["player_out"]
                dm_calls += 1
                dm_in_tok += dm_in(h, decision_rounds + 1)
                dm_out_tok += A["dm_out"]
            if kind == "combat":
                # 敌人回合由本地战术引擎结算 —— 0 次模型调用
                dm_calls += 1
                dm_in_tok += dm_in(h, 1)
                dm_out_tok += A["dm_out"]
            wall += latency * (2 + decision_rounds) * human_factor
    else:  # spell
        for i in range(n):
            h = hist_after(i)
            pc_calls += 2
            pc_in_tok += pc_in(h, 1) + pc_in(h, 2)
            pc_out_tok += 2 * A["player_out"]
            dm_calls += 1
            dm_in_tok += dm_in(h, 2)
            dm_out_tok += A["dm_out"]
            wall += latency * 3 * human_factor

    dm_calls = round(dm_calls * ENG_OVERHEAD)
    pc_calls = round(pc_calls * ENG_OVERHEAD)
    return {
        "dm_calls": dm_calls, "pc_calls": pc_calls,
        "dm_in": dm_in_tok, "dm_out": dm_out_tok,
        "pc_in": pc_in_tok, "pc_out": pc_out_tok,
        "wall_min": wall / 60,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=3, help="AI 玩家数（真人另算）")
    ap.add_argument("--rounds", type=int, default=60, help="探索/剧情轮数")
    ap.add_argument("--combat-rounds", type=int, default=6)
    ap.add_argument("--spells", type=int, default=30, help="整场战斗总施法次数")
    ap.add_argument("--latency", type=float, default=12.0,
                    help="单次调用墙钟秒（含思考）")
    ap.add_argument("--cache", type=float, default=0.0,
                    help="Gemini DM 前缀缓存命中率")
    ap.add_argument("--dm", choices=["gemini", "ark"], default="gemini")
    ap.add_argument("--human", action="store_true", default=True)
    args = ap.parse_args()

    n_pc = args.players
    print("=" * 108)
    print("D&D AI 跑团 · 成本 / 时间模型 v3")
    print(f"阵型：1 DM + {n_pc} 个 AI 玩家 + 1 真人玩家"
          f"｜探索 {args.rounds} 轮 + 战斗 {args.combat_rounds} 轮 + {args.spells} 次施法")
    print(f"单次调用 {args.latency}s｜DM 渠道 = {args.dm}｜前缀缓存命中 {args.cache:.0%}")
    print("=" * 108)

    scen = [
        ("探索轮（1 趟决策）", "turn", args.rounds, 1),
        ("剧情关键轮（3 趟）", "turn", args.rounds, 3),
        ("一场战斗（6 轮）", "combat", args.combat_rounds, 3),
        (f"{args.spells} 次施法", "spell", args.spells, 2),
    ]

    tot_calls = tot_dm_in = tot_dm_out = 0
    tot_wall = 0.0
    print(f"\n{'场景':<24}{'DM 调用':>9}{'PC 调用':>9}{'DM 输出 tok':>13}"
          f"{'墙钟':>10}{'套餐(dsv4.1f)':>16}{'套餐(0.5×)':>14}")
    print("-" * 108)
    for name, kind, n, dec in scen:
        r = simulate(kind, n, n_pc, args.latency, dec, args.human)
        tot_calls += r["dm_calls"] + r["pc_calls"]
        tot_dm_in += r["dm_in"]
        tot_dm_out += r["dm_out"]
        tot_wall += r["wall_min"]
        q125 = (r["dm_calls"] + r["pc_calls"]) * 1.25
        q05 = (r["dm_calls"] + r["pc_calls"]) * 0.5
        print(f"{name:<24}{r['dm_calls']:>9}{r['pc_calls']:>9}{r['dm_out']:>13,}"
              f"{r['wall_min']:>9.1f}分{q125:>16.0f}{q05:>14.0f}")
    print("-" * 108)
    print(f"{'整场会话合计':<24}{'':>9}{tot_calls:>9}{tot_dm_out:>13,}"
          f"{tot_wall:>9.1f}分{tot_calls*1.25:>16.0f}{tot_calls*0.5:>14.0f}")

    print("\n" + "=" * 108)
    print("整场会话账（按你打算选的模型逐个算）")
    print("=" * 108)
    print(f"\n{'模型':<24}{'倍率':>6}{'套餐消耗':>11}{'占月套餐':>10}{'月可跑场数':>12}")
    print("-" * 108)
    for m, info in MODELS.items():
        q = tot_calls * info["mult"]
        print(f"{m:<24}{info['mult']:>6}{q:>11.0f}{q/45000:>10.1%}{45000/q:>12.0f}")

    print("\nDM 侧（Gemini，按 token 付钱）：")
    for ch in (0.0, 0.5, 0.9):
        c = gemini_cost(tot_dm_in, tot_dm_out, ch)
        print(f"  缓存命中 {ch:>4.0%}：输入 {tot_dm_in:,} tok / 输出 {tot_dm_out:,} tok "
              f"⇒ **{c:.2f} 元 / 场**（Gemini 官方价约 {c*5.2:.0f} 元）")
    print("\nDM 侧（改走方舟套餐）：")
    print(f"  DM 的 {tot_calls and round(tot_calls*0.0)} 次调用不额外花钱，"
          f"但整场 {tot_calls} 次调用全吃套餐池 ⇒ DM 走套餐时")
    print(f"  整场约 {tot_calls} 次 × 倍率，按 0.5 倍率算 = {tot_calls*0.5:.0f} 次"
          f" = 月套餐的 {tot_calls*0.5/45000:.1%}")

    print("\n" + "=" * 108)
    print("时间账：一次施法要几趟串行？")
    print("=" * 108)
    for lat in (4, 8, 12, 20):
        one_spell = lat * 3
        pc_turn = lat * (2 + 3)
        print(f"  单次调用 {lat:>2}s ⇒ 一次施法 {one_spell:>4.0f}s｜一个 AI 玩家回合 {pc_turn:>4.0f}s"
              f"｜战斗一轮（{n_pc} AI 串行）{pc_turn*n_pc/60:>4.1f} 分"
              f"｜{args.combat_rounds} 轮战斗 {pc_turn*n_pc*args.combat_rounds/60:>5.1f} 分")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
