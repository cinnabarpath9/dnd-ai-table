# coding=utf-8
"""P5-G3 回归：**被动察觉**（站着不动也能发现）+ 换场后允许重试。

守的口径：

    · 被动值 = 10 + 察觉总加值，与主动检定用同一份加值；
    · 只查 perception 痕迹，investigation 必须主动搜索；
    · 同地点/未发现/本场没试过才自动发现；秘密只进发现者私有台账；
    · 一条痕迹只交给被动值最高的人；发现后记记忆、不重复；
    · 主动检定：同一场失败不许再掷，**换场后允许一次**。
"""

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

from engine.dice import Random  # noqa: E402
from engine.memory import MemoryStore  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.clues import SceneClue, passive_score, skill_bonus  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, World, new_character  # noqa: E402

FAILS: list[str] = []
COUNT = 0
AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 14, "魅力": 10}
TEXT = "地板上有一串新鲜脚印，一直通向楼梯口。"


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def make(passive_bonus: int = 5, names=("甲", "乙")):
    chars = {n: new_character(n, level=3, job="战士", abilities=dict(AB),
                               hp_max=24, ac=15) for n in names}
    st = StateTree.from_template(chars, current=names[0])
    units = []
    for n in names:
        u = make_hero_unit(st, n)
        u.skill_bonuses["perception"] = passive_bonus
        units.append(u)
    loop = GameLoop(st, Ledger(), FakeModel(), memory=MemoryStore())
    loop.set_field(units)
    w = World(st).ensure()
    w.discover("哨塔")
    w.travel("哨塔")
    return st, loop, units, w


def clue(cid="c1", dc=14, skill="perception", secret=True, place="哨塔"):
    return SceneClue(id=cid, text=TEXT, skill=skill, dc=dc, secret=secret,
                     place=place, source="幕后：有人拖过箱子")


def reg(w, c, scene="s1"):
    w.register_clue(c.id, scene=scene, place=c.place, skill=c.skill,
                    dc=c.dc, text=c.text, secret=c.secret,
                    source=c.source)


# --------------------------------------------------------------------------- #
section("A. 被动值口径")

st, loop, units, w = make(passive_bonus=5)
ok(skill_bonus(units[0], "perception") == 5, "A1 技能总加值取 skill_bonuses")
ok(passive_score(units[0]) == 15, "A2 ⭐ 被动察觉 = 10 + 加值", passive_score(units[0]))
u2 = units[1]
u2.skill_bonuses.pop("perception", None)
expect2 = 10 + u2.mod("wisdom")
ok(passive_score(u2) == expect2,
   "A3 没有总加值时按属性调整值算", (passive_score(u2), expect2))


# --------------------------------------------------------------------------- #
section("B. 到达场景：不占动作自动发现")

st, loop, units, w = make(passive_bonus=5)
units[1].skill_bonuses["perception"] = 0      # 乙的被动只有 10
reg(w, clue(dc=14, secret=True))
loop.set_scene_context(scene_id="s1", clues=[clue(dc=14, secret=True)])
loop.round = 1
found = loop._passive_scan(units, None)
node = w.clue("c1")
ok(bool(found) and node.get("已发现") is True,
   "B1 ⭐ 被动 15 ≥ DC 14 → 自动发现", node)
ok(node.get("发现者") == "甲", "B2 交给被动值最高的人", node.get("发现者"))
private = loop.ledger.render("甲")
other = loop.ledger.render("乙")
ok("脚印" in private, "B3 ⭐ 秘密线索进了发现者私有台账")
ok("脚印" not in other, "B4 ⭐ 别的玩家看不到")
ok(loop.memory is not None and any(
    e.source == "c1" for e in loop.memory._entries),
    "B5 发现后记了一条线索记忆")

# 同一场再扫：不重复
n_mem = len(loop.memory._entries)
again = loop._passive_scan(units, None)
ok(again == [] and len(loop.memory._entries) == n_mem,
   "B6 ⭐ 已发现的不再重复发现/记记忆")


# --------------------------------------------------------------------------- #
section("C. 不满足条件：不该自动发现")

st, loop, units, w = make(passive_bonus=0)
reg(w, clue(dc=14))
loop.set_scene_context(scene_id="s1", clues=[clue(dc=14)])
loop.round = 1
ok(loop._passive_scan(units, None) == [] and w.clue("c1")["已发现"] is False,
   "C1 ⭐ 被动 10 < DC 14 → 发现不了")

st, loop, units, w = make(passive_bonus=5)
reg(w, clue(dc=10, skill="investigation"))
loop.set_scene_context(scene_id="s1",
                       clues=[clue(dc=10, skill="investigation")])
loop.round = 1
ok(loop._passive_scan(units, None) == [],
   "C2 ⭐ 调查类痕迹必须主动搜索，被动察觉不管")

st, loop, units, w = make(passive_bonus=5)
reg(w, clue(dc=14))
w.mark_clue_tried("c1", "甲", scene="s1")
w.mark_clue_tried("c1", "乙", scene="s1")
loop.set_scene_context(scene_id="s1", clues=[clue(dc=14)])
loop.round = 1
ok(loop._passive_scan(units, None) == [],
   "C3 ⭐ 同一场已经主动试过 → 不再被动发现（不反复给）")

st, loop, units, w = make(passive_bonus=5)
reg(w, clue(dc=14))
w.mark_clue_tried("c1", "甲", scene="s0")
loop.set_scene_context(scene_id="s1", clues=[clue(dc=14)])
loop.round = 1
ok(bool(loop._passive_scan(units, None)) and w.clue("c1")["已发现"],
   "C4 ⭐ 换场了（条件变化）→ 被动察觉可以再看见")

# 地点不符：别的房间的线索不该在这发现
st, loop, units, w = make(passive_bonus=5)
w.register_location("塔顶")
reg(w, clue(dc=10, place="塔顶"))
loop.set_scene_context(scene_id="s1", clues=[clue(dc=10, place="塔顶")])
loop.round = 1
ok(loop._passive_scan(units, None) == [],
   "C5 不在当前地点 → 不发现", loop.world.current_location)

# 公开线索：进公开台账
st, loop, units, w = make(passive_bonus=5)
reg(w, clue(dc=10, secret=False))
loop.set_scene_context(scene_id="s1", clues=[clue(dc=10, secret=False)])
loop.round = 1
found = loop._passive_scan(units, None)
public = loop.ledger.render("乙")
ok(bool(found) and "脚印" in public,
   "C6 ⭐ 公开线索一发现就全场可见")


# --------------------------------------------------------------------------- #
section("D. 主动检定：同场不许重掷，换场可再试")

st, loop, units, w = make(passive_bonus=0)
w.register_clue("c2", scene="s1", place="哨塔", skill="perception",
                dc=30, text=TEXT, secret=True)
loop.set_scene_context(scene_id="s1", clues=[clue(cid="c2", dc=30)])
loop.round = 1
ok(loop._eligible_clue(units[0], "look") is not None,
   "D1 还没试过 → 可以主动观察")
loop._resolve_look_search(units[0], "look")
ok(w.clue_tried_by("c2") == ["甲"], "D2 失败记一次尝试")
ok(w.clue_tried_scene("c2", "甲") == "s1", "D3 ⭐ 记下「是在哪一场试的」",
   w.clue_tried_scene("c2", "甲"))
ok(loop._eligible_clue(units[0], "look") is None,
   "D4 ⭐ 同一场不再给同一人掷（不许反复掷到成功）")

loop.set_scene_context(scene_id="s2", clues=[clue(cid="c2", dc=30)])
ok(loop._eligible_clue(units[0], "look") is not None,
   "D5 ⭐ 换场后条件变了 → 允许再试一次")

# 旧存档没有"尝试场"：按旧口径（试过就不再试）
st, loop, units, w = make(passive_bonus=0)
w.register_clue("c3", scene="s1", place="哨塔", skill="perception",
                dc=30, text=TEXT)
w.state.commit([{"op": "replace", "path": "/世界/线索/c3/尝试",
                 "value": ["甲"]}], actor="旧档", reason="旧格式")
loop.set_scene_context(scene_id="s1", clues=[clue(cid="c3", dc=30)])
ok(loop._eligible_clue(units[0], "look") is None,
   "D6 旧档（没记场次）仍按「试过就不再试」")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")