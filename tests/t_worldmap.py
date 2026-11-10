# coding=utf-8
"""U13 回归：世界节点图（通道 / 听说 / 迷雾 / 剧本播种 / 网页图快照）。

守四件事：

    A. 世界层：通道是双向的唯一一条；听说 ≠ 去过；迷宫按视角砍；
    B. 剧本层：map 写坏了不许跑；识别不了的布尔值（"false" 字符串）要报；
    C. 推进器：地图幂等播种；跑完一场后玩家只看到"该知道的"；
    D. 网页：/api/state 带 graph；页面有 drawGraph；**页面 JS 能过语法检查**
       （2026-11-06 实测抓到过 PAGE_HTML 里 `'\\n'` 被 Python 解成真换行、
       整段脚本语法错误 —— 这条专门守它）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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

from engine.adventure import (  # noqa: E402
    AdventureError, AdventureRunner, load_adventure, parse_adventure,
    render_skeleton,
)
from engine.demo_web import graph_snapshot  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.persist import load_game, save_game  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import DM, Ledger, StateTree, World, new_character  # noqa: E402
from engine.webui import PAGE_HTML, TableServer, WebBridge  # noqa: E402

FAILS: list[str] = []
COUNT = 0
SAMPLE = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")


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


AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


def make_state() -> StateTree:
    return StateTree.from_template({
        "罗恩": new_character("罗恩", level=3, job="战士", abilities=AB,
                              hp_max=28, ac=16),
    }, current="罗恩", label="节点图测试")


# --------------------------------------------------------------------------- #
section("A. 世界层：通道是双向的、听说不等于去过")

st = make_state()
w = World(st).ensure()
w.register_location("村里", region="山脚", description="老磨坊和几户人家",
                   discovered=True)
w.register_location("荒径", region="村北", description="荒草掩映的小径")
w.register_location("塔顶", region="哨塔", description="强盗拖箱子的地方")
w.register_location("塔后", region="哨塔", description="绕塔背面的窄道",
                    secret="强盗不知道有人会从这边上来")
w.hear_about("塔顶")
w.discover("荒径", region="村北")
w.add_passage("村里", "荒径", note="出村往北的土路", known=True)
w.add_passage("荒径", "塔顶", note="塔内楼梯", known=False,
              secret="楼梯拐角有一处能藏人的凹槽")

ok(w.passage("荒径", "村里") is not None,
   "A1 ⭐ 通道不分方向（反着查也找得到）")
ok(len(w.passages()) == 2, "A2 两条通道都登记了", len(w.passages()))
ok(raises(lambda: w.add_passage("塔顶", "荒径")), "A3 重复登记（反过来写）→ 报错")
ok(raises(lambda: w.add_passage("村里", "村里")), "A4 通道连到自己 → 报错")
ok(raises(lambda: w.add_passage("村里", "不存在的地方")),
   "A5 端点没登记 → 报错（不静默新造一个地点）")
ok(w.current_location == "村里", "A6 第一个发现的地点成了当前地点",
   w.current_location)
ok(raises(lambda: w.travel("塔顶"), Exception),
   "A7 ⭐ 只听说过的地方不能直接过去")

g_dm = w.graph(DM)
ok(len(g_dm["nodes"]) == 4 and len(g_dm["edges"]) == 2,
   "A8 DM 图：四个点、两条边", (len(g_dm["nodes"]), len(g_dm["edges"])))
ok(all("secret" in n for n in g_dm["nodes"]),
   "A9 DM 图带节点秘密（只给主持人）")
ok(any(e["secret"] for e in g_dm["edges"]), "A10 DM 图带通道秘密")

g_p = w.graph("罗恩")
names = {n["name"]: n["state"] for n in g_p["nodes"]}
ok("塔后" not in names, "A11 ⭐ 玩家图里根本没有没听说过的地方", list(names))
ok(names.get("塔顶") == "heard", "A12 ⭐ 只听说过的地方是虚点（heard）", names)
ok(names.get("村里") == "current" and names.get("荒径") == "discovered",
   "A13 当前地点与去过的地点分开标", names)
ok(len(g_p["edges"]) == 1 and g_p["edges"][0]["a"] in ("村里", "荒径"),
   "A14 ⭐ 秘密通道不出现在玩家图里", g_p["edges"])
ok(all("secret" not in e for e in g_p["edges"]),
   "A15 玩家图里没有 secret 字段")
ok(g_p["fog"]["hidden"] == 1 and g_p["fog"]["heard"] == 1,
   "A16 迷雾计数：1 处没听说、1 处只听说", g_p["fog"])

rp = w.render("罗恩")
ok("【通道】" in rp and "出村往北的土路" in rp,
   "A17 玩家世界块里有已知通道")
ok("塔内楼梯" not in rp, "A18 ⭐ 秘密通道的文字不进玩家世界块")
ok("【地点】" in rp and "塔顶（只听说过）" in rp,
   "A19 只听说过的地方标成「只听说过」")
ok("塔后" not in rp, "A20 ⭐ 没听说过的地方连名字都不给玩家")
rd = w.render(DM)
ok("玩家还不知道这条路" in rd and "塔后（未发现）" in rd,
   "A21 DM 世界块标出未公开的通道与未发现的地点")
ok("🔒秘密：楼梯拐角有一处能藏人的凹槽" in rd,
   "A22 DM 能看到通道秘密")

# 听说 → 发现 的升级
w.discover("塔顶")
ok((w.location("塔顶") or {}).get("已发现") is True,
   "A23 听说过的地方可以被真正发现")
ok(w.graph("罗恩")["nodes"][[n["name"] for n in g_p["nodes"]].index("塔顶")]["state"]
   in ("discovered", "current"),
   "A24 发现后不再是虚点")

# 存档往返：通道与已听说都跟着走
tmp = tempfile.mkdtemp(prefix="dnd-map-")
try:
    path = os.path.join(tmp, "map.json")
    led = Ledger()
    loop = GameLoop(st, led, TableClient(dm=FakeModel(), planner=FakeModel(),
                                         players={}),
                    rng=Random(7))
    loop.set_field([make_hero_unit(st, "罗恩")])
    save_game(path, state=st, ledger=led, loop=loop)
    sess = load_game(path)
    w2 = World(sess.state)
    ok(len(w2.passages()) == 2, "A25 ⭐ 通道随存档往返", len(w2.passages()))
    ok(w2.graph("罗恩")["fog"] == {"hidden": 1, "heard": 0},
       "A26 迷雾状态随档往返（塔顶已发现、塔后仍未知）",
       w2.graph("罗恩")["fog"])
    ok(w2.passage("荒径", "塔顶")["秘密"] == "楼梯拐角有一处能藏人的凹槽",
       "A27 通道的秘密也存下来了")
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
section("B. 剧本层：map 写坏了不许跑")

adv = load_adventure(SAMPLE)
ok(len(adv.map["places"]) == 4 and len(adv.map["passages"]) == 4,
   "B1 示例剧本带 4 个点 / 4 条通道",
   (len(adv.map["places"]), len(adv.map["passages"])))


def document_with_map(places=None, passages=None) -> dict:
    d = {
        "title": "地图测试",
        "scenes": [
            {"id": "s1", "title": "门口",
             "situation": "你们站在旧磨坊门口，门里没有灯。",
             "brief": {"present": "甲想推门，乙想绕着看；两个人都在门口",
                       "span": "距上一场一个时辰",
                       "offstage": "磨坊主在屋里磨面，暂时没出来"},
             "world": {"location": "旧磨坊", "date": "1492-01-01",
                       "period": "傍晚"}},
        ],
        "acts": [{"id": "a1", "title": "一幕", "scenes": ["s1"]}],
        "map": {"places": places or [], "passages": passages or []},
    }
    return d


def map_errors(places=None, passages=None) -> list[str]:
    return parse_adventure(document_with_map(places, passages)).validate()[0]


def map_parse_error(places=None, passages=None) -> str:
    """解析期就报的错（布尔值/类型），返回拼起来的报错文本。"""
    try:
        parse_adventure(document_with_map(places, passages)).validate()
        return ""
    except AdventureError as exc:
        return "；".join(exc.problems)


ok(map_errors() == [], "B2 没有地图也合法（可选块）")
ok(map_errors([{"name": "旧磨坊"}], []) == [], "B3 只有点没有路 → 合法")

errs = map_errors([{"name": "旧磨坊"}],
                  [{"from": "旧磨坊", "to": "不存在的地方"}])
ok(any("没有登记" in e for e in errs), "B4 ⭐ 通道端点没登记 → 报错", errs)

errs = map_errors([{"name": "旧磨坊"}],
                  [{"from": "旧磨坊", "to": "旧磨坊"}])
ok(any("连到自己" in e for e in errs), "B5 通道连到自己 → 报错", errs)

errs = map_errors([{"name": "甲地"}, {"name": "乙地"}],
                  [{"from": "甲地", "to": "乙地"},
                   {"from": "乙地", "to": "甲地"}])
ok(any("通道重复" in e for e in errs),
   "B6 ⭐ 同一条路反着写两遍 → 报错（通道是双向的）", errs)

errs_text = map_parse_error([{"name": "甲地", "heard": "false"}], [])
ok("必须是 true/false" in errs_text,
   "B7 ⭐ heard 写成字符串 \"false\" → 报错（不静默当 True）", errs_text)

errs_text = map_parse_error([{"name": "甲地"}],
                            [{"from": "甲地", "to": "旧磨坊",
                              "known": "false"}])
ok("必须是 true/false" in errs_text,
   "B8 known 不是布尔 → 报错", errs_text)

ok("## 地图（节点图）" in render_skeleton(load_adventure(SAMPLE)),
   "B9 骨架视图里有地图一节")


# --------------------------------------------------------------------------- #
section("C. 推进器：幂等播种 + 玩家只看到该知道的")


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
    loop = GameLoop(st, led, table, rng=Random(20261106))
    loop.set_field([make_hero_unit(st, n) for n in names])
    return st, led, loop


st2, led2, loop2 = make_loop()
runner = AdventureRunner(loop2, load_adventure(SAMPLE))
runner.seed_map()
w2 = loop2.world
ok(w2.location("塔顶") is not None and not (w2.location("塔顶") or {})["已发现"],
   "C1 ⭐ 第一场前就把后面场次的地点登记好（未发现）")
ok(len(w2.passages()) == 4, "C2 四条通道都播进去了", len(w2.passages()))
runner.seed_map()
ok(len(w2.passages()) == 4, "C3 ⭐ 重复播种不叠通道（幂等）", len(w2.passages()))

runner.run_all()
g = w2.graph("罗恩")
ok(w2.current_location == "废弃哨塔", "C4 跑完三场后当前地点是哨塔",
   w2.current_location)
ok(all(n["name"] != "塔后" for n in g["nodes"]),
   "C5 ⭐ 玩家图里没有「塔后」（剧本写 heard=false）",
   [n["name"] for n in g["nodes"]])
ok(all(e["note"] != "绕塔外墙的窄道" for e in g["edges"]),
   "C6 ⭐ 秘密通道不进玩家图")
ok(any(n["name"] == "塔顶" and n["state"] in ("heard", "discovered")
       for n in g["nodes"]),
   "C7 塔顶是虚点（只听说过）", g["nodes"])

runner2 = AdventureRunner(loop2, load_adventure(SAMPLE))
runner2.seed_map()
ok(len(loop2.world.passages()) == 4,
   "C8 ⭐ 同一世界再来一个推进器，也不会重复播种")


# --------------------------------------------------------------------------- #
section("D. 网页：图快照 / 接口 / 页面脚本")


ok(graph_snapshot(None) is None, "D1 没有 loop 时图快照是 None")
g1 = graph_snapshot(loop2)
ok(g1 and "nodes" in g1 and "edges" in g1, "D2 图快照有 nodes/edges", list(g1 or {}))
ok(all("secret" not in n for n in g1["nodes"]), "D3 ⭐ 网页快照是玩家视角（没有秘密）")

bridge = WebBridge(timeout=5, snapshot_extra=lambda: {"graph": g1})
srv = TableServer(bridge, port=0).start()
try:
    with urllib.request.urlopen(srv.url + "api/state", timeout=5) as r:
        payload = json.loads(r.read().decode("utf-8"))
    ok(payload.get("graph", {}).get("nodes") == g1["nodes"],
       "D4 ⭐ /api/state 带世界节点图", list(payload))
finally:
    srv.stop()

ok('id="worldgraph"' in PAGE_HTML and "drawGraph" in PAGE_HTML,
   "D5 页面有节点图容器与渲染函数")
ok("renderMapPanel" in PAGE_HTML and "lastGraph" in PAGE_HTML,
   "D6 地图浮层用了图快照")

# D7：页面 JS 语法闸门（2026-11-06 真抓到过一次死脚本）
_js = re.search(r"<script>(.*)</script>", PAGE_HTML, re.S)
_node = shutil.which("node")
if _js and _node:
    _tmp = tempfile.mkdtemp(prefix="dnd-js-")
    try:
        _p = os.path.join(_tmp, "page.js")
        with open(_p, "w", encoding="utf-8") as fh:
            fh.write(_js.group(1))
        _r = subprocess.run([_node, "--check", _p], capture_output=True,
                            timeout=60)
        ok(_r.returncode == 0,
           "D7 ⭐ 页面 JS 过 node --check（整段脚本至少能解析）",
           _r.stderr.decode("utf-8", "replace")[-400:])
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
else:
    print("  ↷ 跳过：没装 node，不做 JS 语法检查")
    ok(True, "D7 跳过（没有 node）")

ok(raises(lambda: parse_adventure({"title": "x", "scenes": [],
                                   "map": "不是对象"}), AdventureError),
   "D8 map 不是对象 → 报错")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
