# coding=utf-8
"""P5-D2 回归：beat 内容 + 战役层（跨剧本欠账）。

守的口径：

    · 每一笔 beat 要写**这一场推进成了什么样**（不是只记"哪场提过"）；
    · `beat_notes` 的键必须在本场 promises 里，空泛/非字符串直接报错；
    · 战役文件只列章节；前面章节里"还 open 且没关"的伏笔，汇总成下一章的欠账；
    · 跨章欠账**只进 DM 的任务说明**，玩家提示词里一个字都没有；
    · 坏战役/坏引用/坏剧本都不许跑（不猜、不静默跳过）。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import (  # noqa: E402
    AdventureRunner, Campaign, CampaignError, CampaignProgress, load_adventure,
    load_campaign, parse_adventure, parse_campaign, previous_chapters,
    progress_path_for, debt_text, debts,
)
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

FAILS: list[str] = []
COUNT = 0
SAMPLE = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")
SAMPLE_CAMPAIGN = os.path.join(ROOT, "adventures", "哨塔的拖痕.campaign.json")

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


def doc() -> dict:
    """最小合法剧本（第二幕用）。"""
    return {
        "title": "塔下的买主",
        "scenes": [
            {"id": "t1", "title": "塔下",
             "situation": "天黑了，塔下的小路上来了一辆没有灯的马车。",
             "brief": {"present": "四个人都躲在塔门后；马车夫在车边等信号",
                       "span": "距上一场两天",
                       "offstage": "买主还在车里，没有露面"},
             "promises": ["q1"],
             "beat_notes": {"q1": "马车到了塔下，买主还藏在车里没现身"},
             "world": {"location": "塔下小路", "date": "1492-09-19",
                       "period": "夜里"}},
        ],
        "acts": [{"id": "a1", "title": "夜会", "scenes": ["t1"]}],
        "promises": [{"id": "q1", "title": "马车里的买主", "tag": "setup_payoff",
                      "status": "open", "summary": "买主是谁、买什么"}],
    }


def make_loop(names=("甲", "乙")):
    st = StateTree.from_template(
        {n: new_character(n, level=3, job="战士", abilities=AB,
                          hp_max=24, ac=15) for n in names},
        current=names[0])
    st.commit([{"op": "insert", "path": f"/角色列表/{n}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}} for n in names],
              actor="填表", reason="装备")
    led = Ledger()
    seats = {n: FakeModel(name=f"{n}-m") for n in names}
    table = TableClient(dm=FakeModel(name="dm-m"),
                        planner=FakeModel(name="pl-m"), players=seats)
    loop = GameLoop(st, led, table, rng=Random(20261107))
    loop.set_field([make_hero_unit(st, n) for n in names])
    return st, led, loop


# --------------------------------------------------------------------------- #
section("A. beat 内容：记「推进成了什么样」，不是只记场次")

adv = load_adventure(SAMPLE)
ok(adv.scene("s2").beat_notes.get("p2", "").startswith("发现一只空木箱"),
   "A1 示例剧本的 beat_notes 读进来了",
   adv.scene("s2").beat_notes)

prog = CampaignProgress()
prog.record_scene(adv.scene("s2"))
beat = prog.beats_of("p2")[0]
ok("木茬是新的" in beat.note and "场景《" not in beat.note,
   "A2 ⭐ 记的是内容，不是「场景《标题》」", beat.line())

text = prog.debt_text(adv)
ok("上一局欠着的伏笔" in text and "木茬是新的" in text,
   "A3 ⭐ 欠账块里带得上「上次推进成了什么样」", text)

prog.record_scene(adv.scene("s2"))
ok(len(prog.beats_of("p2")) == 1, "A4 同一场重跑不重复记")

# 没写 beat_notes 的场景：退化成场景名，不报错
d = doc()
d["scenes"][0].pop("beat_notes")
alt = parse_adventure(d)
pp = CampaignProgress()
pp.record_scene(alt.scene("t1"))
ok("场景《塔下》" in pp.beats_of("q1")[0].note,
   "A5 没写内容时退化成场景名（至少知道哪一场）")

# 校验
d = doc()
d["scenes"][0]["beat_notes"] = {"q9": "写了不属于本场的伏笔"}
errs, _w = parse_adventure(d).validate()
ok(any("不在本场 promises 里" in e for e in errs),
   "A6 ⭐ beat_notes 指向别的伏笔 → 报错", errs)

d = doc()
d["scenes"][0]["beat_notes"] = {"q1": "推进关系"}
errs, _w = parse_adventure(d).validate()
ok(any("推进内容太泛" in e for e in errs),
   "A7 ⭐ beat_notes 写空泛词 → 报错", errs)

d = doc()
d["scenes"][0]["beat_notes"] = {"q1": 123}
ok(raises(lambda: parse_adventure(copy.deepcopy(d))),
   "A8 beat_notes 值不是字符串 → 解析期报错")

d = doc()
d["scenes"][0]["beat_notes"] = ["q1"]
ok(raises(lambda: parse_adventure(copy.deepcopy(d))),
   "A9 beat_notes 不是对象 → 解析期报错")


# --------------------------------------------------------------------------- #
section("B. 战役文件：结构 / 路径 / 坏引用")

camp = load_campaign(SAMPLE_CAMPAIGN)
ok(camp.title and len(camp.chapters) == 1
   and camp.chapters[0].id == "ch1",
   "B1 示例战役文件读得出来", camp.title)

raw = {"title": "x", "chapters": []}
ok(raises(lambda: parse_campaign(raw), CampaignError),
   "B2 没有章节 → 报错")
raw = {"title": "x", "chapters": [{"id": "a", "title": "A", "adventure": "a.json"},
                                  {"id": "a", "title": "B", "adventure": "b.json"}]}
ok(raises(lambda: parse_campaign(raw), CampaignError),
   "B3 章节 id 重复 → 报错")
raw = {"title": "x", "chapters": [{"id": "a", "title": "A"}]}
ok(raises(lambda: parse_campaign(raw), CampaignError),
   "B4 章节缺少 adventure 文件 → 报错")

tmp = tempfile.mkdtemp(prefix="dnd-camp-")
try:
    bad = os.path.join(tmp, "bad.campaign.json")
    json.dump({"title": "x", "chapters": [
        {"id": "a", "title": "A", "adventure": "不存在.json"}]},
        open(bad, "w", encoding="utf-8"), ensure_ascii=False)
    ok(raises(lambda: load_campaign(bad), CampaignError),
       "B5 章节剧本不存在 → 加载期报错（不等到开局）")

    bad2 = os.path.join(tmp, "bad2.campaign.json")
    json.dump({"title": "x", "chapters": [
        {"id": "a", "title": "A", "adventure": "../哨塔的拖痕.json"}]},
        open(bad2, "w", encoding="utf-8"), ensure_ascii=False)
    ok(raises(lambda: load_campaign(bad2), CampaignError),
       "B6 路径指到不合法剧本 → 报错")
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
section("C. 跨章欠账：上一章欠的，下一章开局交给 DM")

tmp = tempfile.mkdtemp(prefix="dnd-camp2-")
try:
    ch1 = os.path.join(tmp, "ch1.json")
    shutil.copyfile(SAMPLE, ch1)
    ch2 = os.path.join(tmp, "ch2.json")
    json.dump(doc(), open(ch2, "w", encoding="utf-8"), ensure_ascii=False)
    camp_path = os.path.join(tmp, "campaign.json")
    json.dump({"title": "测试战役", "chapters": [
        {"id": "ch1", "title": "哨塔", "adventure": "ch1.json"},
        {"id": "ch2", "title": "塔下", "adventure": "ch2.json"},
    ]}, open(camp_path, "w", encoding="utf-8"), ensure_ascii=False)

    camp = load_campaign(camp_path)
    ok([c.id for c in previous_chapters(camp, "ch2")] == ["ch1"],
       "C1 ch2 的前序章节是 ch1")
    ok(previous_chapters(camp, "ch1") == [], "C2 第一章前面没有章节")

    # 跑完第一章（假模型，零花费），进度写在自己旁边
    st, led, loop = make_loop()
    adv1 = load_adventure(ch1)
    prog = CampaignProgress(adventure=adv1.title,
                            path=progress_path_for(ch1))
    AdventureRunner(loop, adv1, progress=prog).run_all()

    rows = debts(camp, "ch2")
    ids = [r["id"] for r in rows]
    ok("p2" in ids and "p3" in ids, "C3 ⭐ 上一章欠的伏笔进了下一章", ids)
    ok("p1" not in ids, "C4 已兑现的 p1 不再算欠账", ids)
    p2 = next(r for r in rows if r["id"] == "p2")
    ok("木茬是新的" in p2["last_note"] and p2["chapter"] == "ch1",
       "C5 ⭐ 欠账带着上次推进的内容与出处", p2)

    limit_rows = debts(camp, "ch2", limit=1)
    ok(len(limit_rows) == 1, "C6 limit 生效", len(limit_rows))

    text = debt_text(camp, "ch2")
    ok("上一章还欠着的伏笔" in text and "《哨塔》" in text
       and "木茬是新的" in text,
       "C7 ⭐ 欠账块写明章节、内容与摘要", text)
    ok(debt_text(camp, "ch1") == "", "C8 第一章开局没有跨章欠账")

    # 第二章开跑：欠账进 DM 提示，玩家提示词里没有
    class Capture(FakeModel):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.reqs = []

        def _complete(self, req):
            self.reqs.append(req)
            return super()._complete(req)

    st2 = StateTree.from_template(
        {n: new_character(n, level=3, job="战士", abilities=AB,
                          hp_max=24, ac=15) for n in ("甲", "乙")},
        current="甲")
    st2.commit([{"op": "insert", "path": f"/角色列表/{n}/物品/长剑",
                 "value": {"类别": "武器", "已装备": True}}
                for n in ("甲", "乙")], actor="填表", reason="装备")
    dm = Capture(name="dm-m")
    seats = {"甲": Capture(name="甲-m"), "乙": Capture(name="乙-m")}
    loop2 = GameLoop(st2, Ledger(),
                     TableClient(dm=dm, planner=FakeModel(), players=seats),
                     rng=Random(20261108))
    loop2.set_field([make_hero_unit(st2, n) for n in ("甲", "乙")])
    adv2 = load_adventure(ch2)
    AdventureRunner(loop2, adv2, campaign_debt=debt_text(camp, "ch2")
                    ).run_all()

    dm_p = next(r.prompt for r in dm.reqs if r.task == "narrate")
    ok("上一章还欠着的伏笔" in dm_p and "上一章欠的" not in dm_p,
       "C9 ⭐ 跨章欠账进了 DM 的任务说明")
    ok("木茬是新的" in dm_p, "C10 欠账内容也在 DM 提示里")
    player_prompts = [r.prompt for cli in seats.values() for r in cli.reqs]
    ok(player_prompts and all("上一章还欠着的伏笔" not in p
                              for p in player_prompts),
       "C11 ⭐ 跨章欠账一个字都没进玩家提示词")
    ok(all("木茬是新的" not in p for p in player_prompts),
       "C12 欠账内容也没进玩家提示词")
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
print("\n=== G. 章节休整（Q35）：rest 只认 \"\" / \"long\" ===")

from engine.adventure.campaign import CampaignError, parse_campaign  # noqa: E402

_base = {"title": "x", "chapters": [
    {"id": "c1", "title": "一", "adventure": "a.json"},
    {"id": "c2", "title": "二", "adventure": "b.json", "rest": "long"},
]}
_camp = parse_campaign(_base)
_G1 = [c.rest for c in _camp.chapters] == ["", "long"]
ok(_G1, "G1 ⭐ 章节 rest 能读（缺省 = 不休）",
   [c.rest for c in _camp.chapters])
try:
    parse_campaign({"title": "x", "chapters": [
        {"id": "c1", "title": "一", "adventure": "a.json", "rest": "short"}]})
    _raised = False
except CampaignError:
    _raised = True
ok(_raised, "G2 ⭐ 不认识的 rest 直接报错（不猜）")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")