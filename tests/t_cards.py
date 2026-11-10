# coding=utf-8
"""物品/法术/技能卡：DM 设定 + 引擎数据分开。

这里守三件事：

    · 卡片有引擎数据（类别/伤害/环阶/规则原文），也有 DM 设定；
    · DM 可补描述/机制/数值/效果/背景，写进世界树并跟存档；
    · DM 设定是**桌面裁定**，不冒充引擎自动验算。
"""

from __future__ import annotations

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.demo_web import sheet_snapshot  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, WorldError, new_character  # noqa: E402
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


print("=== A. DM 设定写进世界树 ===")
st = StateTree.from_template({"甲": new_character("甲", level=3, job="法师")})
loop = GameLoop(st, Ledger(), FakeModel())
loop.world.set_lore("物品", "长剑", "剑身有旧缺口。")
lore = loop.world.lore("物品", "长剑")
ok(lore.get("描述") == "剑身有旧缺口。" and lore.get("补述者") == "DM",
   "A1 单字段补述能写能读", lore)
loop.world.set_lore_fields("物品", "长剑", {
    "描述": "剑身有旧缺口。", "机制": "命中时额外 1 点火焰伤害。",
    "背景": "铁炉堡学徒的练手作。"})
lore = loop.world.lore("物品", "长剑")
ok(lore.get("机制") == "命中时额外 1 点火焰伤害。"
   and lore.get("背景") == "铁炉堡学徒的练手作。",
   "A2 DM 可补机制/背景，不只描述", lore)
ok(loop.state.audit[-1].actor == "DM", "A3 补述走审计，记在 DM 名下")
try:
    loop.world.set_lore_fields("人物", "甲", {"描述": "x"})
    ok(False, "A4 只允许物品/法术/技能三类")
except WorldError:
    ok(True, "A4 只允许物品/法术/技能三类")
try:
    loop.world.set_lore_fields("物品", "长剑", {"伤害": "999"})
    ok(False, "A5 不认识的字段直接报")
except WorldError:
    ok(True, "A5 不认识的字段直接报")
loop.world.set_lore_fields("物品", "长剑", {})
ok(loop.world.lore("物品", "长剑") == {}, "A6 清空设定会删除整条")
ok("机制" in loop.state.audit[-1].reason or "DM" in loop.state.audit[-1].actor,
   "A7 清空也留审计")

print("\n=== B. 卡片数据：引擎与 DM 分开 ===")
# 装备与法术书
st.commit([
    {"op": "insert", "path": "/角色列表/甲/物品/长剑",
     "value": {"类别": "武器", "已装备": True, "伤害公式": "1d8",
               "伤害类型": "挥砍", "描述": "一把旧剑。"}},
    {"op": "insert", "path": "/角色列表/甲/物品/治疗药水",
     "value": {"类别": "消耗品", "数量": 2, "描述": "红色的药水。"}},
    {"op": "insert", "path": "/角色列表/甲/施法/法术书/Magic Missile",
     "value": {"等级": 1, "准备中": True, "学派": "塑能", "成分": "V、S"}},
    {"op": "insert", "path": "/角色列表/甲/熟练配置/技能/察觉",
     "value": "熟练"},
], actor="测试", reason="准备卡片数据")
loop.set_field([make_hero_unit(st, "甲")])
loop.world.set_lore_fields("物品", "治疗药水", {
    "描述": "瓶身贴着圣光教会的封条。",
    "机制": "喝下恢复 2d4+2 生命；不占动作（桌面裁定）。",
    "背景": "圣裔港的药剂师常备。"})
loop.world.set_lore_fields("法术", "Magic Missile", {
    "描述": "三枚银蓝色飞弹从指尖追出。", "背景": "法师塔的基础战斗法术。"})
loop.world.set_lore_fields("技能", "察觉", {
    "描述": "长期守夜练出的眼力。", "机制": "被动察觉 +2（桌面裁定）。"})

snap = sheet_snapshot(loop, "甲")["sheet"]
ok(snap.get("name") == "甲", "B0 角色名不被法术遍历覆盖", snap.get("name"))
items = {x["name"]: x for x in snap["items"]}
ok(items["长剑"].get("伤害公式") == "1d8",
   "B1 物品卡保留引擎伤害数据")
ok(items["治疗药水"]["lore"].get("机制", "").startswith("喝下恢复"),
   "B2 药水卡带 DM 机制", items["治疗药水"].get("lore"))
ok(items["治疗药水"].get("描述") == "红色的药水。",
   "B3 原有描述不被 DM 设定覆盖（单独字段）")
mm = snap["spellbook"]["Magic Missile"]
ok(bool(mm.get("规则描述")),
   "B4 法术卡带 SRD 规则原文", bool(mm.get("规则描述")))
ok(mm.get("施法时间") and mm.get("射程"),
   "B5 法术卡带 SRD 施法时间/射程", (mm.get("施法时间"), mm.get("射程")))
ok(mm["lore"].get("背景") == "法师塔的基础战斗法术。",
   "B6 法术卡带 DM 设定")
ok(snap.get("skill_lore", {}).get("察觉", {}).get("机制") == "被动察觉 +2（桌面裁定）。",
   "B7 技能卡带 DM 机制")

print("\n=== C. 页面入口 ===")
ok('data-panel="lore"' in PAGE_HTML and 'id="lore-text"' in PAGE_HTML,
   "C1 有 DM 设定弹层")
ok('id="lore-mech"' in PAGE_HTML and 'id="lore-bg"' in PAGE_HTML,
   "C2 可写机制/数值/效果与背景")
ok("DM 设定" in PAGE_HTML and "结构化数值已进引擎" in PAGE_HTML
   and "loot-card" in PAGE_HTML,
   "C3 卡片把 DM 设定与引擎结算分开标")
ok('id="loreSave"' in PAGE_HTML and "set_lore" in PAGE_HTML,
   "C4 保存走 set_lore 并会刷新")
ok('id="dm-save"' in PAGE_HTML and "DM 造物" in PAGE_HTML,
   "C5 页面有 DM 造物入口")
ok('id="dm-atk"' in PAGE_HTML and 'id="dm-ac"' in PAGE_HTML,
   "C6 造物可写攻击/AC 数值")
ok("改数值" in PAGE_HTML and "set_item_stats" in PAGE_HTML,
   "C7 已有物品可改引擎数值")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")