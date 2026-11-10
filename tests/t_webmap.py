# coding=utf-8
"""网页地图/信息架构回归：地图快照、分组静态面、桥额外状态。"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: 本机系统代理不排除回环时会吞掉 127.0.0.1 请求（偶发 502）——测试里显式绕开。
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from engine.demo_web import map_snapshot  # noqa: E402
from engine.longrun import build_table_loop  # noqa: E402
from engine.webui import PAGE_HTML, TableServer, WebBridge  # noqa: E402

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
section("B8-1 探索：没有坐标就不画地图")

_st, _led, loop = build_table_loop(seed=3, names=("A", "B", "C", "D"))
m = map_snapshot(None)
ok(m["battle"] is False, "B8-1a 无 loop → battle=False")
m2 = map_snapshot(loop)
ok(m2["battle"] is False, "B8-1b 探索阶段 → battle=False", m2)


# --------------------------------------------------------------------------- #
section("B8-2 战斗：单位/位置/先攻/状态")

loop.start_combat(enemies=[("Bandit", 2)], distance=20)
m = map_snapshot(loop)
ok(m["battle"] is True and m["round"] >= 1, "B8-2a 战斗快照成立", m.get("round"))
ids = [u["id"] for u in m["units"]]
ok(len(ids) == len(set(ids)) and len(ids) >= 6,
   "B8-2b 所有单位都在快照里", ids)
ok(all("pos" in u and len(u["pos"]) == 2 for u in m["units"]),
   "B8-2c 每个单位有二维坐标")
ok(m["order"] and all(uid in ids for uid in m["order"]),
   "B8-2d 先攻序引用真实单位", m["order"])
enemy = next(u for u in m["units"] if u["team"] == "enemy")
ok(enemy["hp"] > 0 and enemy["hp_max"] >= enemy["hp"],
   "B8-2e 敌方 HP 字段合理", enemy)


# --------------------------------------------------------------------------- #
section("B8-3 桥：地图随 state 一起下发")

bridge = WebBridge(timeout=5, snapshot_extra=lambda: {"map": m})
srv = TableServer(bridge, port=0).start()
try:
    with urllib.request.urlopen(srv.url + "api/state", timeout=5) as r:
        st = json.loads(r.read().decode("utf-8"))
    ok(st.get("map", {}).get("battle") is True,
       "B8-3a ⭐ /api/state 带 battle 地图", list(st))
    ok("units" in st.get("map", {}), "B8-3b 地图含单位")
finally:
    srv.stop()


# --------------------------------------------------------------------------- #
section("B8-4 页面信息架构：分组 / 不可用折叠 / 地图容器")

ok("GROUP_ORDER" in PAGE_HTML and "groupOf" in PAGE_HTML,
   "B8-4a ⭐ 选项分组逻辑存在")
ok("暂不可用" in PAGE_HTML and "details" in PAGE_HTML,
   "B8-4b 不可用项折叠")
ok('id="battlemap"' in PAGE_HTML and "drawMap" in PAGE_HTML
   and 'data-panel="map"' in PAGE_HTML,
   "B8-4c ⭐ 地图浮层与渲染函数存在")
ok("hotkeys" in PAGE_HTML and "keydown" in PAGE_HTML,
   "B8-4d 数字键快捷键")


# --------------------------------------------------------------------------- #
section("B8-5 状态/背包浮层 + 角色快照")

from engine.demo_web import sheet_snapshot  # noqa: E402

sheet = sheet_snapshot(loop, "A")["sheet"]
ok(sheet and sheet["name"] == "A", "B8-5a 角色快照有名字", sheet)
ok(sheet["hp_max"] > 0 and sheet["ac"] > 0, "B8-5b HP/AC 字段",
   (sheet["hp_max"], sheet["ac"]))
ok(isinstance(sheet["items"], list), "B8-5c 背包列表存在", sheet["items"])
ok("abilities" in sheet and "slots" in sheet and "conditions" in sheet,
   "B8-5d 属性/法术位/状态效果字段齐全")
ok(all(f'data-panel="{k}"' in PAGE_HTML
       for k in ("sheet", "inventory", "map", "settings")),
   "B8-5e ⭐ 四个开关按钮都在")
ok("openPanel" in PAGE_HTML and "renderInventory" in PAGE_HTML,
   "B8-5f 浮层打开/背包渲染函数存在")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
