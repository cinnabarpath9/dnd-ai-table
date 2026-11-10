# coding=utf-8
"""日常行动解析：JSON 走正道；正文里给了合法 id 就回收并记问题；不合法才兜底。"""

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

from engine.models import FakeModel, ModelReply  # noqa: E402
from engine.rules.loop import GameLoop, RoundLog  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

FAILS: list[str] = []
COUNT = 0
OPTS = [{"id": "look", "name": "观察"}, {"id": "search", "name": "搜索"},
        {"id": "move", "name": "移动"}, {"id": "rest", "name": "短休"},
        {"id": "talk", "name": "交谈"}]


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def make_loop():
    st = StateTree.from_template({"甲": new_character("甲", level=1, job="战士")},
                                 current="甲")
    loop = GameLoop(st, Ledger(), FakeModel())
    loop._cur = RoundLog(n=1, phase="explore")
    return loop


def pick(text="", data=None):
    loop = make_loop()
    out = loop._pick_explore(ModelReply(text=text, data=data), OPTS)
    return out, list(loop._cur.problems)


out, probs = pick(data={"id": "search", "say": "我去搜"})
ok(out["id"] == "search" and out["say"] == "我去搜" and not probs,
   "A1 标准 JSON → 原样采纳，零问题", (out, probs))

out, probs = pick(text="[search] 我蹲下去看灰里的脚印。")
ok(out["id"] == "search" and not out["say"].startswith("[search]"),
   "A2 正文 `[search]` → 回收成 search、去掉前缀", out)
ok(any("回收" in p for p in probs), "A3 回收会记问题（不静默）", probs)

out, probs = pick(text="我选 **[search]**，先看门槛。")
ok(out["id"] == "search" and any("回收" in p for p in probs),
   "A4 中文「我选 **[search]**」也能回收", (out, probs))

out, probs = pick(text="[fly] 我要飞过去。")
ok(out["id"] == "look" and any("非法" in p for p in probs),
   "A5 非法 id → 兜底 look + 记非法", (out, probs))

out, probs = pick(data={"actions": [{"id": "move"}]})
ok(out["id"] == "move", "A6 战斗形 actions 也认（容错）", out)

out, probs = pick(text="我来看看四周。")
ok(out["id"] == "look" and any("非法" in p for p in probs),
   "A7 完全没给 id → look + 记非法", (out, probs))

out, probs = pick(text="[search] " + "很长的描述。" * 80)
ok(out["id"] == "search" and len(out["say"]) <= 301, "A8 回收文本仍截到 300 字",
   len(out["say"]))

ok("只输出一个 JSON 对象" in GameLoop.PLAYER_SYSTEM,
   "A9 玩家系统提示明确要求 JSON")

out, probs = pick(data={"actions": [{"id": "search"}, {"id": "rest"}]})
ok(out["id"] == "search" and any("只执行第一条" in p for p in probs),
   "A10 ⭐ 多动作只执行第一条，但必须记问题", (out, probs))

out, probs = pick(text="我先观察，稍后再看。[search] 不是现在。")
ok(out["id"] == "look" and any("非法" in p for p in probs),
   "A11 正文后文的 [search] 不再被回收（只认开头/我选）", (out, probs))

out, probs = pick(text="我不选 [search]，先看看。")
ok(out["id"] == "look" and any("非法" in p for p in probs),
   "A12 「不选 [search]」不会误回收", (out, probs))

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")