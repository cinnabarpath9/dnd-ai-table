# coding=utf-8
"""P5-B 回归：剧本层（骨架 / 台账 / 议程 / 单场简报）+ 推进器。

守五件事：

    A. **不合法的剧本不许跑** —— 缺字段、空泛词、坏引用、坏战斗配置，逐条说清；
    B. 空泛词（"推进关系"这类）当 summary/简报 = 没写，必须报错；
    C. 骨架视图只是视图，内容与 JSON 一致，不产生第二个真相；
    D. 推进器把地点/时间**播种进世界状态**（走 commit，留审计）；
    E. 单场简报**只进 DM 的提示词**，玩家提示词里一个字都没有（防全知）。

全程零网络、零花费。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.adventure import (  # noqa: E402
    Act, AdventureError, AdventureRunner, load_adventure, parse_adventure,
    render_skeleton,
)
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")


def doc() -> dict:
    """一份最小但合法的剧本（各段测试在它的副本上改坏）。"""
    return {
        "title": "测试剧本",
        "meta": {
            "流程关键词": ["门口探查", "屋里对质"],
            "剧本特色": "旧磨坊、夜磨声、面粉里的异物",
            "玩法体感": "短探索 + 一场小战斗，线索靠观察",
            "剧本目标": "查明夜里的磨面声并找到磨坊主",
        },
        "scenes": [
            {"id": "s1", "title": "门口",
             "situation": "你们站在一座旧磨坊门口，门里没有灯。",
             "brief": {"present": "甲想推门，乙想绕着看；两个人都在门口",
                       "span": "距上一场一个时辰",
                       "offstage": "磨坊主在屋里磨面，暂时没出来"},
             "threads": ["t1"], "promises": ["p1"],
             "world": {"location": "旧磨坊", "date": "1492-01-01",
                       "period": "傍晚"}},
            {"id": "s2", "title": "屋里",
             "situation": "屋里全是面粉的味道，磨盘还在慢慢转。",
             "brief": {"present": "两个人都在屋里；甲想查磨盘，乙想找磨坊主",
                       "span": "紧接上一场",
                       "offstage": "后院有人在搬麻袋"},
             "world": {"location": "旧磨坊", "period": "夜里"},
             "combat": {"enemies": [["Bandit", 1]], "distance": 20,
                        "rounds": 2}},
        ],
        "acts": [{"id": "a1", "title": "一幕", "scenes": ["s1", "s2"]}],
        "threads": [{"id": "t1", "title": "磨坊的怪声", "kind": "main",
                     "summary": "有人在夜里磨面"}],
        "promises": [{"id": "p1", "title": "磨盘上的粉", "tag": "motif",
                      "status": "open", "summary": "面粉里混了别的东西"}],
        "decisions": [{"id": "d1", "what": "第二场才开打",
                       "why": "先给一场探索", "risk": "玩家可能直接冲"}],
        "agenda": [{"who": "磨坊主", "next": "把后院的麻袋藏进地窖",
                    "reentry": "天亮前"}],
    }


def problems(d: dict) -> tuple[list[str], list[str]]:
    return parse_adventure(copy.deepcopy(d)).validate()


def has(items: list[str], needle: str) -> bool:
    return any(needle in x for x in items)


# --------------------------------------------------------------------------- #
section("A. 校验：坏剧本不许跑")

errs, warns = problems(doc())
ok(errs == [], "A1 基准剧本合法", errs)
ok(warns == [], "A2 基准剧本没有提醒", warns)

d = doc()
d["scenes"][1]["id"] = "s1"
errs, _ = problems(d)
ok(has(errs, "场景 id 重复"), "A3 场景 id 重复 → 报错", errs)

d = doc()
d["scenes"][0]["threads"] = ["t9"]
errs, _ = problems(d)
ok(has(errs, "不存在的 Thread"), "A4 引用不存在的因果线 → 报错", errs)

d = doc()
d["scenes"][0]["promises"] = ["p9"]
errs, _ = problems(d)
ok(has(errs, "不存在的 Promise"), "A5 引用不存在的伏笔 → 报错", errs)

d = doc()
d["scenes"][0]["brief"]["span"] = ""
errs, _ = problems(d)
ok(has(errs, "简报缺少「时间跨度」"), "A6 简报缺一段 → 报错（带场次）", errs)

d = doc()
d["scenes"][0]["brief"]["present"] = "推进关系"
errs, _ = problems(d)
ok(has(errs, "空泛表述"), "A7 ⭐ 空泛词当简报 → 报错", errs)

d = doc()
d["scenes"][0]["brief"]["offstage"] = "无"
errs, _ = problems(d)
ok(has(errs, "太短"), "A8 离场只写「无」不够（要写谁在做什么）", errs)

d = doc()
d["scenes"][0]["situation"] = "进塔"
errs, _ = problems(d)
ok(has(errs, "situation 太短"), "A9 场景描述太短 → 报错", errs)

d = doc()
d["scenes"][1]["combat"]["enemies"] = []
errs, _ = problems(d)
ok(has(errs, "enemies 必须是非空数组"), "A10 战斗没有敌人 → 报错", errs)

d = doc()
d["scenes"][1]["combat"]["enemies"] = [["Bandit", 0]]
errs, _ = problems(d)
ok(has(errs, "数量必须 > 0"), "A11 敌人数量为 0 → 报错", errs)

d = doc()
d["scenes"][1]["combat"]["rounds"] = -1
errs, _ = problems(d)
ok(has(errs, "不能是负数"), "A12 战斗轮数为负 → 报错", errs)

d = doc()
d["promises"][0]["tag"] = "随便"
errs, _ = problems(d)
ok(has(errs, "tag 必须是"), "A13 伏笔类别不认识 → 报错", errs)

d = doc()
d["threads"][0]["kind"] = "主线"
errs, _ = problems(d)
ok(has(errs, "kind 必须是"), "A14 因果线类别不认识 → 报错", errs)

d = doc()
d["decisions"][0]["why"] = ""
errs, _ = problems(d)
ok(has(errs, "缺少 why"), "A15 决策没写理由 → 报错（下个人会改回去）", errs)

d = doc()
d["agenda"][0]["next"] = "埋下伏笔"
errs, _ = problems(d)
ok(has(errs, "空泛表述"), "A16 NPC 议程写空话 → 报错", errs)

d = doc()
d["acts"][0]["scenes"] = ["s1", "s9"]
errs, _ = problems(d)
ok(has(errs, "不存在的场景"), "A17 幕引用了不存在的场景 → 报错", errs)

d = doc()
d["acts"][0]["scenes"] = ["s1"]
_, warns = problems(d)
ok(has(warns, "没有归到任何一幕"), "A18 场景没归幕 → 提醒（不是错误）", warns)

d = doc()
d["promises"].append({"id": "p2", "title": "没埋的伏笔", "tag": "motif",
                      "status": "open", "summary": "没人提过它"})
_, warns = problems(d)
ok(has(warns, "没有任何场景提到它"), "A19 悬空伏笔 → 提醒", warns)

d = doc()
d["decisions"][0]["risk"] = ""
_, warns = problems(d)
ok(has(warns, "没写 risk"), "A20 决策没写风险 → 提醒", warns)

try:
    parse_adventure(["不是对象"])
    ok(False, "A21 根不是对象 → 抛错")
except AdventureError as exc:
    ok(has(exc.problems, "JSON 对象"), "A21 根不是对象 → 抛错", exc.problems)

try:
    parse_adventure({"scenes": "不是数组"})
    ok(False, "A22 scenes 不是数组 → 抛错")
except AdventureError as exc:
    ok(has(exc.problems, "必须是数组"), "A22 scenes 不是数组 → 抛错", exc.problems)

try:
    parse_adventure({"scenes": [{"id": "s1", "brief": "不是对象"}]})
    ok(False, "A23 brief 不是对象 → 抛错")
except AdventureError as exc:
    ok(has(exc.problems, "brief 必须是对象"), "A23 brief 不是对象 → 抛错", exc.problems)


# --------------------------------------------------------------------------- #
section("B. 加载文件：示例剧本与坏文件")

adv = load_adventure(SAMPLE)
ok(adv.title == "哨塔的拖痕", "B1 读到标题", adv.title)
ok([s.id for s in adv.scenes] == ["s1", "s2", "s3"], "B2 三场按顺序",
   [s.id for s in adv.scenes])
ok(len(adv.acts) == 2 and len(adv.threads) == 2, "B3 幕与因果线数量",
   (len(adv.acts), len(adv.threads)))
ok(len(adv.promises) == 3 and len(adv.agenda) == 3, "B4 伏笔与议程数量",
   (len(adv.promises), len(adv.agenda)))
ok(adv.warnings == [], "B5 示例剧本没有提醒", adv.warnings)
ok(adv.source.endswith("哨塔的拖痕.json"), "B6 记了来源路径", adv.source)

try:
    load_adventure(os.path.join(ROOT, "adventures", "根本没有这个文件.json"))
    ok(False, "B7 文件不存在 → 抛错")
except AdventureError as exc:
    ok(has(exc.problems, "读不到剧本"), "B7 文件不存在 → 抛错", exc.problems)

with tempfile.TemporaryDirectory() as td:
    bad_json = os.path.join(td, "bad.json")
    with open(bad_json, "w", encoding="utf-8") as f:
        f.write("{ 不是 json")
    try:
        load_adventure(bad_json)
        ok(False, "B8 坏 JSON → 抛错")
    except AdventureError as exc:
        ok(has(exc.problems, "不是合法 JSON"), "B8 坏 JSON → 抛错", exc.problems)

    bad_sem = os.path.join(td, "bad-sem.json")
    d = doc()
    d["scenes"][0]["brief"]["present"] = ""
    with open(bad_sem, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    try:
        load_adventure(bad_sem)
        ok(False, "B9 语义不合法 → 抛错")
    except AdventureError as exc:
        ok(has(exc.problems, "简报缺少"), "B9 语义不合法 → 抛错", exc.problems)
        ok(exc.source == bad_sem, "B10 错误里带文件路径", exc.source)


# --------------------------------------------------------------------------- #
section("C. 骨架视图：只是视图，不是第二个真相")

text = render_skeleton(adv)
ok("哨塔的拖痕" in text and "act1" in text, "C1 有标题与幕")
ok(all(s.id in text for s in adv.scenes), "C2 每场都在视图里")
ok("在场与主动性" in text and "时间跨度" in text and "离场" in text,
   "C3 三段简报都渲染出来")
ok("Bandit×3" in text, "C4 战斗写进视图")
ok(render_skeleton(adv) == text, "C5 渲染是确定性的（同输入同输出）")
ok("别拿它当第二个真相" in text or "只作阅读视图" in text,
   "C6 视图里写明它不是真相")


# --------------------------------------------------------------------------- #
section("D. 推进器：世界播种 + 简报只给 DM + 战斗")


class CaptureModel(FakeModel):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.reqs = []

    def _complete(self, req):
        self.reqs.append(req)
        return super()._complete(req)


def make_loop(names=("甲", "乙")):
    st = StateTree.from_template(
        {n: new_character(n, level=3, job="战士",
                          abilities={"力量": 16, "敏捷": 14, "体质": 14},
                          hp_max=24, ac=15) for n in names},
        current=names[0])
    st.commit([{"op": "insert", "path": f"/角色列表/{n}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}} for n in names],
              actor="填表", reason="装备")
    led = Ledger()
    seats = {n: CaptureModel(name=f"{n}-m") for n in names}
    dm = CaptureModel(name="dm-m")
    pl = CaptureModel(name="pl-m")
    table = TableClient(dm=dm, planner=pl, players=seats)
    loop = GameLoop(st, led, table, rng=Random(20261104))
    loop.set_field([make_hero_unit(st, n) for n in names])
    return st, led, loop, dm, pl, seats


st, led, loop, dm, pl, seats = make_loop()
adv2 = parse_adventure(doc())
adv2.validate()
runner = AdventureRunner(loop, adv2)
logs = runner.run_scene(adv2.scene("s1"))
ok(len(logs) >= 1 and logs[0].phase == "explore", "D1 第一场跑的是探索轮",
   [lg.phase for lg in logs])
ok(loop.world.current_location == "旧磨坊", "D2 ⭐ 地点播种进了世界状态",
   loop.world.current_location)
ok(loop.world.clock.get("日期") == "1492-01-01"
   and loop.world.clock.get("时段") == "傍晚", "D3 时间也播种了", loop.world.clock)
ok(any("进入场景 s1" in e.line() for e in st.audit),
   "D4 世界改动有审计（谁、为什么）",
   [e.line() for e in st.audit][-3:])

dm_p = next(r.prompt for r in dm.reqs if r.task == "narrate")
ok("本场简报" in dm_p and "磨坊主在屋里磨面" in dm_p,
   "D5 ⭐ 单场简报进了 DM 的提示词", dm_p[-160:])
ok("在场与主动性" in dm_p and "时间跨度" in dm_p and "离场" in dm_p,
   "D6 DM 提示词里三段齐全")
ok("【剧本四要素" in dm_p and "剧本目标" in dm_p,
   "D6b ⭐ DM 提示词带四要素/目标", dm_p[:220])
player_reqs = [r for cli in seats.values() for r in cli.reqs
               if r.task == "explore_action"]
ok(len(player_reqs) == 2, "D7 两个玩家各问了一次", len(player_reqs))
ok(all("队伍目标" in r.prompt and "查明夜里的磨面声" in r.prompt
       for r in player_reqs),
   "D7b ⭐ 玩家提示词带队伍目标，不再不知道往哪走",
   [("队伍目标" in r.prompt, "查明夜里的磨面声" in r.prompt,
     len(r.prompt)) for r in player_reqs])
ok(all("本场简报" not in r.prompt for r in player_reqs),
   "D8 ⭐ 玩家提示词里没有「本场简报」")
ok(all("磨坊主在屋里磨面" not in r.prompt for r in player_reqs),
   "D9 ⭐ 离场信息一个字都没进玩家提示词")
ok("本场因果线" in dm_p and "磨坊的怪声" in dm_p,
   "D10 ⭐ 本场挂的因果线也进了 DM 提示词", dm_p[-200:])
ok("本场伏笔" in dm_p and "磨盘上的粉" in dm_p,
   "D11 ⭐ 本场挂的伏笔也进了 DM 提示词")
ok(all("磨坊的怪声" not in r.prompt for r in player_reqs),
   "D12 ⭐ 因果线没进玩家提示词")
ok(all("磨盘上的粉" not in r.prompt for r in player_reqs),
   "D13 ⭐ 伏笔没进玩家提示词")

logs2 = runner.run_scene(adv2.scene("s2"))
phases = [lg.phase for lg in logs2]
ok("explore" in phases and "combat" in phases, "D14 有战斗的场景两段都跑",
   phases)
ok(hasattr(loop, "initiative") and loop.initiative, "D15 先攻序建好了")
ok(len([lg for lg in logs2 if lg.phase == "combat"]) <= 3,
   "D16 战斗轮数受场景配置约束（1 开局 + 最多 2 轮）", len(logs2))
_combat_tail = "\n".join(x for lg in logs2 if lg.phase == "combat" for x in lg.lines)
ok("战斗结束" in _combat_tail or "轮后收束" in _combat_tail,
   "D16b ⭐ 按轮数收场也有收尾旁白", _combat_tail[-120:])
ok(runner.scenes_done == ["s1", "s2"], "D17 记录了跑过的场景",
   runner.scenes_done)
ok(any("进入场景 s2" in e.line() for e in st.audit),
   "D18 第二场也留了世界审计")

# 跑一场没有战斗的场景 → 不应有 combat 日志
st3, led3, loop3, dm3, pl3, seats3 = make_loop()
adv3 = parse_adventure(doc())
adv3.scenes = [adv3.scenes[0]]
adv3.acts = [Act(id="a1", title="一幕", scenes=["s1"])]
_ = adv3.validate()
runner3 = AdventureRunner(loop3, adv3)
lg3 = runner3.run_all()
ok(all(lg.phase == "explore" for lg in lg3),
   "D19 没配战斗的场景不会自己开打", [lg.phase for lg in lg3])
ok(len(pl3.reqs) == 0, "D20 没有战斗就不调敌方规划", len(pl3.reqs))

# 传一个不属于这个剧本的场景
other = parse_adventure(doc())
other.scenes[0].id = "s9"
try:
    runner3.run_scene(other.scenes[0])
    ok(False, "D21 场景不属于剧本 → 抛错")
except AdventureError as exc:
    ok(has(exc.problems, "没有场景"), "D21 场景不属于剧本 → 抛错", exc.problems)

# 同一场景重复跑两次：地点不重复登记，也不能抛"已经登记过"
st4, led4, loop4, dm4, pl4, seats4 = make_loop()
adv4 = parse_adventure(doc())
runner4 = AdventureRunner(loop4, adv4)
runner4.run_scene(adv4.scene("s1"))
try:
    runner4.run_scene(adv4.scene("s1"))
    ok(True, "D22 重复进同一地点不抛错")
except Exception as exc:                      # noqa: BLE001
    ok(False, f"D22 重复进同一地点不抛错：{exc}")

# 示例剧本整段跑完（假模型，零花费）
st5, led5, loop5, dm5, pl5, seats5 = make_loop(("艾尔玛", "罗恩"))
runner5 = AdventureRunner(loop5, load_adventure(SAMPLE))
all_logs = runner5.run_all()
ok(runner5.scenes_done == ["s1", "s2", "s3"], "D23 ⭐ 示例剧本三场跑完",
   runner5.scenes_done)
ok(any(lg.phase == "combat" for lg in all_logs), "D24 示例剧本里的战斗真的打了")
ok(loop5.world.current_location == "废弃哨塔", "D25 示例剧本世界状态正确",
   loop5.world.current_location)
ok("未发现私有内容泄漏" in loop5.report(), "D26 跑完剧本后泄漏检查仍然通过")

# TPK：全 PC 倒地后 run_all 必须停，不再继续没有行动者的场景
st6, led6, loop6, dm6, pl6, seats6 = make_loop()
for _u in loop6.field:
    _u.hp = 0
_tpk_events = []
runner6 = AdventureRunner(loop6, parse_adventure(doc()),
                          on_scene_end=lambda sc: _tpk_events.append(sc.id))
runner6.run_all()
ok(runner6.failed, "D27 ⭐ 全 PC 倒地 → runner 标记失败")
ok(runner6.scenes_done == ["s1"],
   "D28 ⭐ TPK 后不再跑下一场", runner6.scenes_done)
ok(_tpk_events == [], "D29 TPK 的场不写检查点", _tpk_events)

st7, led7, loop7, dm7, pl7, seats7 = make_loop(("甲",))
adv7 = parse_adventure(doc())
adv7.meta["地图"] = {"terrain": ["~.", "~~"], "locations": []}
AdventureRunner(loop7, adv7)
ok(loop7.region_map == adv7.meta["地图"],
   "D30 ⭐ 剧本区域地图原样挂到 loop（旅行层可读）", loop7.region_map)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")