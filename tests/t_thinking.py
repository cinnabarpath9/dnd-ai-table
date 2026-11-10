# coding=utf-8
"""思考提示：调用钩子 → bridge 状态 → 快照 → 页面开关。"""
import json
import os
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.models import ModelRequest, TableClient  # noqa: E402
from engine.models.client import FakeModel  # noqa: E402
from engine.webui import PAGE_HTML, TableServer, WebBridge  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond, label, extra=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(label)
        print(f"  ✗ {label}  extra={extra!r}")


# ---- A. 客户端钩子 ----
events = []


class Speedy(FakeModel):
    pass


seat = Speedy()
seat.on_call = lambda actor, task, phase: events.append((actor, task, phase))
seat.complete(ModelRequest(role="dm", actor="dm", task="narrate",
                           prompt="x", expect="narration"))
ok(events == [("dm", "narrate", "start"), ("dm", "narrate", "end")],
   "A1 ⭐ 每次调用都发 start/end（顺序对）", events)

# 钩子抛异常不能影响调用
def boom(actor, task, phase):
    raise RuntimeError("钩子坏了")


seat2 = Speedy()
seat2.on_call = boom
reply = seat2.complete(ModelRequest(role="dm", actor="dm", task="narrate",
                                    prompt="x", expect="narration"))
ok(reply.ok, "A2 ⭐ 钩子抛异常不影响模型调用", reply.error)

# table 只发一次（不是每个座位加一遍）
table = TableClient(dm=Speedy(), planner=Speedy(),
                    players={"甲": Speedy()})
seen = []
table.on_call = lambda actor, task, phase: seen.append((actor, phase))
table.complete(ModelRequest(role="player", actor="甲", task="combat_turn",
                            prompt="x", expect="intent"))
ok(seen == [("甲", "start"), ("甲", "end")], "A3 ⭐ TableClient 只发一轮",
   seen)

# ---- B. bridge 状态 + 快照 ----
bridge = WebBridge(print_log=False)
bridge.think("布兰", "narrate", "start")
snap = bridge.snapshot(0)
ok(snap.get("thinking") and snap["thinking"]["seat"] == "布兰",
   "B1 ⭐ 调用中快照带 thinking", snap.get("thinking"))
ok(abs(snap["thinking"]["t0"] - time.time()) < 5, "B2 t0 是当前时间")
bridge.think("布兰", "narrate", "end")
ok(bridge.snapshot(0).get("thinking") is None, "B3 调用结束清掉 thinking")

# ---- C. HTTP 里能拿到 ----
bridge2 = WebBridge(print_log=False)
bridge2.think("罗恩", "explore_action", "start")
server = TableServer(bridge2, port=0).start()
try:
    with urllib.request.urlopen(server.url + "api/state", timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    ok(data.get("thinking", {}).get("seat") == "罗恩",
       "C1 ⭐ /api/state 里有 thinking", data.get("thinking"))
finally:
    server.stop()

# ---- D. 页面开关 ----
ok("id=\"thinkToggle\"" in PAGE_HTML, "D1 顶栏有「思考提示」开关")
ok("id=\"thinkline\"" in PAGE_HTML, "D2 有专门的思考状态行")
ok("dnd_show_think" in PAGE_HTML, "D3 ⭐ 开关存 localStorage（刷新还记得）")
ok("applyThink(s.thinking)" in PAGE_HTML, "D4 轮询时应用 thinking")
ok("SHOW_THINK ? fmtThink() : ''" in PAGE_HTML,
   "D5 ⭐ 关掉后连「谁在想」都不显示")
ok("setInterval" in PAGE_HTML and "clearInterval" in PAGE_HTML,
   "D6 计时器会清（切换/结束时不留空转）")
# 真浏览器抓到过：插入代码时把 `async function poll` 的前缀吃掉，
# 整段脚本不执行（页面看着正常、轮询全停）。这条守着它。
ok("async function poll(){" in PAGE_HTML,
   "D7 ⭐ poll 仍是 async（await 只能在 async 函数里）")
ok(PAGE_HTML.count("applyThink(s.thinking)") == 1,
   "D8 ⭐ applyThink 只在 poll 里调用一次（别插错函数）",
   PAGE_HTML.count("applyThink(s.thinking)"))

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for x in FAILS:
        print(f"  ✗ {x}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")