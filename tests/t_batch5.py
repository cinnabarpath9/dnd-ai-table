# coding=utf-8
"""Batch 5 回归：日志过滤 / 任务备注 / 网页输入与来源校验。"""

from __future__ import annotations

import json
import os
import sys
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

from engine.state import StateTree, new_world  # noqa: E402
from engine.state.world import World  # noqa: E402
from engine.webui import (  # noqa: E402
    PAGE_HTML, TableServer, WebBridge, viewer_log_filter,
)

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
section("B5-1 网页日志：别人的私有行不许进这个座位")

bridge = WebBridge(timeout=5, log_filter=viewer_log_filter("布兰"))
bridge.log("【公告】你们到了哨塔。")
bridge.log("【艾尔玛·私有】你发现了石板下的钥匙。")
bridge.log("【布兰·私有】你摸到了墙里的暗格。")
texts = [x["text"] for x in bridge.snapshot()["log"]]
ok(any("石板下的钥匙" not in t for t in texts) is True,
   "B5-1a ⭐ 别人的私有发现没进日志", texts)
ok(any("墙里的暗格" in t for t in texts), "B5-1b 自己的私有保留")
ok(any("你们到了哨塔" in t for t in texts), "B5-1c 公共行保留")


# --------------------------------------------------------------------------- #
section("B5-2 任务「备注」只给 DM")

st = StateTree.from_template({}, world=new_world(date="1492-09-17", period="午后"))
w = World(st).ensure()
w.open_quest("找回货物", description="商队被劫。", objective="找回箱子",
             public=True, round=1)
w.update_quest("找回货物", note="DM 内部：货里藏着祭品", actor="DM")
player = w.render("布兰")
dm = w.render("dm")
ok("藏着祭品" not in player, "B5-2a ⭐ 玩家世界块看不到 DM 备注", player)
ok("藏着祭品" in dm, "B5-2b DM 仍看得到备注")


# --------------------------------------------------------------------------- #
section("B5-3 页面：有台词输入框、提交按钮可用逻辑")

ok('id="say"' in PAGE_HTML and "<textarea" in PAGE_HTML,
   "B5-3a ⭐ 页面有 #say 输入框")
ok("submitEl.onclick" in PAGE_HTML and "selected" in PAGE_HTML,
   "B5-3b 选完选项后可以提交（不再是永远 disabled 的死按钮）")


# --------------------------------------------------------------------------- #
section("B5-4 HTTP：来源校验与安全头")

srv = TableServer(WebBridge(timeout=5), port=0).start()
base = srv.url


def http(path, payload=None, *, headers=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


code, hdrs, _body = http("")
ok(code == 200 and hdrs.get("X-Frame-Options") == "DENY",
   "B5-4a ⭐ 正常请求通过，且带 X-Frame-Options/CSP", hdrs.get("X-Frame-Options"))
ok("frame-ancestors" in hdrs.get("Content-Security-Policy", ""),
   "B5-4b CSP frame-ancestors 存在")

code, _h, _b = http("", headers={"Host": "evil.example.com"})
ok(code == 403, "B5-4c ⭐ 非回环 Host → 403（挡 DNS rebinding）", code)

code, _h, _b = http("api/answer", {"seq": 1, "id": "look"},
                    headers={"Origin": "http://evil.example.com"})
ok(code == 403, "B5-4d ⭐ 跨站 Origin → 403", code)

code, _h, _b = http("api/answer", {"seq": 0, "id": "look"})
ok(code in (400, 409), "B5-4e 同源但无问题时仍是 4xx（没被来源校验误伤）", code)
srv.stop()


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
