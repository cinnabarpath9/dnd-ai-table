# coding=utf-8
"""战斗轮：三种调用结构的对比（回答"敌人/友方统一交给一个大模型"这个设计）

三种打法：
  A 分散：每个 AI 玩家一次调用（各自决策） + DM 每个单位一次裁决
  B 友方合并：一次调用决策所有 AI 友方 + DM 每个单位一次裁决
  C 双方合并：一次调用决策所有 AI 友方 + 一次调用决策所有敌方 + DM 每单位裁决

同时算两个口径：
  · 口径1（省调用不省输出）：每个单位仍然输出 ~700 tok 的决策正文
  · 口径2（代码算数值、模型只出意图）：每个单位只输出 ~150 tok 的意图 JSON

关键问题不是"省不省 token"，而是**墙钟**和**上下文上限**。

跑法：
  python combat_calls.py
  python combat_calls.py --pc 3 --enemies 4 --rounds 6 --latency 12
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
# 每单位在"战斗中真正需要"的上下文（token）—— MINE，估算
# --------------------------------------------------------------------------- #
# ⚠️ 关键：战斗中不需要整张角色卡。只需要"打仗要用的那几栏"。
COMBAT_UNIT_BLOCK = {
    "完整角色卡": 1200,   # 日常轮要用的量（属性/所有技能/全部法术书/物品/背景）
    "战斗必需块": 260,    # 这一战要用的：AC/HP/豁免/移动/动作经济/已备法术/状态
}
#: 一个单位的"决策输出"
OUT = {
    "决策正文（含说台词）": 700,
    "意图 JSON（代码算数值）": 150,
}
#: 战斗公共上下文（每个参与方都要看到的战场）
BATTLE_COMMON = 900      # 战场描述 + 先攻序 + 各单位位置/HP/状态摘要
#: 每个敌方单位对"敌方调用"贡献的额外信息（它自己知道的）
ENEMY_PRIVATE_PER_UNIT = 150
#: 战斗轮历史增长（每轮之后战场日志增加多少）
COMBAT_LOG_GROWTH = 250


def calls_and_tokens(mode: str, n_pc: int, n_enemy: int, rounds: int,
                     block: int, out_per_unit: int) -> dict:
    """返回整场战斗的调用数与 token。"""
    pc_calls = enemy_calls = dm_calls = 0
    pc_in = pc_out = dm_in = dm_out = 0

    for r in range(rounds):
        log = COMBAT_LOG_GROWTH * r
        common = BATTLE_COMMON + log

        # ---------- 友方（AI 玩家）----------
        if mode == "A":            # 分散：每 PC 一次
            for _ in range(n_pc):
                pc_calls += 1
                pc_in += common + block
                pc_out += out_per_unit
        else:                      # B/C：一次调用，带所有 PC 的块
            pc_calls += 1
            pc_in += common + n_pc * block
            pc_out += n_pc * out_per_unit

        # ---------- 敌方 ----------
        if mode in ("A", "B"):     # 分散 or 只有友方合并
            for _ in range(n_enemy):
                pc_calls += 1
                pc_in += common + block
                pc_out += out_per_unit
        else:                      # C：一次调用决策所有敌人
            pc_calls += 1
            pc_in += common + n_enemy * (COMBAT_UNIT_BLOCK["战斗必需块"]
                                         + ENEMY_PRIVATE_PER_UNIT)
            pc_out += n_enemy * out_per_unit

        # ---------- DM 裁决：每单位一次（这个不能合并，见分析）----------
        dm_units = n_pc + n_enemy
        dm_calls += dm_units + 1     # +1 是每轮收尾叙事
        dm_in += (dm_units + 1) * (common + 700)
        dm_out += (dm_units + 1) * 250

    return {"pc_calls": pc_calls, "enemy_in_pc_calls": 0, "dm_calls": dm_calls,
            "pc_in": pc_in, "pc_out": pc_out, "dm_in": dm_in, "dm_out": dm_out}


def wall_minutes(mode: str, n_pc: int, n_enemy: int, rounds: int,
                 latency: float, out_per_unit: int = 700) -> float:
    """墙钟（秒 → 分）。这是本方案真正的收益所在。

    三种打法的关键差别：**一次调用要吐多少字**。
    合并之后调用次数少了，但单次调用的输出变长了 —— 输出越长，生成越久。

      生成时间 ≈ 输出 token / 吞吐（取 150 tok/s 作保守值）
      一次调用的墙钟 ≈ TTFT + 生成时间

    A 分散（单位之间并发）：友方 1 趟 + 敌方 1 趟 + DM 裁决 2 趟，每趟输出只含 1 个单位
    B 友方合并：友方 1 趟但要吐 n_pc 个单位的输出 + 敌方 1 趟 + DM 2 趟
    C 双方合并：友方 1 趟（n_pc 个单位）+ 敌方 1 趟（n_enemy 个单位）+ DM 2 趟
    """
    ttft = 3.0          # 保守取 3 秒（Gemini 那类可能是 13 秒，另算）
    tps = 150.0         # 保守吞吐

    def one_call(n_units: int) -> float:
        return ttft + (n_units * out_per_unit) / tps

    per_round = 0.0
    if mode == "A":
        per_round += one_call(1)          # 友方各自并发
        per_round += one_call(1)          # 敌方各自并发
    elif mode == "B":
        per_round += one_call(n_pc)       # 友方合并成一次，但要吐 n_pc 份
        per_round += one_call(1)          # 敌方仍分散
    else:
        per_round += one_call(n_pc)
        per_round += one_call(n_enemy)
    per_round += one_call(1) * 2          # DM 裁决 + 收尾叙事（串行）
    return per_round * rounds / 60


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pc", type=int, default=3, help="AI 友方单位数")
    ap.add_argument("--enemies", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--latency", type=float, default=12.0)
    args = ap.parse_args()

    print("=" * 104)
    print("战斗轮调用结构对比")
    print(f"参数：AI 友方 {args.pc} 个｜敌方 {args.enemies} 个｜战斗 {args.rounds} 轮"
          f"｜单次调用 {args.latency}s（真人玩家不占模型调用，但占墙钟）")
    print("=" * 104)

    blocks = [("完整角色卡塞进去", COMBAT_UNIT_BLOCK["完整角色卡"]),
              ("只塞战斗必需块", COMBAT_UNIT_BLOCK["战斗必需块"])]
    outs = list(OUT.items())

    for block_name, block in blocks:
        for out_name, out in outs:
            print(f"\n{'─'*104}")
            print(f"上下文口径：{block_name}（{block} tok/单位）｜输出口径：{out_name}（{out} tok/单位）")
            print(f"{'─'*104}")
            print(f"{'打法':<26}{'模型调用':>10}{'输入 tok':>13}{'输出 tok':>12}"
                  f"{'套餐(dsv1.25)':>15}{'墙钟':>9}")
            print("-" * 104)
            for mode, label in (("A", f"A 分散（每单位一次）"),
                                ("B", f"B 友方合并（敌 {args.enemies} 次）"),
                                ("C", f"C 双方合并（各 1 次）")):
                r = calls_and_tokens(mode, args.pc, args.enemies, args.rounds, block, out)
                total_calls = r["pc_calls"] + r["dm_calls"]
                tot_in = r["pc_in"] + r["dm_in"]
                tot_out = r["pc_out"] + r["dm_out"]
                w = wall_minutes(mode, args.pc, args.enemies, args.rounds, args.latency)
                print(f"{label:<26}{total_calls:>10}{tot_in:>13,}{tot_out:>12,}"
                      f"{total_calls*1.25:>15.0f}{w:>8.1f}分")

    print("\n" + "=" * 104)
    print("单项拆解（完整角色卡 + 决策正文口径，6 轮战斗）")
    print("=" * 104)
    print(f"{'打法':<26}{'友方调用':>10}{'DM 调用':>10}{'PC 输入 tok':>14}{'PC 输出 tok':>14}")
    print("-" * 104)
    for mode, label in (("A", "A 分散"), ("B", "B 友方合并"), ("C", "C 双方合并")):
        r = calls_and_tokens(mode, args.pc, args.enemies, args.rounds,
                             COMBAT_UNIT_BLOCK["完整角色卡"], OUT["决策正文（含说台词）"])
        print(f"{label:<26}{r['pc_calls']:>10}{r['dm_calls']:>10}"
              f"{r['pc_in']:>14,}{r['pc_out']:>14,}")

    print("\n" + "=" * 104)
    print("上下文上限检查：一次调用要塞多少（合并打法的真正风险）")
    print("=" * 104)
    print(f"{'内容':<44}{'tok':>10}")
    print("-" * 104)
    items = [
        ("战斗公共上下文（战场+先攻+位置/HP/状态）", BATTLE_COMMON),
        (f"{args.pc} 个友方的战斗必需块 @{COMBAT_UNIT_BLOCK['战斗必需块']}", args.pc * COMBAT_UNIT_BLOCK["战斗必需块"]),
        (f"{args.enemies} 个敌方 @{COMBAT_UNIT_BLOCK['战斗必需块']+ENEMY_PRIVATE_PER_UNIT}", args.enemies * (COMBAT_UNIT_BLOCK["战斗必需块"] + ENEMY_PRIVATE_PER_UNIT)),
        ("战斗日志（第 6 轮，250tok/轮）", COMBAT_LOG_GROWTH * args.rounds),
        ("════ 双方合并时单次最大输入 ≈", 0),
    ]
    s = 0
    for name, v in items[:-1]:
        s += v
        print(f"{name:<44}{v:>10,}")
    print(f"{'双方合并时单次最大输入 ≈':<44}{s:>10,}")
    print(f"{'（对比：若塞完整角色卡 @@1200/单位）':<44}"
          f"{BATTLE_COMMON + (args.pc+args.enemies)*1200 + COMBAT_LOG_GROWTH*args.rounds:>10,}")
    print(f"\n  Gemini 3.8 Flash 上下文窗口约 1M tok｜你选的候选模型普遍 128K~256K")
    print(f"  ⇒ 单次 {s:,} tok 的输入，距离任何一家的上限都很远。**爆上下文不是这套方案的风险。**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
