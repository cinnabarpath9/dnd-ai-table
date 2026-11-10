# coding=utf-8
"""P5-C 回归：真人网页座位（本地 HTTP ↔ 座位）。

守四件事：
    W1 桥：问→答的握手、过期序号、非法选项、需要目标、超时
    W2 HTTP：页面能打开、状态是 JSON、坏请求有明确的 4xx
    W3 座位：`WebHumanSeat` 就是 `HumanSeat`（同一套校验与兜底，不另起一套）
    W4 端到端：用真 HTTP 驱动一轮，人的行动真的进了台账
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
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

from engine.models import FakeModel, TableClient  # noqa: E402
from engine.models.client import ModelRequest  # noqa: E402
from engine.models.seats import HumanSeat  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.state.world import new_world  # noqa: E402
from engine.webui import (  # noqa: E402
    TableServer, WebBridge, WebHumanSeat,
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


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


def req(options: list[dict], *, actor: str = "布兰", task: str = "explore_action"):
    return ModelRequest(role="player", actor=actor, task=task,
                        prompt="（上下文）", expect="intent",
                        allowed={"options": options})


EXPLORE_OPTS = [
    {"id": "look", "kind": "observe", "name": "观察", "summary": "看四周"},
    {"id": "talk", "kind": "social", "name": "交谈", "summary": "说话"},
]
COMBAT_OPTS = [
    {"id": "a1", "kind": "attack", "slot": "action", "name": "砍",
     "requires_target": True, "targets": ["E1"]},
    {"id": "a9", "kind": "end_turn", "slot": "action", "name": "结束回合"},
]


# --------------------------------------------------------------------------- #
section("W1 桥：握手、校验、超时")

b = WebBridge(timeout=5)
result: dict = {}


def _ask_worker(payload):
    r = req(EXPLORE_OPTS)
    result["line"] = b.ask(r)


th = threading.Thread(target=_ask_worker, args=(None,), daemon=True)
th.start()
for _ in range(50):
    time.sleep(0.02)
    p = b.snapshot()["pending"]
    if p:
        break
ok(p is not None and p["actor"] == "布兰" and len(p["options"]) == 2,
   "W1a 座位一发问，网页就看见 pending（带选项）", p)
ok(b.submit(seq=p["seq"] + 99, oid="look")[0] is False,
   "W1b ⭐ 过期序号被拒（页面刷新后乱点不会顶掉新问题）")
ok(b.submit(seq=p["seq"], oid="nope")[0] is False,
   "W1c 菜单里没有的选项被拒")
ok(b.submit(seq=p["seq"], oid="look", say="我贴着门听")[0] is True,
   "W1d 合法答案被接受")
th.join(timeout=3)
ok(result.get("line") == "look 我贴着门听",
   "W1e 座位拿到的就是一行合法输入", result.get("line"))

b2 = WebBridge(timeout=5)
th = threading.Thread(
    target=lambda: result.__setitem__("combat", b2.ask(req(COMBAT_OPTS))),
    daemon=True)
th.start()
for _ in range(50):
    time.sleep(0.02)
    p2 = b2.snapshot()["pending"]
    if p2:
        break
ok(p2 is not None and p2["combat"] is True, "W1f 战斗菜单被识别为 combat")
ok(b2.submit(seq=p2["seq"], oid="a1")[0] is False,
   "W1g ⭐ 需要目标的选项，不给目标 → 拒绝")
ok(b2.submit(seq=p2["seq"], oid="a1", target="E1")[0] is True,
   "W1h 给了合法目标 → 接受")
th.join(timeout=3)
ok(result.get("combat") == "a1 E1", "W1i 战斗答案拼成 `id target`",
   result.get("combat"))

b3 = WebBridge(timeout=0.2)
t0 = time.time()
line = b3.ask(req(EXPLORE_OPTS))
ok(line == "" and time.time() - t0 >= 0.15,
   "W1j 超时返回空串（座位会按「空输入」记一次尝试，不假装人答了）", line)

b3.log("一行日志")
snap = b3.snapshot(since=0)
ok(any("一行日志" in x["text"] for x in snap["log"]), "W1k 日志能进快照")
ok(b3.snapshot(since=snap["log"][-1]["seq"])["log"] == [],
   "W1l since 过滤生效（不重复刷旧日志）")


# --------------------------------------------------------------------------- #
section("W2 HTTP：页面 / 状态 / 坏请求")

srv = TableServer(WebBridge(timeout=5), port=0).start()
base = srv.url


def http(path: str, payload=None, *, method=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    r = urllib.request.Request(base + path, data=data,
                               method=method or ("POST" if data else "GET"),
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, ctype, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")


code, ctype, body = http("")
ok(code == 200 and "text/html" in ctype and "你的座位" in body,
   "W2a 首页能打开（内联 HTML，无外部资源）", (code, ctype))

code, ctype, body = http("api/state")
st = json.loads(body)
ok(code == 200 and "application/json" in ctype and "pending" in st and "log" in st,
   "W2b /api/state 是 JSON，字段齐全", (code, st.keys()))

code, _c, body = http("")
ok('data-panel="dm-action"' in body and 'id="dma-write"' in body,
   "W2g 页面有 DM 技能表单（独立弹层）")
ok("openActionEditor" in body and "specSummary" in body
   and "dm_actions" in body,
   "W2h 页面有技能编辑/卡面渲染脚本")
ok('data-panel="npc"' in body and 'data-panel="dm-npc"' in body,
   "W2i 页面有人物面板与人物数值表单")
ok("renderNpcPanel" in body and "dm_add_npc_to_combat" in body,
   "W2j 人物面板能渲染、能放人上场")

code, _c, body = http("api/nope")
ok(code == 404, "W2c 不存在的路径 → 404", code)

code, _c, body = http("api/answer", {"seq": 0, "id": "look"})
ok(code in (400, 409), "W2d 没有问题时提交 → 4xx（不是 500）", code)

code, _c, body = http("api/answer", {"bad": "json"})
ok(code in (400, 409), "W2e 缺字段的答案 → 4xx", code)

# 畸形 JSON（直接发非 JSON 文本）
r = urllib.request.Request(base + "api/answer", data=b"{not json",
                           headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(r, timeout=5)
    bad_code = 200
except urllib.error.HTTPError as exc:
    bad_code = exc.code
ok(bad_code == 400, "W2f 坏 JSON → 400（不抛异常、不 500）", bad_code)

srv.stop()


# --------------------------------------------------------------------------- #
section("W3 座位：就是 HumanSeat，不另起一套")

ok(issubclass(WebHumanSeat, HumanSeat), "W3a WebHumanSeat 继承 HumanSeat")
ok(getattr(WebHumanSeat(WebBridge()), "is_human", False),
   "W3a2 ⭐ 网页真人显式 is_human=True（不再靠类名判断）")
_hosted = WebHumanSeat(WebBridge(), auto=["look"])
ok(_hosted._auto == ["look"],
   "W3b ⭐ 网页座位接受 auto（托管就是这条；不再拒绝）")
ok(_hosted._answer(ModelRequest(role="player", actor="布兰", task="explore_action",
                                prompt="", expect="intent",
                                allowed={"options": [{"id": "look"}]}),
                   "> ") == "look",
   "W3b2 ⭐ 托管时不问网页、直接取 auto 的答案")
ok(WebHumanSeat(WebBridge()).max_tries == 3,
   "W3c 复用同样的三次失败上限")


# --------------------------------------------------------------------------- #
section("W4 端到端：真 HTTP 驱动一轮，人的行动进台账")

bridge = WebBridge(timeout=30)
seat = WebHumanSeat(bridge, name="真人·布兰")
st = StateTree.from_template(
    {"布兰": new_character("布兰", level=3, job="牧师",
                           abilities={"力量": 12, "敏捷": 10, "体质": 14,
                                      "智力": 10, "感知": 16, "魅力": 12},
                           hp_max=24, ac=15)},
    current="布兰", world=new_world(date="1492-09-17", period="午后"))
ledger = Ledger()
table = TableClient(
    dm=FakeModel(name="dm"), planner=FakeModel(name="planner"),
    players={"布兰": seat}, name="网页桌")
loop = GameLoop(st, ledger, table, label="网页桌")
loop.set_field([make_hero_unit(st, "布兰")])
srv = TableServer(bridge, port=0).start()
base = srv.url

run_error: list[str] = []


def _run():
    try:
        loop.run_explore_round_all(situation="你们站在哨塔门外。")
    except Exception as exc:                 # noqa: BLE001
        run_error.append(str(exc))
    finally:
        bridge.finish()


th = threading.Thread(target=_run, daemon=True)
th.start()

pending = None
for _ in range(100):
    time.sleep(0.03)
    st_now = json.loads(http("api/state")[2])
    pending = st_now.get("pending")
    if pending:
        break
ok(pending is not None, "W4a 网页看到了人的回合（pending）", pending)

if pending:
    oid = next(o["id"] for o in pending["options"] if o["id"] == "look")
    code, _c, body = http("api/answer",
                          {"seq": pending["seq"], "id": oid,
                           "say": "我贴着门听一下"})
    ok(code == 200 and json.loads(body).get("ok") is True,
       "W4b 提交合法行动 → 200 / ok", (code, body))

th.join(timeout=10)
final_state = json.loads(http("api/state")[2])
ok(final_state.get("done") is True and final_state.get("pending") is None,
   "W4e 结束后状态是 done、pending 清空", final_state)
srv.stop()
ok(not run_error, "W4c 这一轮跑完了（没有异常）", run_error)
says = [e for e in ledger.entries if e.kind == "对话" and e.actor == "布兰"]
ok(any("我贴着门听一下" in e.text for e in says),
   "W4d ⭐ 人说的话进了台账（不是引擎替他编的）",
   [e.text for e in says])


# --------------------------------------------------------------------------- #
section("W5 真模型座位（--real）：换人不换前端")

from engine import demo_web as _dw  # noqa: E402

_orig_factory = _dw.SEAT_FACTORY
try:
    def _factory(label):
        return _dw.CappedReal({"n": 0}, cap=0, name=label, model="m",
                              base_url="http://fake", api_key="k", enabled=True)

    _dw.SEAT_FACTORY = _factory
    _table = _dw.make_web_table(WebBridge())
    ok(isinstance(_table.dm, _dw.CappedReal)
       and isinstance(_table.planner, _dw.CappedReal),
       "W5a ⭐ 工厂接上后 DM/planner 都是真模型座位")
    ok(all(isinstance(_table.players[n], _dw.CappedReal)
           for n in ("艾尔玛", "罗恩", "小翠")),
       "W5b ⭐ 三名同伴也是真模型座位")
    ok(isinstance(_table.players[_dw.HUMAN], WebHumanSeat),
       "W5c ⭐ 真人座位仍是网页（不因为 --real 变成模型）")
finally:
    _dw.SEAT_FACTORY = _orig_factory

# 2026-11-08 真浏览器抓到：页面从未把 pending.seq 写进 seq → 真人提交必 409。
# 这里守一句“页面里有这行赋值”，属于最低限度的看门狗（真点击见 tools/web_browser_check.py）。
from engine.webui import PAGE_HTML as _PAGE  # noqa: E402
ok(_PAGE.count("seq = s.pending.seq;") >= 2,
   "W5f ⭐ 页面把 pending.seq 写进提交用的 seq（两处：poll / refreshState）",
   _PAGE.count("seq = s.pending.seq;"))
ok(isinstance(_dw.make_web_table(WebBridge()).dm, _dw._DemoDM),
   "W5d 没接工厂时回到假模型（默认路径不变）")

# 上限闸：到 cap 之后不再发请求（网页自己往下跑，没闸会一直烧钱）
_lim = _dw.CappedReal({"n": 0}, cap=0, name="x", model="m",
                      base_url="http://fake", api_key="k", enabled=True)
# run_all 默认开进程闸门；这里要验的是**上限闸**，先摘掉再还原
_gate = os.environ.pop("DND_NO_REAL_CALLS", None)
try:
    _r = _lim.complete(ModelRequest(role="dm", actor="dm", task="narrate",
                                    prompt="x", expect="narration"))
finally:
    if _gate is not None:
        os.environ["DND_NO_REAL_CALLS"] = _gate
ok(not _r.ok and "上限" in str(_r.error or ""),
   "W5e ⭐ 到调用上限后拒绝发请求（不抛、循环记成没动）", _r.error)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
print("\n=== W6 多模型座位配置：标签 → model/base_url/env ===")

_cfg = {
    "defaults": {"max_tokens": 12000, "temperature": 0.7},
    "seats": {
        "dm": {"model": "gemini-3.8-flash", "base_url": "https://pay/v1",
               "api_key_env": "DND_52PAY_KEY", "reasoning_effort": None},
        "pc:*": {"model": "deepseek-v4.1-flash", "base_url": "https://qianfan/v2",
                 "api_key_env": "QIANFAN_API_KEY", "reasoning_effort": "low"},
        "pilot": {"model": "deepseek-v4.1-flash", "base_url": "https://qianfan/v2",
                  "api_key_env": "QIANFAN_API_KEY", "reasoning_effort": "medium"},
        "pc:罗恩": {"model": "claude-haiku-5-5", "base_url": "https://pay/v1",
                    "api_key_env": "DND_52PAY_KEY", "reasoning_effort": "medium"},
    },
}
ok(_dw.seat_spec("dm", _cfg)["model"] == "gemini-3.8-flash",
   "W6a 精确座位取 dm 配置")
ok(_dw.seat_spec("pc:罗恩", _cfg)["model"] == "claude-haiku-5-5",
   "W6b 精确座位取 PC 配置")
ok(_dw.seat_spec("pc:小翠", _cfg)["model"] == "deepseek-v4.1-flash",
   "W6c 未列名字回落到 pc:*", _dw.seat_spec("pc:小翠", _cfg).get("model"))
ok(_dw.seat_spec("pc:pilot:布兰", _cfg)["reasoning_effort"] == "medium",
   "W6d 托管 pilot 有自己的档位")
ok(_dw.seat_spec("dm", _cfg)["max_tokens"] == 12000,
   "W6e defaults 铺底")

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                 encoding="utf-8") as fh:
    json.dump(_cfg, fh, ensure_ascii=False)
    _path = fh.name
try:
    ok(_dw.load_seat_config(_path)["seats"]["dm"]["model"] == "gemini-3.8-flash",
       "W6f JSON 配置文件可加载")
finally:
    os.unlink(_path)

_old_env = os.environ.pop("DND_52PAY_KEY", None)
try:
    _missing = _dw.missing_seat_envs(_cfg)
    ok(any("DND_52PAY_KEY" in x for x in _missing),
       "W6g 缺 api_key_env 会提前报出来", _missing)
finally:
    if _old_env is not None:
        os.environ["DND_52PAY_KEY"] = _old_env

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")



