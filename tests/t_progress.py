# coding=utf-8
"""P5-D 回归：跨局记忆（伏笔 beats / 已兑现 / 下一局欠账）。

守五件事：

    A. **一提一笔**：跑完一场给本场引用的伏笔记一笔，同一场重复跑不重复记；
    B. **兑现要明确**：`pays` 或 `close()` 才算收掉，引擎不猜；
    C. **坏档不静默**：格式/版本/条目不对都报错，不把新档当旧档读；
    D. **下一局只把欠账给 DM**：首场提示词里有，玩家提示词里没有；
    E. **不写剧本**：进度单独落盘，剧本 JSON 一个字节都不动。

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
    AdventureRunner, CampaignProgress, ProgressError, load_adventure,
    parse_adventure, progress_path_for, render_skeleton,
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
    return {
        "title": "伏笔测试",
        "scenes": [
            {"id": "s1", "title": "门口",
             "situation": "你们站在旧磨坊门口，门里没有灯。",
             "brief": {"present": "甲想推门，乙想绕后",
                       "span": "距上一场一个时辰",
                       "offstage": "磨坊主在屋里磨面"},
             "promises": ["p1", "p2"]},
            {"id": "s2", "title": "屋里",
             "situation": "屋里全是面粉味，磨盘还在转。",
             "brief": {"present": "两个人都进了屋",
                       "span": "紧接上一场",
                       "offstage": "后院有人搬麻袋"},
             "promises": ["p1"], "pays": ["p1"]},
        ],
        "acts": [{"id": "a1", "title": "一幕", "scenes": ["s1", "s2"]}],
        "threads": [{"id": "t1", "title": "磨坊的怪声", "kind": "main",
                     "summary": "有人在夜里磨面"}],
        "promises": [
            {"id": "p1", "title": "磨盘上的粉", "tag": "motif",
             "status": "open", "summary": "粉里混了别的东西"},
            {"id": "p2", "title": "后院的麻袋", "tag": "setup_payoff",
             "status": "open", "summary": "麻袋里装的是什么"},
            {"id": "p3", "title": "没人提过的旧债", "tag": "open_question",
             "status": "open", "summary": "从来没在场上出现过"},
            {"id": "p4", "title": "作者已收的", "tag": "motif",
             "status": "paid", "summary": "剧本里已经标了 paid"},
        ],
        "decisions": [{"id": "d1", "what": "第二场才收", "why": "先让玩家看见"}],
        "agenda": [{"who": "磨坊主", "next": "把麻袋藏进地窖"}],
    }


def adv_of(d: dict | None = None):
    a = parse_adventure(copy.deepcopy(d or doc()))
    errs, _ = a.validate()
    if errs:
        raise AssertionError(f"测试剧本不合法：{errs}")
    return a


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
    table = TableClient(dm=dm, planner=FakeModel(name="pl"),
                        players=seats)
    loop = GameLoop(st, led, table, rng=Random(7))
    loop.set_field([make_hero_unit(st, n) for n in names])
    return loop, dm, seats


# --------------------------------------------------------------------------- #
section("A. 一提一笔：记录、去重、兑现")

adv = adv_of()
prog = CampaignProgress(adventure=adv.title)
new = prog.record_scene(adv.scene("s1"))
ok(sorted(new) == ["p1", "p2"], "A1 本场引用的两条伏笔各记一笔", new)
ok(len(prog.beats_of("p1")) == 1 and prog.beats_of("p1")[0].scene == "s1",
   "A2 beat 记了场次", [b.scene for b in prog.beats_of("p1")])
ok("门口" in prog.beats_of("p1")[0].note, "A3 beat 的注里有场名",
   prog.beats_of("p1")[0].note)
ok(prog.beats_of("p1")[0].at != "", "A4 beat 带时间戳")
ok(prog.scenes_done == ["s1"], "A5 记了跑过的场", prog.scenes_done)

again = prog.record_scene(adv.scene("s1"))
ok(again == [] and len(prog.beats_of("p1")) == 1,
   "A6 ⭐ 同一场重复跑不重复记（'提过'是事实，不是尝试次数）", again)

new2 = prog.record_scene(adv.scene("s2"))
ok("p1" in new2 and prog.is_closed("p1"),
   "A7 ⭐ 场景 `pays` 把伏笔标成已兑现", (new2, prog.is_closed("p1")))
ok(len(prog.beats_of("p1")) == 2, "A8 兑现的那场也算提过一笔",
   len(prog.beats_of("p1")))
ok("兑现" in prog.promises["p1"].closed_note, "A9 兑现的说明能看出来源")

ok(prog.close("p1") is False, "A10 已经兑现过的再关 → False（不重复记）")
ok(prog.close("p2", note="DM 手动收") is True
   and prog.promises["p2"].closed, "A11 手动也能关")

ok(prog.beats_of("不存在") == [], "A12 没记录过的伏笔 → 空列表")

owed = [p.id for p in prog.unresolved(adv)]
ok("p1" not in owed and "p2" not in owed, "A13 已兑现的不算欠着", owed)
ok(owed == ["p3"], "A14 ⭐ 没提过、也没收的也算欠着（p4 作者已收）", owed)

text = prog.debt_text(adv)
ok("上一局欠着的伏笔" in text and "p3" in text, "A15 欠账块列出了 p3", text)
ok("p1" not in text and "p2" not in text, "A16 已兑现的不会出现在欠账块")
ok("没提到" in text or "没提过" in text, "A17 从没提过的会写明")

prog2 = CampaignProgress()
text2 = prog2.debt_text(adv)
line_p2 = next(ln for ln in text2.splitlines() if "p2" in ln)
ok("没提到" in line_p2, "B1 全新进度：所有 open 伏笔都算欠着，并写明没提过",
   line_p2)

prog3 = CampaignProgress()
small = adv_of()
small.promises = []
ok(prog3.debt_text(small) == "", "B2 没有伏笔 → 欠账块为空串")

# 截断
prog4 = CampaignProgress()
many = adv_of()
many.promises = [type(many.promises[0])(id=f"p{i}", title=f"第{i}条",
                                        tag="motif", status="open")
                 for i in range(10)]
t = prog4.debt_text(many, limit=3)
ok("还有 7 条没列" in t, "B3 欠账太多会截断并写明还有几条", t[-60:])


# --------------------------------------------------------------------------- #
section("C. 文件：原子写盘、往返、坏档报错")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "x.progress.json")
    p = CampaignProgress(adventure=adv.title, path=path)
    p.record_scene(adv.scene("s1"))
    p.record_scene(adv.scene("s2"))
    ok(os.path.exists(path), "C1 记完一场就落盘")
    raw = json.load(open(path, encoding="utf-8"))
    ok(raw["format"] == "dnd-campaign-progress" and raw["version"] == 1,
       "C2 文件带格式与版本")
    p2 = CampaignProgress.load(path)
    ok(p2.scenes_done == ["s1", "s2"], "C3 读回场次一致", p2.scenes_done)
    ok(len(p2.beats_of("p1")) == 2 and p2.is_closed("p1"),
       "C4 读回 beats 与兑现状态")
    ok(p2.adventure == adv.title, "C5 读回剧本名")

    leftovers = [f for f in os.listdir(td) if f.endswith(".tmp")]
    ok(leftovers == [], "C6 原子写盘没有留下临时文件", leftovers)

    missing = os.path.join(td, "nope.json")
    fresh = CampaignProgress.load(missing, missing_ok=True, adventure="A")
    ok(fresh.scenes_done == [] and fresh.path == missing,
       "C7 文件不存在 = 新的一局（missing_ok）")

    try:
        CampaignProgress.load(missing, missing_ok=False)
        ok(False, "C8 missing_ok=False 时文件不存在 → 抛错")
    except ProgressError as exc:
        ok("读不到进度文件" in str(exc), "C8 missing_ok=False → 抛错", str(exc))

    bad = os.path.join(td, "bad.json")
    open(bad, "w", encoding="utf-8").write("{ 不是 json")
    try:
        CampaignProgress.load(bad)
        ok(False, "C9 坏 JSON → 抛错")
    except ProgressError as exc:
        ok("不是合法 JSON" in str(exc), "C9 坏 JSON → 抛错", str(exc))

    wrong = os.path.join(td, "wrong.json")
    json.dump({"format": "别的", "version": 1}, open(wrong, "w", encoding="utf-8"))
    try:
        CampaignProgress.load(wrong)
        ok(False, "C10 格式不对 → 抛错")
    except ProgressError as exc:
        ok("不是本项目的进度文件" in str(exc), "C10 格式不对 → 抛错", str(exc))

    ver = os.path.join(td, "ver.json")
    json.dump({"format": "dnd-campaign-progress", "version": 99},
              open(ver, "w", encoding="utf-8"))
    try:
        CampaignProgress.load(ver)
        ok(False, "C11 版本不对 → 抛错")
    except ProgressError as exc:
        ok("版本" in str(exc), "C11 版本不对 → 抛错", str(exc))

    broken = os.path.join(td, "broken.json")
    json.dump({"format": "dnd-campaign-progress", "version": 1,
               "promises": {"p1": {"beats": [{"note": "没场次"}]}}},
              open(broken, "w", encoding="utf-8"))
    try:
        CampaignProgress.load(broken)
        ok(False, "C12 beats 条目坏 → 抛错")
    except ProgressError as exc:
        ok("格式不对" in str(exc), "C12 beats 条目坏 → 抛错", str(exc))

ok(progress_path_for("a\\b\\剧本.json").endswith("剧本.progress.json"),
   "C13 默认进度文件与剧本同名前缀", progress_path_for("a\\b\\剧本.json"))


# --------------------------------------------------------------------------- #
section("D. 剧本侧：pays 的解析、校验、渲染")

d = doc()
a = adv_of(d)
ok(a.scene("s2").pays == ["p1"], "D1 pays 被解析出来")

d = doc()
d["scenes"][1]["pays"] = ["p9"]
bad = parse_adventure(d)
errs, _ = bad.validate()
ok(any("兑现了不存在的 Promise" in e for e in errs),
   "D2 pays 指向不存在的伏笔 → 报错", errs)

d = doc()
d["scenes"][1]["promises"] = []
d["scenes"][1]["pays"] = ["p2"]
warn_adv = parse_adventure(d)
errs, warns = warn_adv.validate()
ok(errs == [] and any("兑现" in w and "没列在本场" in w for w in warns),
   "D3 pays 没列在本场 promises → 提醒", warns)

text = render_skeleton(adv_of())
ok("兑现：p1" in text, "D4 骨架视图里能看到哪场兑现哪条")


# --------------------------------------------------------------------------- #
section("E. 推进器：跑完记一笔、下一局只把欠账给 DM")

adv = adv_of()
prog = CampaignProgress(adventure=adv.title)
loop, dm, seats = make_loop()
runner = AdventureRunner(loop, adv, progress=prog)
runner.run_scene(adv.scene("s1"))
ok(len(prog.beats_of("p1")) == 1 and len(prog.beats_of("p2")) == 1,
   "E1 跑完一场就记了一笔", (len(prog.beats_of("p1")),
                              len(prog.beats_of("p2"))))
ok(not prog.is_closed("p1"), "E2 只提到、没兑现 → 仍是欠着")

runner.run_scene(adv.scene("s2"))
ok(prog.is_closed("p1"), "E3 兑现场跑完 → 自动收掉")

owed = [p.id for p in prog.unresolved(adv)]
ok(owed == ["p2", "p3"], "E4 ⭐ 下一局欠着 p2 / p3（p1 已收、p4 作者已收）", owed)

# 第一场的 DM 提示词里有欠账（这次重开一个 runner，进度带着历史）
loop2, dm2, seats2 = make_loop()
runner2 = AdventureRunner(loop2, adv, progress=prog)
runner2.run_scene(adv.scene("s1"))
dm_p = next(r.prompt for r in dm2.reqs if r.task == "narrate")
ok("上一局欠着的伏笔" in dm_p, "E5 ⭐ 下一局首场把欠账给了 DM")
ok("p2" in dm_p and "p3" in dm_p, "E6 欠账里列出 p2 / p3")
ok("p1" not in dm_p.split("上一局欠着的伏笔")[1][:200],
   "E7 已兑现的 p1 不再出现在欠账里")
player_p = [r.prompt for s in seats2.values() for r in s.reqs
            if r.task == "explore_action"]
ok(all("上一局欠着的伏笔" not in x for x in player_p),
   "E8 ⭐ 欠账一个字都没进玩家提示词")
ok(all("p3" not in x for x in player_p), "E9 伏笔编号没漏进玩家提示词")

# 同一局的第二场不再重复塞欠账
dm_hint2 = runner2.dm_hint(adv.scene("s2"))
ok("上一局欠着的伏笔" not in dm_hint2,
   "E10 欠账只在本次第一场出现，不每场都塞")

# 没有 progress：不记、不塞、不写盘
loop3, dm3, seats3 = make_loop()
runner3 = AdventureRunner(loop3, adv)
runner3.run_scene(adv.scene("s1"))
dm_p3 = next(r.prompt for r in dm3.reqs if r.task == "narrate")
ok("上一局欠着的伏笔" not in dm_p3, "E11 没配 progress → 不塞欠账")
ok(runner3.progress is None, "E12 没配 progress → 不记录")

# 落盘：跑完一场文件里就有
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "run.progress.json")
    p = CampaignProgress(adventure=adv.title, path=path)
    loop4, dm4, seats4 = make_loop()
    runner4 = AdventureRunner(loop4, adv, progress=p)
    runner4.run_scene(adv.scene("s1"))
    ok(os.path.exists(path), "E13 跑完一场进度文件就在盘上")
    reloaded = CampaignProgress.load(path)
    ok(reloaded.scenes_done == ["s1"]
       and len(reloaded.beats_of("p2")) == 1, "E14 读档接得上跨局记忆")

# 示例剧本：一局跑完 p1 收了，p2/p3 欠着
sample = load_adventure(SAMPLE)
prog5 = CampaignProgress(adventure=sample.title)
loop5, dm5, seats5 = make_loop(("艾尔玛", "罗恩"))
runner5 = AdventureRunner(loop5, sample, progress=prog5)
runner5.run_all()
owed5 = [p.id for p in prog5.unresolved(sample)]
ok(owed5 == ["p2", "p3"], "E15 ⭐ 示例剧本跑完：p1 已收，欠 p2/p3", owed5)
ok(prog5.scenes_done == ["s1", "s2", "s3"], "E16 三场都记了",
   prog5.scenes_done)

# 不写剧本：跑一遍之后剧本 JSON 内容不变
before = open(SAMPLE, "rb").read()
loop6, dm6, seats6 = make_loop(("艾尔玛", "罗恩"))
AdventureRunner(loop6, sample, progress=CampaignProgress()).run_all()
after = open(SAMPLE, "rb").read()
ok(before == after, "E17 ⭐ 跨局记忆不碰剧本 JSON（一个字节都没变）")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")