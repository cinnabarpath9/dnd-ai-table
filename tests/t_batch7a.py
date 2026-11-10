# coding=utf-8
"""Batch 7A 回归：存档版本/BOM/场景、世界清单封顶、lint 双向、verify 世界覆盖。"""

from __future__ import annotations

import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure.model import _is_generic  # noqa: E402
from engine.longrun import build_table_loop  # noqa: E402
from engine.persist import (  # noqa: E402
    SAVE_VERSION, dump_game, load_from_dict, save_game, load_game,
)
from engine.rules.clues import SceneClue  # noqa: E402
from engine.state import Ledger, StateTree, new_world  # noqa: E402
from engine.state.visibility import lint_leaks  # noqa: E402
from engine.state.world import World  # noqa: E402
from engine.verify_isolation import run_all_checks  # noqa: E402

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


# --------------------------------------------------------------------------- #
section("B7A-1 存档：版本 3 / BOM / 场景状态")

ok(SAVE_VERSION == 4, "B7A-1a SAVE_VERSION = 4（v3 仍可读）", SAVE_VERSION)

_st, _led, loop = build_table_loop(seed=11, names=("艾尔玛", "罗恩"))
loop.set_scene_context(scene_id="s1", clues=[
    SceneClue(id="c1", text="门口有脚印。", skill="perception", dc=12),
])
doc = loop_scene = dump_game(state=_st, ledger=_led, loop=loop)
loaded = load_from_dict(doc)
ok(loaded.loop.scene_id == "s1" and len(loaded.loop.scene_clues) == 1
   and loaded.loop.scene_clues[0].text == "门口有脚印。",
   "B7A-1b ⭐ 场景 id 与线索随档往返",
   (loaded.loop.scene_id, [c.text for c in loaded.loop.scene_clues]))

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "bom.dnd.json")
    save_game(path, state=_st, ledger=_led, loop=loop)
    raw = open(path, "rb").read()
    with open(path, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + raw)     # 记事本式 UTF-8 BOM
    try:
        sess = load_game(path)
        ok(sess.loop.round == loop.round, "B7A-1c ⭐ 带 BOM 的存档能读")
    except Exception as exc:                 # noqa: BLE001
        ok(False, "B7A-1c 带 BOM 的存档能读", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
section("B7A-2 世界清单封顶 + 未发现地点不渲染")

st = StateTree.from_template({}, world=new_world(date="1492-09-17", period="午后",
                                                 current="村口"))
w = World(st).ensure()
for i in range(25):
    name = f"地点{i:02d}"
    w.register_location(name, description=f"第 {i} 处。", region="荒原")
    w.discover(name)
player = w.render("布兰")
ok("另有 5 项更早的未列出" in player,
   "B7A-2a ⭐ 地点超过 20 条会写明折叠了多少", player[:160])
ok(player.count("  · 地点") == 20, "B7A-2b 只列最近 20 条",
   player.count("  · 地点"))
ok("【当前地点】村口" not in player,
   "B7A-2c ⭐ 未发现的当前地点不渲染给玩家", player[:120])


# --------------------------------------------------------------------------- #
section("B7A-3 lint：DM→pc 与 pc→pc 两个方向")

led = Ledger()
led.public("公告", "你们在营地外停下。", section="公告")
led.secret("公告", "队长是内应，今晚就要动手，别让玩家知道。", section="公告")
led.private("对话", "队长是内应，今晚就要动手，别让玩家知道。", "甲",
            section="对话")
problems = lint_leaks(led)
ok(any("DM 密记" in p for p in problems),
   "B7A-3a ⭐ DM 密记抄进玩家私有会被抓到", problems)

led2 = Ledger()
led2.public("公告", "你们进了地下室。", section="公告")
led2.private("检定", "你仔细看过、听过，没发现什么异常。", "甲")
led2.private("检定", "你仔细看过、听过，没发现什么异常。", "乙")
ok(lint_leaks(led2) == [],
   "B7A-3b 两个 PC 拿到同一条中性结果时不误报（pc→pc 暂不查）",
   lint_leaks(led2))

clean = Ledger()
clean.public("公告", "你们到了哨塔门口。")
clean.private("检定", "你发现墙里有个暗格。", "甲")
ok(lint_leaks(clean) == [], "B7A-3c 正常台账不误报", lint_leaks(clean))


# --------------------------------------------------------------------------- #
section("B7A-4 verify_isolation：玩家世界内容有覆盖")

problems, captures = run_all_checks()
ok(problems == [], "B7A-4a ⭐ 加世界 marker 后仍零问题", problems[:3])
ok(any("旧哨塔" in c.prompt and c.viewer == "艾尔玛" for c in captures),
   "B7A-4b 玩家包里确有已发现地点")


# --------------------------------------------------------------------------- #
section("B7A-5 剧本校验：组合空泛词 vs 正常两字")

ok(_is_generic("制造冲突，埋下伏笔"), "B7A-5a ⭐ 两个空泛词串联会被判空泛")
ok(not _is_generic("天亮"), "B7A-5b ⭐ 正常两字不被误判")
ok(not _is_generic("商队被劫，货物失踪"), "B7A-5c 正常描述通过")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
