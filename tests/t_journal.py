# coding=utf-8
"""任务日志 / 结算回执：只读投影的边界。

守四件事：

    · 只出现"当前一场"和"已完成的场"，未跑的场不剧透；
    · 线索只出现**已发现**的，没发现的连文字都不出现；
    · 回执只记完成/线索/收线，不编金币/经验；
    · 页面有任务入口和回执区。
"""

from __future__ import annotations

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import (  # noqa: E402
    Act, Adventure, Brief, JOURNAL_NOTE, Promise, Scene, build_journal,
)
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.webui import PAGE_HTML  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


print("=== A. 任务日志只读投影 ===")
sc1 = Scene(id="s1", title="哨塔的拖痕", situation="进入哨塔，查清箱子被拖到哪里。",
            brief=Brief(present="守卫在塔顶", span="昨夜", offstage="买主在镇上等"),
            pays=["p1"])
sc2 = Scene(id="s2", title="塔下的买主", situation="顺着拖痕找到镇上的买主。",
            brief=Brief(present="买主在酒馆", span="第二天", offstage="守卫在塔顶"))
sc3 = Scene(id="s3", title="回程", situation="带着证据回城复命。",
            brief=Brief(present="城门口", span="三天后", offstage="无人"))
adv = Adventure(title="测试剧本", scenes=[sc1, sc2, sc3],
                acts=[Act(id="a1", title="第一幕", scenes=["s1", "s2"])],
                promises=[Promise(id="p1", title="谁拖走了箱子",
                                  summary="箱子被拖上塔顶")])

st = StateTree.from_template({"甲": new_character("甲", level=3)})
loop = GameLoop(st, Ledger(), FakeModel())
loop.world.register_clue("c1", scene="s1", place="哨塔", skill="investigation",
                         dc=12, text="地上有新鲜的拖痕。", secret=True)
loop.world.register_clue("c2", scene="s2", place="镇上", skill="perception",
                         dc=14, text="买主的袖口有塔灰。", secret=True)
loop.world.mark_clue_found("c1", by="甲")

j = build_journal(adv, loop)
blob = json.dumps(j, ensure_ascii=False)
ok(j["title"] == "测试剧本" and j["completed"] == 0,
   "A1 还没跑完时 completed=0", j["completed"])
ok(j["current"] and j["current"]["scene"] == "s1",
   "A2 当前目标是第一场", j["current"])
ok(not j["receipts"], "A3 没跑完就没有回执")
ok(j["current"]["clues"] == [{"id": "c1", "text": "地上有新鲜的拖痕。"}],
   "A4 当前场只带已发现线索，且不混入 DM 简报", j["current"]["clues"])

loop.mark_scene_done("s1")
j = build_journal(adv, loop)
blob = json.dumps(j, ensure_ascii=False)
ok(j["completed"] == 1 and j["current"]["scene"] == "s2",
   "A5 跑完 s1 后当前推进到 s2")
ok(len(j["receipts"]) == 1 and j["receipts"][0]["scene"] == "s1",
   "A6 出现 s1 的结算回执")
ok(j["receipts"][0]["clues"] == [{"id": "c1", "text": "地上有新鲜的拖痕。"}],
   "A7 回执只带已发现线索", j["receipts"][0]["clues"])
ok(j["receipts"][0]["closed"] == [{"id": "p1", "title": "谁拖走了箱子"}],
   "A8 回执带本场收掉的伏笔")
ok("买主的袖口有塔灰" not in blob, "A9 未发现线索不泄露")
ok("回程" not in blob, "A10 未跑的 s3 标题不泄露")
ok("在场与主动性" not in blob and "买主在酒馆" not in blob,
   "A11 不把 DM 简报带进玩家任务日志")
ok("金币" in j["note"] and "经验" in j["note"] and "不虚报" in j["note"],
   "A12 回执口径明确：没有登记奖励就不写")

loop.mark_scene_done("s2")
j = build_journal(adv, loop)
ok([r["scene"] for r in j["receipts"]] == ["s2", "s1"],
   "A13 回执最近的先看", [r["scene"] for r in j["receipts"]])
ok(j["current"]["scene"] == "s3", "A14 当前推进到 s3")

loop.mark_scene_done("s3")
j = build_journal(adv, loop)
ok(j["all_done"] and j["current"] is None and j["completed"] == 3,
   "A15 全部跑完：没有当前目标，完成数为 3")

print("\n=== B. 页面入口 ===")
ok('data-panel="quests"' in PAGE_HTML and 'id="quests-body"' in PAGE_HTML,
   "B1 有任务入口和任务日志弹层")
ok("renderQuests" in PAGE_HTML and "结算回执" in PAGE_HTML,
   "B2 页面会渲染任务与回执")
ok("q.note" in PAGE_HTML and "回执只记已发生的事实" in JOURNAL_NOTE,
   "B3 页面会显示服务端给的回执口径")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")