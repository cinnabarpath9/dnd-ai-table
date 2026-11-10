# coding=utf-8
"""跑团体验层：开桌说明、血量叙事阶梯、战斗先攻条的数据与页面接线。

只守"看得见的契约"，不重复测规则数值：

    · 开桌页有四条桌规、角色审卡、必须勾选才能开始；
    · DM 提示词有 HP 叙事阶梯（>50% 不写重伤；≤50% 才见血）；
    · 战斗快照有先攻顺序/状态，页面有先攻条与"见血"徽章。
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

from engine.demo_web import map_snapshot  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.webui import PAGE_HTML  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


print("=== A. 开桌页 ===")
ok('id="session0"' in PAGE_HTML and 'aria-label="开桌"' in PAGE_HTML,
   "A1 开桌弹层在页面上、有可读名")
ok(all(s in PAGE_HTML for s in ("共同叙事", "DM 不是敌人，也不是保姆",
                                "失败也是故事", "代价与权衡")),
   "A2 四条桌规都在")
ok('id="s0-review"' in PAGE_HTML and "renderSession0" in PAGE_HTML,
   "A3 有角色审卡区并会渲染")
ok('id="s0-ack"' in PAGE_HTML and 'id="s0Start" disabled' in PAGE_HTML,
   "A4 必须先勾选才能开始（按钮默认禁用）")
ok('id="openSession0"' in PAGE_HTML and "openSession0" in PAGE_HTML,
   "A5 设置里能重新打开开桌说明")

print("\n=== B. 血量叙事阶梯 ===")
ok("血量叙事阶梯" in GameLoop.DM_SYSTEM, "B1 DM 提示词有血量阶梯")
ok("Bloodied" in GameLoop.DM_SYSTEM and "50%" in GameLoop.DM_SYSTEM,
   "B2 50% 分界与 Bloodied 写清楚了")
ok("致命一击" in GameLoop.DM_SYSTEM and "不写实质重伤" in GameLoop.DM_SYSTEM,
   "B3 高血量不写重伤、0 HP 才致命一击")
ok("chip bloodied" in PAGE_HTML and "见血" in PAGE_HTML,
   "B4 状态栏有见血徽章")

print("\n=== C. 战斗先攻条 ===")
ok('id="initiative"' in PAGE_HTML and "renderInitiative" in PAGE_HTML,
   "C1 页面有先攻条与渲染函数")
ok("renderInitiative(lastMap, lastPending)" in PAGE_HTML,
   "C2 地图刷新时按当前等待者高亮")
ok("init-card" in PAGE_HTML and "状态：" in PAGE_HTML,
   "C3 先攻卡显示 HP/先攻/状态")

st = StateTree.from_template(
    {"甲": new_character("甲", level=3, job="战士", hp_max=24, ac=15),
     "乙": new_character("乙", level=3, job="盗贼", hp_max=20, ac=14)},
    current="甲")
u1, u2 = make_hero_unit(st, "甲"), make_hero_unit(st, "乙")
u2.conditions["prone"] = 1
loop = GameLoop(st, Ledger(), FakeModel())
loop.set_field([u1, u2])
loop.initiative = [(u1, 15), (u2, 10)]
m = map_snapshot(loop)
ok(m.get("battle") is True and m.get("order") == [u1.id, u2.id],
   "C4 快照带先攻顺序", m.get("order"))
bad = [u for u in m.get("units", []) if u["id"] == u2.id]
ok(bad and "prone" in bad[0]["conditions"],
   "C5 快照带状态（倒地）", bad)
first = [u for u in m.get("units", []) if u["id"] == u1.id]
ok(first and first[0]["init_score"] == 15,
   "C6 快照带真实先攻值（不是排名）", first[0].get("init_score") if first else None)

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")