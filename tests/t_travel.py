# coding=utf-8
"""U13 剩余回归：**沿已知通道自由移动**（玩家自己走，不是 DM 摆放）。

守的口径：

    · 只能沿**已知道**的通道走；秘密通道玩家看不见也走不了；
    · 目的地必须"去过"或"只听说过"（没听说过的连路都不该知道）；
    · 到达"只听说过"的地方 → 顺手变成"去过"（走一趟就算发现）；
    · `travel()`（DM/剧本直接摆放）和 `move_to()`（玩家自己走）不是一回事；
    · 战斗中不能赶路；网页地图的"可前往"按钮走同一条校验。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.error
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

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.persist import load_game, save_game  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, World, new_character  # noqa: E402
from engine.state.world import WorldError  # noqa: E402
from engine.web_actions import ActionError, apply_action  # noqa: E402
from engine.webui import PAGE_HTML, TableServer, WebBridge  # noqa: E402

FAILS: list[str] = []
COUNT = 0
WHO = "甲"
AB = {"力量": 15, "敏捷": 12, "体质": 14, "智力": 10, "感知": 10, "魅力": 10}


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


def make_state() -> StateTree:
    st = StateTree.from_template(
        {WHO: new_character(WHO, level=3, job="战士", abilities=AB,
                            hp_max=24, ac=15)}, current=WHO)
    st.commit([{"op": "insert", "path": f"/角色列表/{WHO}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}}],
              actor="填表", reason="装备")
    return st


def seed_map(st: StateTree) -> World:
    w = World(st).ensure()
    w.discover("村里", description="老磨坊和几户人家")
    w.register_location("荒径", description="荒草掩映的小径")
    w.hear_about("荒径")                 # 听说过，没去过
    w.hear_about("塔顶")                 # 只听说过
    w.register_location("塔后", secret="绕塔背面的窄道")
    w.discover("哨塔")
    w.add_passage("村里", "荒径", note="出村往北的土路", known=True)
    w.add_passage("荒径", "塔顶", note="塔内楼梯", known=False,
                  secret="这条楼梯玩家还没发现")
    w.add_passage("村里", "塔后", note="绕塔外墙的窄道", known=True)
    return w


def make_loop(st: StateTree) -> GameLoop:
    loop = GameLoop(st, Ledger(),
                    TableClient(dm=FakeModel(), planner=FakeModel(), players={}),
                    rng=Random(20261109))
    loop.set_field([make_hero_unit(st, WHO)])
    return loop


# --------------------------------------------------------------------------- #
section("A. 世界层：只能沿已知通道走")

st = make_state()
w = seed_map(st)
ok(w.current_location == "村里", "A1 起点在村里", w.current_location)
ok(w.can_move_to("荒径")[0] is True, "A2 ⭐ 听说过的地方、有已知的路 → 能走")
ok(w.can_move_to("塔顶")[0] is False, "A3 秘密通道 → 不能走")
ok("没有路" in w.can_move_to("塔顶")[1],
   "A3b 村里到塔顶根本没有直达的路", w.can_move_to("塔顶"))
ok(w.can_move_to("塔后")[0] is False, "A4 没听说过的地方 → 不能走")
ok("还没听说过" in w.can_move_to("塔后")[1],
   "A4b 拒绝理由说清是「没听说过」", w.can_move_to("塔后"))

w.move_to("荒径")
ok(w.current_location == "荒径", "A5 ⭐ 走过来了", w.current_location)
ok((w.location("荒径") or {}).get("已发现") is True,
   "A6 ⭐ 到达「只听说过」的地方 → 变成「去过」")
ok("还不知道" in w.can_move_to("塔顶")[1],
   "A6b ⭐ 站在荒径：通往塔顶的楼梯是秘密，拒绝理由说清「路还不知道」",
   w.can_move_to("塔顶"))
ok(any("沿通道移动" in e.reason or "前往" in e.reason for e in st.audit),
   "A7 移动有审计", [e.line() for e in st.audit][-2:])

ok(w.can_move_to("村里")[0] is True,
   "A8 ⭐ 通道是双向的（回得去）")
w.move_to("村里")
ok(w.current_location == "村里", "A9 走回村里")

# 没有直达的路：村里→塔顶 没有通道（只有荒径→塔顶）
ok(w.can_move_to("塔顶")[0] is False, "A10 没有直达通道 → 不能走")
ok(raises(lambda: w.move_to("塔顶"), WorldError), "A10b 硬走 → 报错")
ok(raises(lambda: w.move_to("村里"), WorldError),
   "A11 已经在的地方 → 报错")

# travel() 是 DM 摆放，不检查通道
w.travel("哨塔")
ok(w.current_location == "哨塔",
   "A12 ⭐ DM 的 travel() 仍是直接摆放（脚本播种不受通道限制）")
ok(raises(lambda: w.move_to("荒径"), WorldError),
   "A13 哨塔与荒径之间没有路 → 玩家走不过去")


# --------------------------------------------------------------------------- #
section("B. 网页动作：赶路走同一条校验")

st = make_state()
w = seed_map(st)
loop = make_loop(st)
res = apply_action(st, loop, WHO, {"kind": "travel", "to": "荒径"})
ok(res["ok"] and w.current_location == "荒径",
   "B1 ⭐ 网页「前往」能走", res.get("message"))
ok(raises(lambda: apply_action(st, loop, WHO,
                               {"kind": "travel", "to": "塔后"}),
          ActionError),
   "B2 没听说过的地方 → ActionError")

loop.start_combat(enemies=[("Bandit", 1)], distance=20)
ok(raises(lambda: apply_action(st, loop, WHO,
                               {"kind": "travel", "to": "村里"}),
          ActionError),
   "B3 ⭐ 战斗中不能赶路")
ok(w.current_location == "荒径", "B4 被拒的动作没有改状态")

loop2 = make_loop(st)
srv = TableServer(WebBridge(timeout=2), port=0).start()
srv.bridge.action_handler = lambda p: apply_action(st, loop2, WHO, p)
try:
    req = urllib.request.Request(
        srv.url + "api/act", data=json.dumps({"kind": "travel", "to": "村里"}).encode(),
        headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
    ok(data.get("ok") and w.current_location == "村里",
       "B5 ⭐ /api/act 能赶路", data)
finally:
    srv.stop()

ok("renderTravel" in PAGE_HTML and "可前往" in PAGE_HTML,
   "B6 网页地图有「可前往」入口")
ok("kind:'travel'" in PAGE_HTML or 'kind:"travel"' in PAGE_HTML
   or "kind:'travel'" in PAGE_HTML or '"travel"' in PAGE_HTML,
   "B7 按钮会调用 travel 动作")


# --------------------------------------------------------------------------- #
section("C. 图与存档：走过去之后玩家看得见")

st = make_state()
w = seed_map(st)
w.move_to("荒径")
g = w.graph("罗恩")
states = {n["name"]: n["state"] for n in g["nodes"]}
ok(states.get("荒径") == "current", "C1 当前地点标成 current", states)
ok(states.get("村里") == "discovered", "C2 走过的村里仍是去过")
ok(states.get("塔顶") == "heard", "C3 塔顶仍是虚点（没走过）")
ok("塔后" not in states, "C4 ⭐ 没听说过的地方仍不在图里")

tmp = tempfile.mkdtemp(prefix="dnd-travel-")
try:
    loop = make_loop(st)
    path = os.path.join(tmp, "t.json")
    save_game(path, state=st, ledger=loop.ledger, loop=loop)
    sess = load_game(path)
    ok(World(sess.state).current_location == "荒径",
       "C5 ⭐ 走到哪随存档往返")
    ok((World(sess.state).location("荒径") or {}).get("已发现") is True,
       "C6 发现状态随档")
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