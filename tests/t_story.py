# coding=utf-8
"""Batch B 回归：章节/场景进度进 GameLoop + 存档 + 续跑。"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import AdventureRunner, load_adventure  # noqa: E402
from engine.longrun import build_table_loop  # noqa: E402
from engine.persist import dump_game, load_from_dict  # noqa: E402

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


ADV = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")
adv = load_adventure(ADV)
st, led, loop = build_table_loop(seed=5, names=("A", "B", "C", "D"))
runner = AdventureRunner(loop, adv)

# --------------------------------------------------------------------------- #
section("B10-1 进场景记进度")

ok(loop.adventure_title == adv.title, "B10-1a 剧本标题进 loop", loop.adventure_title)
first = adv.scenes[0]
runner.run_scene(first)
ok(loop.story_scene == first.id, "B10-1b ⭐ 当前场景记下了", loop.story_scene)
ok(first.id in loop.scenes_done, "B10-1c 已跑场景记下了", loop.scenes_done)
expected_act = next((a.id for a in adv.acts if first.id in a.scenes), "")
ok(loop.story_act == expected_act, "B10-1d 当前幕记下了", loop.story_act)
ok(loop.story_status in ("running", "done"),
   "B10-1e 状态是 running/done", loop.story_status)


# --------------------------------------------------------------------------- #
section("B10-2 存档往返")

doc = dump_game(state=st, ledger=led, loop=loop)
sess = load_from_dict(doc)
lp = sess.loop
ok(lp.adventure_title == loop.adventure_title, "B10-2a 标题随档")
ok(lp.story_scene == first.id and lp.story_act == loop.story_act,
   "B10-2b ⭐ 当前幕/场随档", (lp.story_act, lp.story_scene))
ok(lp.scenes_done == loop.scenes_done, "B10-2c 已跑场景随档", lp.scenes_done)
ok(lp.story_status == loop.story_status, "B10-2d 状态随档", lp.story_status)


# --------------------------------------------------------------------------- #
section("B10-3 读档续跑：跳过已跑完的场")

runner2 = AdventureRunner(lp, adv)
ok(runner2.scenes_done == [first.id],
   "B10-3a 新推进器认领已跑场景", runner2.scenes_done)
logs = runner2.run_all(resume=True)
ok(len(runner2.scenes_done) == len(adv.scenes),
   "B10-3b ⭐ 续跑后全部场景都完成", runner2.scenes_done)
ok(lp.story_status == "done", "B10-3c 跑完状态 done", lp.story_status)
ok(lp.story_scene == adv.scenes[-1].id,
   "B10-3d 当前场停最后一场", lp.story_scene)
ok(all(sid in runner2.scenes_done for sid in [s.id for s in adv.scenes]),
   "B10-3e 没有漏场")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")