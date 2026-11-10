# coding=utf-8
"""P3 验收 demo：**一轮完整跑团，全程假模型，零花费**。

跑的是同一份 `GameLoop` 代码 —— 换成真模型客户端就能打真局。
这是决定"能不能接真模型"的那道门：**假模型跑通了，才准花钱。**

跑法：
    python -m engine.demo_loop
    python -m engine.demo_loop --rounds 3 --enemies 3
    python -m engine.demo_loop --verbose        # 打印每轮的完整回合日志
"""

from __future__ import annotations

import argparse
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


def build_party(state: StateTree) -> list:
    """把 stat_data 里的角色变成战场单位。"""
    return [make_hero_unit(state, n) for n in state.characters()]


def main() -> int:
    ap = argparse.ArgumentParser(description="一轮完整跑团（假模型，零花费）")
    ap.add_argument("--rounds", type=int, default=3, help="日常轮数")
    ap.add_argument("--enemies", type=int, default=3)
    ap.add_argument("--combat-rounds", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20261014)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    rng = Random(args.seed)

    # ================================================================ #
    print("=" * 88)
    print("P3 验收 demo —— 一轮完整跑团（**假模型**，零网络、零花费）")
    print("=" * 88)

    # ---- 建局 ----
    state = StateTree.from_template({
        "艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=AB,
                                hp_max=28, ac=16),
        "罗恩": new_character("罗恩", level=3, job="法师",
                              abilities={"力量": 8, "敏捷": 14, "体质": 12,
                                         "智力": 16, "感知": 12, "魅力": 10},
                              hp_max=18, ac=12),
    }, current="艾尔玛", label="哨塔遭遇战")

    # 装备与法术（低频"填表"场景：这类 patch 将来可以由填表模型产）
    state.commit([
        {"op": "insert", "path": "/角色列表/艾尔玛/物品/长剑",
         "value": {"类别": "武器", "已装备": True, "伤害公式": "1d8",
                   "伤害类型": "挥砍"}},
        {"op": "insert", "path": "/角色列表/罗恩/物品/匕首",
         "value": {"类别": "武器", "已装备": True, "伤害公式": "1d4",
                   "伤害类型": "穿刺"}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术位/1",
         "value": {"当前": 4, "最大": 4}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术位/2",
         "value": {"当前": 2, "最大": 2}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术书/魔法飞弹",
         "value": {"等级": 1, "准备中": True, "学派": "塑能", "成分": "V、S"}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术书/火球术",
         "value": {"等级": 3, "准备中": True, "学派": "塑能", "成分": "V、S、M"}},
    ], actor="填表", reason="开局装备与法术准备")

    print("\n【1】开局状态")
    print("-" * 88)
    for n in state.characters():
        print("  " + state.render(n).replace("\n", "\n  "))
    print(f"\n  审计：{len(state.audit)} 条状态改动记录")
    for e in state.audit:
        print("    " + e.line())

    # ---- 装配 ----
    ledger = Ledger()
    client = FakeModel(name="fake-p3")
    loop = GameLoop(state, ledger, client, rng=rng, label="哨塔遭遇战")
    loop.set_field(build_party(state))

    print("\n【2】战场单位")
    print("-" * 88)
    for u in loop.field:
        print("  " + u.combat_block().replace("\n", "\n  "))
        for n in u.notes:
            print("    ⚠️ " + n)

    # ================================================================ #
    print("\n【3】日常轮（DM 推进 → AI 玩家决策 → 引擎裁定）")
    print("-" * 88)
    situations = [
        "秋日午后，你们沿着荒草掩映的小径走到一座废弃哨塔前。塔门半开着，里面很暗。",
        "塔内一层积着厚厚的灰，地上有拖拽的痕迹通向楼梯。",
        "楼梯上方传来轻微的金属碰撞声，像是有人在挪动什么重物。",
        "罗恩在一段墙根发现了刻痕，看起来是某种记号。",
        "空气变冷了，你们听见低沉的呼吸声。",
    ]
    for i in range(args.rounds):
        lg = loop.run_explore_round(
            pc_name="艾尔玛" if i % 2 == 0 else "罗恩",
            situation=situations[i % len(situations)])
        print(f"\n── 第 {lg.n} 轮（{lg.phase}）｜调用 {lg.calls} 次 ──")
        for ln in lg.lines:
            print("  " + ln.replace("\n", "\n  "))
        if lg.problems:
            print("  ⚠️ 问题：" + "；".join(lg.problems))

    # ================================================================ #
    print("\n【4】进入战斗")
    print("-" * 88)
    lg = loop.start_combat(enemies=[("Bandit", args.enemies)], distance=30)
    for ln in lg.lines:
        print("  " + ln.replace("\n", "\n  "))
    print("\n  先攻序：")
    for i, (u, init) in enumerate(loop.initiative, 1):
        print(f"    {i}. {u.name:<16} 先攻 {init}")

    # ================================================================ #
    print(f"\n【5】战斗轮 × {args.combat_rounds}（敌人一次规划 / PC 各自决策）")
    print("-" * 88)
    for _ in range(args.combat_rounds):
        if not loop.alive_enemies or not loop.alive_pcs:
            break
        lg = loop.run_combat_round(pc_name="艾尔玛")
        print(f"\n── 战斗第 {lg.n} 轮｜调用 {lg.calls} 次 ──")
        pc_calls = [c for c in client.calls
                    if c.task in ("plan_all_enemies", "combat_turn")]
        for ln in lg.lines:
            print("  " + ln.replace("\n", "\n  "))
        if lg.problems:
            print("  ⚠️ 问题：" + "；".join(lg.problems))
        print("\n  战况：")
        for u in loop.field:
            from engine.rules.turn import DeathState
            st = loop.death.get(u.id)
            extra = f"　{st.line()}" if st and (st.successes or st.failures
                                               or st.stable or st.dead) else ""
            print(f"    {u.status_line()}{extra}")

    # ================================================================ #
    print("\n【6】调用记账（这是「接真模型前必须先看的账」）")
    print("-" * 88)
    tot = client.totals()
    print(f"  模型调用 {tot['calls']} 次（失败 {tot['failed']}）")
    print(f"  输入 {tot['prompt_tokens']:,} tok｜输出 {tot['completion_tokens']:,} tok")
    print(f"  上游耗时合计 {tot['elapsed']:.2f}s")
    print("\n  按任务分类：")
    by_task: dict[str, list] = {}
    for c in client.calls:
        by_task.setdefault(c.task, []).append(c)
    for task, rows in sorted(by_task.items()):
        pin = sum(r.prompt_tokens for r in rows)
        pout = sum(r.completion_tokens for r in rows)
        print(f"    {task:<22} {len(rows):>3} 次｜入 {pin:>7,}｜出 {pout:>6,} tok")

    # 逐条调用明细
    if args.verbose:
        print("\n  逐条调用：")
        for c in client.calls:
            print("    " + c.line())

    # ================================================================ #
    print("\n【7】可见性：三个视角看到的台账")
    print("-" * 88)
    from engine.state import DM
    for viewer in ("艾尔玛", "罗恩", DM, "engine"):
        n = len(ledger.visible_to(viewer))
        print(f"\n  ── {viewer}（可见 {n} 条）──")
        for ln in ledger.render(viewer).splitlines():
            print("    " + ln)

    # ================================================================ #
    print("\n【8】泄漏检查")
    print("-" * 88)
    from engine.state import lint_leaks
    leaks = lint_leaks(ledger)
    if leaks:
        for x in leaks:
            print("  ✗ " + x)
    else:
        print("  ✅ 未发现私有内容泄漏到公开条目")

    # ================================================================ #
    print("\n【9】状态审计（每一次改动都有据可查）")
    print("-" * 88)
    for e in state.audit:
        print("  " + e.line())

    # ================================================================ #
    print("\n" + "=" * 88)
    print("【结论】")
    print("=" * 88)
    print(f"  日常轮 {args.rounds} 轮 + 战斗 {args.combat_rounds} 轮，"
          f"共 {tot['calls']} 次模型调用（**全是假模型**）")
    print(f"  全程：零网络、零花费、零真模型")
    print(f"  校验：台账 {len(ledger)} 条｜状态审计 {len(state.audit)} 条｜"
          f"泄漏 {len(leaks)} 处")
    print(f"  战斗推进："
          f"{'有实际伤害' if any(u.hp < u.hp_max for u in loop.field) else '没有伤害（异常）'}")
    print()
    print("  ⭐ 这份输出就是 P3 的验收证据：**同一份 GameLoop，换客户端就能打真局。**")
    print("     下一步（P4）只需实现 RealModel._complete()，循环一行都不用改。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
