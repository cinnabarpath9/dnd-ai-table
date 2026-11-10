# coding=utf-8
"""Batch D 回归：网页存档 / 读档 / 重开（同一进程换 loop）。"""

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

from engine.adventure import load_adventure  # noqa: E402
from engine.demo_web import WebSession  # noqa: E402
from engine.webui import WebBridge  # noqa: E402

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
td = tempfile.TemporaryDirectory()
bridge = WebBridge(timeout=5)
session = WebSession(bridge, adv, save_dir=td.name)

# --------------------------------------------------------------------------- #
section("D-1 新开一局")

out = session.action({"kind": "new_game"})
ok(out.get("ok") is True, "D-1a 新开一局返回 ok")
ok(session.loop is not None and session.loop.story_status in ("running", ""),
   "D-1b 有 loop 且状态正常", session.loop.story_status if session.loop else None)
hp0 = session.state.read("/角色列表/布兰/生命值/当前")
ok(hp0 == 24, "D-1c 新局 HP 24", hp0)

# --------------------------------------------------------------------------- #
section("D-2 存档")

session.loop.set_story_position(scene="s1", act="a1", status="running")
session.loop.mark_scene_done("s1")
session.state.commit([{"op": "delta", "path": "/角色列表/布兰/生命值/当前",
                       "value": -4}], actor="测试", reason="存档前改血")
out = session.action({"kind": "save_game"})
ok(out.get("ok") is True, "D-2a 存档返回 ok", out)
latest = os.path.join(td.name, "latest.json")
ok(os.path.exists(latest), "D-2b latest.json 落盘", latest)

# 存档后继续改，读档应回到存档点
session.state.commit([{"op": "replace", "path": "/角色列表/布兰/生命值/当前",
                       "value": 1}], actor="测试", reason="存档后改血")
session.loop.scenes_done.clear()

# --------------------------------------------------------------------------- #
section("D-3 读档")

out = session.action({"kind": "load_game"})
ok(out.get("ok") is True, "D-3a 读档返回 ok", out)
ok(session.state.read("/角色列表/布兰/生命值/当前") == 20,
   "D-3b ⭐ 回到存档时的 HP（20）",
   session.state.read("/角色列表/布兰/生命值/当前"))
ok("s1" in session.loop.scenes_done, "D-3c 剧本进度也回来了",
   session.loop.scenes_done)

# --------------------------------------------------------------------------- #
section("D-4 重开")

out = session.action({"kind": "new_game"})
ok(out.get("ok") is True, "D-4a 重开返回 ok")
ok(session.state.read("/角色列表/布兰/生命值/当前") == 24,
   "D-4b ⭐ 重开后 HP 回满")
ok(session.loop.scenes_done == [], "D-4c 进度清零", session.loop.scenes_done)


# --------------------------------------------------------------------------- #
try:
    session._stop_old()
    bridge.finish()
finally:
    td.cleanup()

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")