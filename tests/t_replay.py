# coding=utf-8
"""P5-B3 回归：**回到某一场重跑**（前面的不重放、进度不倒退、不重复记）。

守的口径：

    · 只能从"前面都已跑完"的场景开始；否则报错，不静默跳过；
    · 重跑只动运行态 `scenes_done`，进度文件（beat/兑现）是历史，不倒退；
    · 重跑不重复记 beat、不重复造记忆（幂等键）；
    · 重跑期间不再塞"上一局欠账"；
    · 重跑后存档、读档续跑不重复跑已完成的场。
"""

from __future__ import annotations

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
    AdventureError, AdventureRunner, CampaignProgress, load_adventure,
    parse_adventure,
)
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.persist import load_game, save_game  # noqa: E402

FAILS: list[str] = []
COUNT = 0
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
    scenes = []
    for i, (sid, title, promise, place) in enumerate((
            ("s1", "塔外", "p1", "塔外"), ("s2", "塔内", "p2", "塔内"),
            ("s3", "塔顶", "p2", "塔顶")), 1):
        scenes.append({
            "id": sid, "title": title,
            "situation": f"这是第 {i} 场：{title}。",
            "brief": {"present": "四个人都在", "span": "接着上一场",
                      "offstage": "楼下暂时没人上来"},
            "promises": [promise],
            "beat_notes": {promise: f"第 {i} 场把 {promise} 推进了一步"},
            "world": {"location": place, "date": f"1492-09-{16 + i}",
                      "period": "午后"},
        })
    return {
        "title": "重跑测试",
        "scenes": scenes,
        "acts": [{"id": "a1", "title": "一幕", "scenes": ["s1", "s2", "s3"]}],
        "promises": [
            {"id": "p1", "title": "伏笔一", "tag": "setup_payoff",
             "status": "open", "summary": "第一场的悬念"},
            {"id": "p2", "title": "伏笔二", "tag": "mystery",
             "status": "open", "summary": "第二场的悬念"},
        ],
    }


def make_loop() -> tuple[StateTree, Ledger, GameLoop]:
    st = StateTree.from_template(
        {n: new_character(n, level=3, job="战士", abilities=dict(AB),
                          hp_max=24, ac=15) for n in ("甲", "乙")},
        current="甲")
    st.commit([{"op": "insert", "path": f"/角色列表/{n}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}}
               for n in ("甲", "乙")], actor="填表", reason="装备")
    led = Ledger()
    table = TableClient(dm=FakeModel(name="dm"), planner=FakeModel(name="pl"),
                        players={"甲": FakeModel(name="甲"),
                                 "乙": FakeModel(name="乙")})
    loop = GameLoop(st, led, table, rng=Random(20261111))
    loop.set_field([make_hero_unit(st, n) for n in ("甲", "乙")])
    return st, led, loop


def new_runner(progress=None):
    st, led, loop = make_loop()
    adv = parse_adventure(doc())
    return AdventureRunner(loop, adv, progress=progress or CampaignProgress()), loop


# --------------------------------------------------------------------------- #
section("A. 前置条件：前面没跑完不能从中间开始")

runner, loop = new_runner()
ok(raises(lambda: runner.run_from("不存在"), AdventureError),
   "A1 不存在的场景 → 报错")
ok(raises(lambda: runner.run_from("s2"), AdventureError),
   "A2 ⭐ 前面没跑完 → 拒绝，不静默跳过")

ok(raises(lambda: runner.run_from("s2"), AdventureError), "A3 仍然拒绝")


# --------------------------------------------------------------------------- #
section("B. 从中间重跑：只跑这一场到结尾")

progress = CampaignProgress()
runner, loop = new_runner(progress)
runner.run_all()
ok(loop.scenes_done == ["s1", "s2", "s3"], "B1 先跑完三场", loop.scenes_done)
beats_before = {p: len(progress.beats_of(p)) for p in ("p1", "p2")}
mem_before = len(loop.memory._entries) if loop.memory else 0

logs = runner.run_from("s2")
ok(len(logs) == 2, "B2 ⭐ 只重跑 s2、s3 两场（不含 s1）", len(logs))
ok(loop.story_scene == "s3", "B3 最后停在第 s3 场", loop.story_scene)
ok(loop.scenes_done == ["s1", "s2", "s3"],
   "B4 scenes_done 不重复（重跑不是追加）", loop.scenes_done)
beats_after = {p: len(progress.beats_of(p)) for p in ("p1", "p2")}
ok(beats_after == beats_before,
   "B5 ⭐ 重跑不重复记 beat（进度是历史，不倒退也不叠加）",
   (beats_before, beats_after))
if loop.memory:
    ok(len(loop.memory._entries) == mem_before,
       "B6 重跑不重复造记忆（幂等键）", len(loop.memory._entries))
else:
    ok(True, "B6 没有记忆库（跳过）")

# 开局块（跨局欠账）现在靠 `_opening_done` 管，不再靠 `_first` 兼职：
# 新一局的第一场要带，同一局重跑第一场不重复带，非首场从不带。
adv_b = parse_adventure(doc())
_marker = progress.debt_text(adv_b)
ok(bool(_marker), "B7 前置：这一局确实有开局欠账块可塞", _marker[:40])
fresh, _loop2 = new_runner(progress)
ok(_marker in fresh.dm_hint(adv_b.scenes[0]),
   "B7a ⭐ 新一局的首场带开局块")
ok(_marker not in fresh.dm_hint(adv_b.scenes[1]),
   "B7b ⭐ 非首场从不带开局块")
fresh.run_all()
ok(_marker not in fresh.dm_hint(adv_b.scenes[0]),
   "B7c ⭐ 同一局重跑第一场不再重复塞开局块（_opening_done 管住）")


# --------------------------------------------------------------------------- #
section("C. 从头重跑：整条线可以重打")

logs = runner.run_from("s1")
ok(len(logs) == 3, "C1 从 s1 重跑 → 三场都跑", len(logs))
ok(loop.scenes_done == ["s1", "s2", "s3"], "C2 完成列表仍正确", loop.scenes_done)


# --------------------------------------------------------------------------- #
section("D. 重跑后存档：续跑不重复")

tmp = tempfile.mkdtemp(prefix="dnd-replay-")
try:
    path = os.path.join(tmp, "replay.json")
    save_game(path, state=loop.state, ledger=loop.ledger, loop=loop)
    sess = load_game(path)
    adv = parse_adventure(doc())
    runner2 = AdventureRunner(sess.loop, adv)
    before = len(runner2.logs)
    runner2.run_all(resume=True)
    ok(len(runner2.logs) == before,
       "D1 ⭐ 读档后续跑跳过已完成的场（不重复打）", len(runner2.logs) - before)
    ok(sess.loop.scenes_done == ["s1", "s2", "s3"],
       "D2 完成列表随档往返", sess.loop.scenes_done)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
