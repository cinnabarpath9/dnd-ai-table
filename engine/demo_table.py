# coding=utf-8
"""P5-B 验收 demo：**一张四个人的桌子，跑一段写在剧本文件里的冒险**。

三件事一起验：

    · 座位层  —— DM / 敌方规划 / 每个 PC 各一个客户端；第 4 个座位是**真人**；
    · 同桌轮  —— 一场里每个 PC 都轮到自己（不是"一轮只转一个人"）；
    · 剧本层  —— 幕 / 场景 / 单场简报 / 因果线 / 伏笔 / NPC 议程，
                 全部来自 `adventures\\*.json`，不再写死在代码里。

跟 `demo_loop` 的区别：

    · demo_loop  —— P3 口径：一轮只让一个 PC 动，验"一轮能闭合"；
    · demo_table —— P5 口径：一幕里每个人都动，剧本驱动，真人坐第 4 个座位。

全程 **零网络、零花费**：AI 座位用 `FakeModel`，真人座位用 `HumanSeat`。
换成真模型时只换座位上的客户端，`GameLoop` 一行都不用改。

跑法：

    python -m engine.demo_table                  # 默认剧本，交互
    python -m engine.demo_table --auto           # 全自动（预设答案，适合当回归）
    python -m engine.demo_table --adventure adventures\\我的剧本.json
    python -m engine.demo_table --save saves\\table.json
    python -m engine.demo_table --show-context   # 真人座位把完整提示词也打出来

真人输入约定（一行）：

    <选项 id>                例：look
    <选项 id> <台词>          例：look 我贴着门听一下
    <选项 id> <目标> [台词]   例：a3 E1 我劈他
    ?                        重印菜单
"""

from __future__ import annotations

import argparse
import os
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.adventure import (  # noqa: E402
    AdventureError, AdventureRunner, CampaignProgress, ProgressError,
    load_adventure, progress_path_for,
)
from engine.dice import Random  # noqa: E402
from engine.memory import MemoryStore, recall as memory_recall  # noqa: E402
from engine.models import (  # noqa: E402
    FakeModel, HumanSeat, ModelReply, TableClient,
)
from engine.models.client import _pick_option  # noqa: E402
from engine.persist import SaveError, save_game  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import (  # noqa: E402
    DM, Ledger, StateTree, lint_leaks, new_character,
)

HUMAN = "布兰"

PARTY = {
    "艾尔玛": dict(level=3, job="战士", hp_max=28, ac=16,
                   abilities={"力量": 16, "敏捷": 14, "体质": 15,
                              "智力": 10, "感知": 12, "魅力": 10}),
    "罗恩": dict(level=3, job="法师", hp_max=18, ac=12,
                 abilities={"力量": 8, "敏捷": 14, "体质": 12,
                            "智力": 16, "感知": 12, "魅力": 10}),
    "小翠": dict(level=3, job="游荡者", hp_max=20, ac=14,
                 abilities={"力量": 10, "敏捷": 16, "体质": 12,
                            "智力": 13, "感知": 12, "魅力": 10}),
    "塔莉亚": dict(level=3, job="游侠", hp_max=26, ac=14,
                  abilities={"力量": 12, "敏捷": 16, "体质": 13,
                             "智力": 10, "感知": 14, "魅力": 10}),
    HUMAN: dict(level=3, job="牧师", hp_max=24, ac=15,
                abilities={"力量": 12, "敏捷": 10, "体质": 14,
                           "智力": 10, "感知": 16, "魅力": 12},
                #: U12：真人开局带点钱，买得起一瓶治疗药水
                钱币={"金币": 60, "银币": 0, "铜币": 0}),
}

#: 开局装备与法术（这类"低频填表"将来可以由填表模型产 patch，现在先写死）
KIT = [
    ("艾尔玛", "/角色列表/艾尔玛/物品/长剑",
     {"类别": "武器", "已装备": True, "伤害公式": "1d8", "伤害类型": "挥砍"}),
    ("罗恩", "/角色列表/罗恩/物品/匕首",
     {"类别": "武器", "已装备": True, "伤害公式": "1d4", "伤害类型": "穿刺"}),
    ("小翠", "/角色列表/小翠/物品/短剑",
     {"类别": "武器", "已装备": True, "伤害公式": "1d6", "伤害类型": "穿刺"}),
    ("小翠", "/角色列表/小翠/物品/短弓",
     {"类别": "武器", "已装备": True, "伤害公式": "1d6", "伤害类型": "穿刺"}),
    ("布兰", "/角色列表/布兰/物品/硬头锤",
     {"类别": "武器", "已装备": True, "伤害公式": "1d6", "伤害类型": "钝击"}),
    ("艾尔玛", "/角色列表/艾尔玛/物品/链甲",
     {"类别": "护甲", "已装备": True, "护甲类型": "重甲"}),
    #: U12：开局带两瓶治疗药水（数值不写死 —— 喝的时候从 SRD 表格抽）
    (HUMAN, f"/角色列表/{HUMAN}/物品/治疗药水",
     {"类别": "消耗品", "数量": 2, "已装备": False,
      "英文名": "Potion of Healing", "描述": "喝下后恢复生命值的红药水"}),
    ("小翠", "/角色列表/小翠/物品/皮甲",
     {"类别": "护甲", "已装备": True, "护甲类型": "轻甲"}),
    ("塔莉亚", "/角色列表/塔莉亚/物品/长弓",
     {"类别": "武器", "已装备": True, "伤害公式": "1d8", "伤害类型": "穿刺"}),
    ("塔莉亚", "/角色列表/塔莉亚/物品/短剑",
     {"类别": "武器", "已装备": True, "伤害公式": "1d6", "伤害类型": "穿刺"}),
    ("塔莉亚", "/角色列表/塔莉亚/物品/皮甲",
     {"类别": "护甲", "已装备": True, "护甲类型": "轻甲"}),
    ("布兰", "/角色列表/布兰/物品/链甲衫",
     {"类别": "护甲", "已装备": True, "护甲类型": "中甲"}),
    ("布兰", "/角色列表/布兰/物品/盾牌",
     {"类别": "盾牌", "已装备": False, "护甲类型": "盾牌"}),
    ("罗恩", "/角色列表/罗恩/施法/法术位/1", {"当前": 4, "最大": 4}),
    ("罗恩", "/角色列表/罗恩/施法/法术位/2", {"当前": 2, "最大": 2}),
    ("罗恩", "/角色列表/罗恩/施法/法术书/魔法飞弹",
     {"等级": 1, "准备中": True, "学派": "塑能", "成分": "V、S"}),
    ("罗恩", "/角色列表/罗恩/施法/法术书/燃烧之手",
     {"等级": 1, "准备中": True, "学派": "塑能", "成分": "V、S"}),
    ("布兰", "/角色列表/布兰/施法/法术位/1", {"当前": 4, "最大": 4}),
    ("布兰", "/角色列表/布兰/施法/法术位/2", {"当前": 2, "最大": 2}),
    ("布兰", "/角色列表/布兰/施法/法术书/治疗术",
     {"等级": 1, "准备中": True, "学派": "防护", "成分": "V、S"}),
    ("布兰", "/角色列表/布兰/施法/法术书/祝福术",
     {"等级": 1, "准备中": True, "学派": "塑能", "成分": "V、S、M"}),
    ("布兰", "/角色列表/布兰/施法/法术书/致伤术",
     {"等级": 1, "准备中": True, "学派": "死灵", "成分": "V、S"}),
]

class _DemoDM(FakeModel):
    """demo 专用 DM 座位：旁白用写好的几段（仍然零网络、零花费）。"""

    def __init__(self, *, narrations: list[str], name: str = "DM（假模型）"):
        super().__init__(name=name)
        self._narr = list(narrations)

    def _complete(self, req):
        if req.task == "narrate" and self._narr:
            return ModelReply(text=self._narr.pop(0))
        return super()._complete(req)


def _status_line(loop: GameLoop, who: str) -> str:
    u = loop.find_unit(who)
    if u is None:
        return f"  {who}：不在场上"
    line = "  " + u.status_line()
    if u.spell_slots:
        slots = " ".join(f"{lv}环 {v['current']}/{v['max']}"
                         for lv, v in sorted(u.spell_slots.items()))
        line += f"｜法术位 {slots}"
    return line


def _auto_answer(human: str):
    """`--auto` 时真人座位的替身：按同一套本地策略挑，**不假装是人**。"""
    counter = {"i": 0}

    def answer(req) -> str:
        opts = list((req.allowed or {}).get("options") or [])
        if not opts:
            return ""
        combat = any("slot" in o for o in opts)
        if combat:
            pick = _pick_option(opts)
            ans = str(pick.get("id") or "")
            if pick.get("target"):
                ans += " " + str(pick["target"])
            return ans
        seq = ["look 我贴着门听一下", "search 我查查地上的痕迹",
               "talk 有人吗？", "move 我跟上队伍"]
        ans = seq[counter["i"] % len(seq)]
        counter["i"] += 1
        return ans

    return answer


def build_table(auto: bool, show_context: bool,
                narrations: list[str] | None = None,
                memory: MemoryStore | None = None
                ) -> tuple[StateTree, Ledger, GameLoop]:
    state = StateTree.from_template(
        {n: new_character(n, **kw) for n, kw in PARTY.items()},
        current=HUMAN, label="哨塔的拖痕")
    state.commit([
        {"op": "insert", "path": path, "value": value} for _who, path, value in KIT
    ], actor="填表", reason="开局装备与法术准备")

    ledger = Ledger()
    human_seat = HumanSeat(
        name=f"真人·{HUMAN}",
        auto=_auto_answer(HUMAN) if auto else None,
        show_context=show_context,
    )
    table = TableClient(
        dm=_DemoDM(narrations=narrations or []),
        planner=FakeModel(name="敌方规划（假模型）"),
        players={
            "艾尔玛": FakeModel(name="艾尔玛的模型（假）"),
            "罗恩": FakeModel(name="罗恩的模型（假）"),
            "小翠": FakeModel(name="小翠的模型（假）"),
            HUMAN: human_seat,
        },
        name="四人桌",
    )

    loop = GameLoop(state, ledger, table, rng=Random(20261104),
                    label="哨塔的拖痕", on_line=lambda s: print("  " + s),
                    memory=memory)
    loop.set_field([make_hero_unit(state, n) for n in state.characters()])
    return state, ledger, loop


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ADV = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")


def main() -> int:
    ap = argparse.ArgumentParser(description="四人同桌跑团（假模型 + 真人座位，零花费）")
    ap.add_argument("--adventure", default=DEFAULT_ADV,
                    help="剧本 JSON（默认 adventures\\哨塔的拖痕.json）")
    ap.add_argument("--auto", action="store_true",
                    help="真人座位改用预设答案（无人值守也能跑完）")
    ap.add_argument("--combat-rounds", type=int, default=None,
                    help="覆盖剧本里的战斗轮数")
    ap.add_argument("--show-context", action="store_true",
                    help="真人座位把完整提示词也打出来（查防全知用）")
    ap.add_argument("--progress", default="",
                    help="跨局记忆文件（默认与剧本同目录的 *.progress.json）")
    ap.add_argument("--no-progress", action="store_true",
                    help="本局不记跨局记忆（也不给 DM 看上一局的欠账）")
    ap.add_argument("--save", default="", help="跑完后存到这里（例：saves\\table.json）")
    ap.add_argument("--no-memory", action="store_true",
                    help="不挂记忆库（对比用：没有往事可召回）")
    ap.add_argument("--show-memory", action="store_true",
                    help="每场结束打印 DM 这一刻召回了哪几条（复盘用）")
    args = ap.parse_args()

    try:
        adv = load_adventure(args.adventure)
    except AdventureError as exc:
        print(f"❌ 剧本不能用：{exc}")
        for p in exc.problems:
            print("   · " + p)
        return 1
    for w in adv.warnings:
        print(f"⚠️ {w}")

    progress: CampaignProgress | None = None
    if not args.no_progress:
        p_path = args.progress or progress_path_for(args.adventure)
        try:
            progress = CampaignProgress.load(p_path, missing_ok=True,
                                             adventure=adv.title)
        except ProgressError as exc:
            print(f"❌ 跨局记忆读不了：{exc}")
            return 1

    state, ledger, loop = build_table(
        args.auto, args.show_context,
        narrations=[s.narration for s in adv.scenes if s.narration],
        memory=None if args.no_memory else MemoryStore(label=adv.title))
    runner = AdventureRunner(loop, adv, progress=progress)

    print("=" * 88)
    print(f"P5-B —— 一张四个人的桌子｜剧本：{adv.title}")
    print("=" * 88)
    print("  座位：DM / 敌方规划 / 艾尔玛 / 罗恩 / 小翠 / 布兰（真人）")
    print(f"  你坐的是：{HUMAN}（牧师）")
    print(f"  这一幕有 {len(adv.scenes)} 场、{len(adv.acts)} 幕；"
          f"因果线 {len(adv.threads)}｜伏笔 {len(adv.promises)}｜"
          f"NPC 议程 {len(adv.agenda)}")
    print("  全程假模型：零网络、零花费；真人座位只是把菜单交给你。")
    if progress is not None:
        debt = progress.unresolved(adv)
        label = "上一局欠着" if progress.scenes_done else "本局开局欠着"
        print(f"  跨局记忆：{progress.path or '（只在内存）'}")
        print(f"  {label} {len(debt)} 条伏笔"
              + (f"：{'、'.join(p.id for p in debt)}" if debt else "")
              + "（首场会作为「欠账」只给 DM）")
    else:
        print("  跨局记忆：本局关闭（--no-progress）")

    print("\n【开局】你的角色")
    print("-" * 88)
    print(_status_line(loop, HUMAN))
    print("  其他三个人由各自的模型座位控制（这里是假模型）。")

    started = time.time()
    current_act = ""
    try:
        for idx, scene in enumerate(adv.scenes, 1):
            act = next((a for a in adv.acts if scene.id in a.scenes), None)
            if act is not None and act.id != current_act:
                current_act = act.id
                print("\n" + "═" * 88)
                print(f"〔幕 {act.id}〕{act.title}")
                print("═" * 88)
            print("\n" + "-" * 88)
            print(f"第 {idx} 场：{scene.title}（{scene.id}）")
            print(f"  〔场景〕{scene.situation}")
            if scene.combat:
                foes = "、".join(f"{e[0]}×{e[1]}"
                                 for e in (scene.combat.get("enemies") or []))
                print(f"  〔本场有战斗〕{foes}｜距离 "
                      f"{scene.combat.get('distance', 30)} 尺")
            print("-" * 88)
            runner.run_scene(scene, combat_rounds=args.combat_rounds)
            print("\n  〔这一场结束时的队伍〕")
            for u in loop.field:
                if u.team in ("pc", "ally"):
                    print(_status_line(loop, u.name))
            if args.show_memory and loop.memory is not None:
                res = memory_recall(loop.memory, loop.memory_query(DM),
                                    count=loop.recall_count)
                print("  〔DM 此刻召回（按内容挑）〕"
                      + ("、".join(f"{r.entry.id}/{r.kind}" for r in res.items)
                         or "还没有往事"))
    except KeyboardInterrupt:
        print("\n\n（你中断了这一局。进度只在内存里，没有落盘。）")
        return 130
    except EOFError:
        print("\n\n（输入流关闭了。真正的终端里请按 Ctrl+D 前先输入选项 id。）")
        return 1

    elapsed = time.time() - started

    # ================================================================ #
    print("\n" + "=" * 88)
    print("【战报】")
    print("=" * 88)
    print(loop.report())

    print("\n【座位记账（谁花了多少）】")
    print("-" * 88)
    print("  " + loop.client.seat_summary().replace("\n", "\n  "))

    if progress is not None:
        print("\n【跨局记忆】")
        print("-" * 88)
        print("  " + progress.summary(adv).replace("\n", "\n  "))

    if loop.memory is not None:
        print("\n【时间召回 · 本局记忆】")
        print("-" * 88)
        print("  " + loop.memory.summary(DM).replace("\n", "\n  "))
        dm_now = loop.memory_query(DM)
        dm_hit = memory_recall(loop.memory, dm_now, count=loop.recall_count)
        print(f"  DM 收尾时召回 {len(dm_hit)} 条（共 {dm_hit.available} 条可选）")
    else:
        print("\n【时间召回】本局关闭（--no-memory）")

    print("\n【可见性】")
    print("-" * 88)
    for viewer in ("艾尔玛", HUMAN, DM):
        print(f"  {viewer}：可见 {len(ledger.visible_to(viewer))} 条")
    leaks = lint_leaks(ledger)
    print("  " + ("✅ 未发现私有内容泄漏" if not leaks else "✗ " + "；".join(leaks)))

    saved = ""
    if args.save:
        try:
            saved = save_game(args.save, state=state, ledger=ledger, loop=loop,
                              note=f"P5-B 四人桌 demo｜剧本《{adv.title}》")
            print(f"\n【存档】{saved}")
        except SaveError as exc:
            print(f"\n【存档失败】{exc}")

    print("\n" + "=" * 88)
    print(f"【结算】{len(loop.logs)} 轮｜{len(runner.scenes_done)} 场"
          f"｜模型座位调用 {loop.client.totals()['calls']} 次"
          f"｜墙钟 {elapsed:.1f}s｜花费 0 元")
    print(f"  剧本：{adv.title}（{args.adventure}）")
    print("  真人座位：布兰由你决定；三个 AI 座位是假模型（确定性、零花费）。")
    print("  换成真模型只换座位上的客户端 —— GameLoop、菜单、装配器都不用改。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())