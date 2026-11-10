# coding=utf-8
"""P5-G2 验收 demo：**幕后事件 → 玩家眼前，走检定**。

一段剧本里写着"离场"（DM 才知道的幕后），同时写着"痕迹"（玩家可能发现的那句话）。
玩家选择「观察 / 搜索」时，引擎掷 `d20 + 技能加值 vs DC`：

    · 成功 → 把痕迹交给玩家（秘密的只给发现者 + DM；公开的一发现全场可见）；
    · 失败 → **连"这里有东西"都不说**（与"没有线索"完全同一句话）；
    · 同一个人对同一条痕迹只能试一次（不许掷骰到成功），别人还能试。

全程 **零网络、零花费**（假模型 + 真骰子，固定种子，可反复跑）。

跑法：
    python -m engine.demo_clues
    python -m engine.demo_clues --verbose    # 把三个出口的原文都打出来
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

from engine.adventure.model import clue_brief_text  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.memory import MemoryStore, Query, recall  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.clues import SceneClue  # noqa: E402
from engine.rules.loop import GameLoop, RoundLog, make_hero_unit  # noqa: E402
from engine.state import DM, Ledger, StateTree, new_character  # noqa: E402
from engine.state.world import World, new_world  # noqa: E402

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}
SECRET = "楼梯上方传来压低的说话声，两个人正在争那只箱子该归谁。"
PUBLIC = "门轴有一道新刮痕，门槛下积着碎木屑：这扇门最近被反复推开过。"

FAILS = 0


def check(cond: bool, label: str) -> None:
    global FAILS
    print(f"  {'✅' if cond else '❌'} {label}")
    if not cond:
        FAILS += 1


def main() -> int:
    ap = argparse.ArgumentParser(description="幕后映射 demo（零花费）")
    ap.add_argument("--verbose", action="store_true", help="打印三个出口的原文")
    args = ap.parse_args()

    print("=" * 84)
    print("P5-G2 —— 幕后事件 → 玩家眼前：走检定，不走 DM 投放")
    print("=" * 84)

    state = StateTree.from_template({
        "艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=AB,
                                hp_max=28, ac=16),
        "罗恩": new_character("罗恩", level=3, job="法师", abilities=AB,
                              hp_max=18, ac=12),
    }, current="艾尔玛", world=new_world(date="1492-09-17", period="午后"))
    w = World(state).ensure()
    w.discover("废弃哨塔", description="塔门半开，里面很暗。")
    w.travel("废弃哨塔", actor="DM")

    mem = MemoryStore(label="demo 线索记忆")
    loop = GameLoop(state, Ledger(), FakeModel(name="demo-clues"),
                    rng=Random(20261105), memory=mem)
    loop.set_field([make_hero_unit(state, "艾尔玛"),
                    make_hero_unit(state, "罗恩")])

    def unit(name):
        return loop.find_unit(name)

    clues = (
        SceneClue(id="c1", text=SECRET, skill="perception", dc=14,
                  source="楼梯上的两个强盗在低声争执那只箱子归谁"),
        SceneClue(id="c2", text=PUBLIC, skill="investigation", dc=12,
                  secret=False, source="强盗最近反复进出塔门"),
    )
    for c in clues:
        w.register_clue(c.id, scene="s1", place="废弃哨塔", skill=c.skill,
                        dc=c.dc, text=c.text, secret=c.secret, source=c.source)
    loop.set_scene_context(scene_id="s1", clues=clues, tags=("t1",))

    print("\n【剧本写了什么】")
    print("-" * 84)
    print("  离场简报（只有 DM 看）：塔顶的强盗正在把木箱往墙根挪。")
    print("  本场痕迹（玩家要检定才可能拿到）：")
    print(clue_brief_text(
        type("S", (), {"clues": list(clues)})(), w.found_clues()))

    # ---------- ① 失败 ----------
    print("\n【① 失败：连「这里有东西」都不说】")
    print("-" * 84)
    ron = unit("罗恩")
    ron.skill_bonuses["perception"] = -30       # 保证失败（demo 用）
    r = loop._resolve_look_search(ron, "look")
    print(f"  公开（所有人）：{r.public}")
    print(f"  私有（只有罗恩）：{r.private}")
    print(f"  DM 那条：{r.dm or '（无）'}")
    check(r.public == "罗恩观察了四周。", "公开句只写「做了什么」")
    check(SECRET not in r.public + r.private + r.dm, "失败时三句都没有痕迹正文")
    check(r.clue is not None and not r.clue.found, "结果标为失败")
    check(w.clue_tried_by("c1") == ["罗恩"], "记下「罗恩试过了」")

    # ---------- ② 成功（秘密） ----------
    print("\n【② 成功（秘密痕迹）：只给发现者 + DM】")
    print("-" * 84)
    elma = unit("艾尔玛")
    elma.skill_bonuses["perception"] = 30       # 保证成功（demo 用）
    r2 = loop._resolve_look_search(elma, "look")
    print(f"  公开（所有人）：{r2.public}")
    print(f"  私有（只有艾尔玛）：{r2.private}")
    print(f"  DM 那条：{r2.dm}")
    check("你发现了" in r2.private and SECRET in r2.private, "秘密痕迹进私有")
    check(SECRET not in r2.public, "公开句里没有正文")
    check(w.clue("c1")["已发现"] and w.clue("c1")["发现者"] == "艾尔玛",
          "当场写进世界树（读档也记得）")
    loop._record_clue_memory(elma, r2.clue)

    # ---------- ③ 不能再试 ----------
    print("\n【③ 同一个人试过就不能再掷（不许掷骰到成功）】")
    print("-" * 84)
    r3 = loop._resolve_look_search(elma, "look")
    print(f"  艾尔玛再观察：{r3.public} 私有：{r3.private}")
    check(r3.clue is None, "不再撞上同一条（走的是「普通观察」分支）")

    # ---------- ④ 公开痕迹 ----------
    print("\n【④ 成功（公开痕迹）：一发现全场可见】")
    print("-" * 84)
    elma.skill_bonuses["investigation"] = 30
    r4 = loop._resolve_look_search(elma, "search")
    print(f"  公开：{r4.public}")
    check("艾尔玛发现了" in r4.public and PUBLIC in r4.public, "公开痕迹进公开句")
    check(r4.private == "", "公开痕迹没有私有那句")
    loop._record_clue_memory(elma, r4.clue)

    # ---------- ⑤ 别人看不到 ----------
    print("\n【⑤ 视角：罗恩拿不到艾尔玛发现的秘密痕迹】")
    print("-" * 84)
    asm = loop._assembler()
    loop._set_world(asm, "罗恩")
    asm.set_block("角色卡", state.render("罗恩"))
    pack = asm.build("罗恩", task="轮到你。")
    check(SECRET not in pack.text(), "罗恩的完整提示词里没有秘密痕迹")
    check("线索 · 仅 DM" not in state.render("罗恩"), "玩家的世界块没有线索清单")
    q = Query(text="箱子", people=("罗恩",), places=("废弃哨塔",),
              viewer="罗恩")
    ids = recall(mem, q, count=6).ids()
    print(f"  罗恩的记忆召回：{ids or '（无）'}")
    check(not any(i for i in ids if i in ("AM0001",)), "秘密记忆不在罗恩的召回里")
    check(any(i for i in ids if i == "AM0002"), "公开记忆罗恩能召回")

    # ---------- ⑥ DM 视角 ----------
    print("\n【⑥ DM 视角：知道哪条被发现、哪条还藏着】")
    print("-" * 84)
    dm_text = w.render(DM)
    check("线索 · 仅 DM" in dm_text, "DM 世界块有线索清单")
    check("已被 艾尔玛 发现" in dm_text, "标出发现者")
    check("c1" in dm_text and "c2" in dm_text, "两条都在")
    if args.verbose:
        print(dm_text)

    print("\n" + "=" * 84)
    print("【结论】")
    print("  ① 玩家主动观察/搜索，引擎掷骰；成功才把痕迹交出去；")
    print("  ② 失败与「没有线索」是同一句话 —— 不泄漏「这里有东西」；")
    print("  ③ 秘密痕迹只给发现者 + DM；公开痕迹一发现全场可见；")
    print("  ④ 同一个人只能试一次，别人还能试；")
    print("  ⑤ 发现后进记忆，公开/秘密的可见性跟着记忆走；")
    print("  ⑥ 全程 0 次网络、0 次真实模型调用、0 元。")
    print("=" * 84)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())