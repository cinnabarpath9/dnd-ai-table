# coding=utf-8
"""存档 / 读档 demo：**关掉进程再接着跑**，全程假模型、零花费。

跑法：
    python -m engine.demo_save          # 存到临时目录，验完自动删
    python -m engine.demo_save --keep   # 保留存档文件（会打印路径）

它证明四件事：
  1. 状态树 + 审计 + 台账 + 战场单位 + 先攻序 + RNG 都能完整落盘；
  2. 读档后的数值与存档前逐项一致；
  3. 读档后还能继续跑下一个战斗轮（不是只能看）；
  4. 默认客户端是假模型 —— 读档不会因为忘了传客户端而真花钱。
"""

from __future__ import annotations

import argparse
import os
import shutil
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
from engine.persist import load_game, save_game  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

AB = {"力量": 8, "敏捷": 14, "体质": 12,
      "智力": 16, "感知": 12, "魅力": 10}


def build_session() -> tuple[StateTree, Ledger, GameLoop, object]:
    """造一局"打到一半"的战斗。"""
    state = StateTree.from_template({
        "罗恩": new_character("罗恩", level=3, job="法师",
                              abilities=AB, hp_max=18, ac=12),
    }, current="罗恩", label="存档 demo")
    state.commit([
        {"op": "insert", "path": "/角色列表/罗恩/物品/匕首",
         "value": {"类别": "武器", "已装备": True, "伤害公式": "1d4",
                   "伤害类型": "穿刺"}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术位/1",
         "value": {"当前": 4, "最大": 4}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术位/2",
         "value": {"当前": 2, "最大": 2}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术书/魔法飞弹",
         "value": {"等级": 1, "准备中": True, "学派": "塑能", "成分": "V、S"}},
        # 先受 6 点伤：证明"读档保留的不只是初始值"
        {"op": "delta", "path": "/角色列表/罗恩/生命值/当前", "value": -6},
    ], actor="引擎", reason="开场前先挨了一下（demo 用）")

    ledger = Ledger()
    ledger.public("公告", "你们在废弃哨塔前遭遇了强盗。", section="公告",
                  actor="DM", round=1)
    ledger.secret("公告", "DM 私记：强盗头目藏在塔里。", section="公告",
                  actor="DM", round=1)
    ledger.private("对话", "罗恩注意到墙根有新鲜的抓痕。", "罗恩",
                   section="对话", actor="DM", round=1)

    client = FakeModel(name="fake-save-demo")
    loop = GameLoop(state, ledger, client, rng=Random(20261023),
                    label="存档 demo")
    unit = make_hero_unit(state, "罗恩")
    loop.set_field([unit])

    loop.run_explore_round(pc_name="罗恩", situation="哨塔的门半开着。")
    loop.start_combat(enemies=[("Bandit", 3)], distance=30)
    loop.run_combat_round(pc_name="罗恩")      # 打到一半再存
    return state, ledger, loop, client


def main() -> int:
    ap = argparse.ArgumentParser(description="存档/读档 demo（假模型，零花费）")
    ap.add_argument("--keep", action="store_true", help="保留存档文件")
    args = ap.parse_args()

    state, ledger, loop, client = build_session()
    before = {
        "round": loop.round,
        "hp": {u.id: u.hp for u in loop.field},
        "slots": {u.id: {k: v["current"] for k, v in u.spell_slots.items()}
                  for u in loop.field},
        "init": [u.id for u, _ in getattr(loop, "initiative", [])],
        "ledger": len(ledger),
        "audit": len(state.audit),
        "rng": loop.rng.getstate(),
    }

    outdir = tempfile.mkdtemp(prefix="dnd-save-demo-")
    path = os.path.join(outdir, "session.json")
    save_game(path, state=state, ledger=ledger, loop=loop,
              note="demo：战斗打到一半")
    size = os.path.getsize(path)

    print("=" * 78)
    print("存档 demo —— 关掉进程再接着跑（假模型，零花费）")
    print("=" * 78)
    print(f"\n【1】存档前：{loop.round} 轮｜台账 {len(ledger)} 条｜"
          f"审计 {len(state.audit)} 条｜模型调用 {client.totals()['calls']} 次")
    for u in loop.field:
        print(f"    {u.status_line()}｜法术位 "
              + (" ".join(f"{lv}环{v['current']}/{v['max']}"
                          for lv, v in sorted(u.spell_slots.items())) or "无"))
    print(f"\n【2】落盘：{path}（{size:,} 字节）")

    session = load_game(path)
    s, led, lp = session.state, session.ledger, session.loop
    print(f"\n【3】读档：{session.summary()}")
    print(f"    默认客户端：{type(lp.client).__name__}"
          f"（enabled={lp.client.enabled}，零花费）")
    print(f"    状态树自检：{session.warnings or '无问题'}")

    checks = {
        "轮次一致": lp.round == before["round"],
        "HP 一致": {u.id: u.hp for u in lp.field} == before["hp"],
        "法术位一致": {u.id: {k: v["current"] for k, v in u.spell_slots.items()}
                       for u in lp.field} == before["slots"],
        "先攻序一致": [u.id for u, _ in getattr(lp, "initiative", [])]
                     == before["init"],
        "台账一致": led.render_omniscient() == ledger.render_omniscient(),
        "审计一致": len(s.audit) == before["audit"],
        "骰子状态一致": lp.rng.getstate() == before["rng"],
    }
    for name, good in checks.items():
        print(f"    {'✅' if good else '❌'} {name}")

    # 读档后继续跑一轮：不是只能"看存档"
    calls_before = lp.client.totals()["calls"]
    lp.run_combat_round(pc_name="罗恩")
    print(f"\n【4】读档后继续跑：第 {lp.round} 轮｜"
          f"台账 {len(led)} 条（+{len(led) - before['ledger']}）｜"
          f"模型调用 {lp.client.totals()['calls'] - calls_before} 次（假模型）")

    ok = all(checks.values()) and lp.round == before["round"] + 1
    print("\n" + "=" * 78)
    print("✅ 存档 → 读档 → 续跑：全部通过" if ok else "❌ 有检查项失败")
    print("=" * 78)

    if args.keep:
        print(f"\n存档已保留：{path}")
    else:
        shutil.rmtree(outdir, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
