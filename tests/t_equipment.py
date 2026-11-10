# coding=utf-8
"""装备栏：8 个战斗栏、盾牌左手、双手武器、战斗饰品 2 件、日常不限。

这些是**引擎口径**，不是 UI 装饰：
    · 盾牌占左手，有盾不能双手武器；
    · 双手武器占左右手；
    · 战斗饰品最多 2 件；
    · 日常服装/饰品不占战斗栏，也不参与 AC/攻击。
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

from engine.models import FakeModel  # noqa: E402
from engine.rules.armor import armor_class  # noqa: E402
from engine.rules.equipment import SLOT_NAMES, slot_map, validate_equip  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.web_actions import ActionError, apply_action  # noqa: E402
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


def make(items):
    st = StateTree.from_template({"甲": new_character("甲", level=3)})
    st.commit([{"op": "insert", "path": f"/角色列表/甲/物品/{k}", "value": v}
               for k, v in items.items()], actor="DM", reason="测试装备")
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, "甲")])
    return st, loop


def equip(st, loop, name, **kw):
    return apply_action(st, loop, "甲", {"kind": "equip_item", "name": name, **kw})


print("=== A. 8 个栏位与基础装备 ===")
ok(SLOT_NAMES == ("头盔", "上装", "下装", "靴子", "左手", "右手", "饰品1", "饰品2"),
   "A1 固定 8 个战斗栏", SLOT_NAMES)
st, loop = make({
    "头盔": {"类别": "头盔", "装备类型": "战斗"},
    "上衣": {"类别": "护甲", "装备类型": "战斗", "护甲加值": 12, "护甲类型": "轻甲"},
    "长裤": {"类别": "下装", "装备类型": "战斗"},
    "靴子": {"类别": "靴子", "装备类型": "战斗"},
    "长剑": {"类别": "武器", "装备类型": "战斗", "攻击加值": 5, "伤害骰": "1d8"},
})
for name in ("头盔", "上衣", "长裤", "靴子", "长剑"):
    r = equip(st, loop, name)
    ok(r.get("ok"), f"A2 装备「{name}」", r.get("message"))
m = slot_map(st.read("/角色列表/甲/物品"))
ok(set(SLOT_NAMES) == set(m) - {"日常"}, "A3 栏位表正好 8 个")
ok(m["头盔"] == "头盔" and m["上装"] == "上衣"
   and m["下装"] == "长裤" and m["靴子"] == "靴子"
   and m["右手"] == "长剑", "A4 各就各位", m)
try:
    equip(st, loop, "头盔")
    # 已经装备 → 卸下，不是重复错误
    ok(True, "A5 再点同一件是卸下")
except ActionError:
    ok(False, "A5 再点同一件是卸下")

print("\n=== B. 盾牌 / 双手武器 ===")
st2, loop2 = make({
    "盾牌": {"类别": "盾牌", "装备类型": "战斗"},
    "巨剑": {"类别": "武器", "装备类型": "战斗", "双手": True,
             "攻击加值": 5, "伤害骰": "2d6"},
})
ok(equip(st2, loop2, "盾牌").get("ok"), "B1 盾牌能装")
m = slot_map(st2.read("/角色列表/甲/物品"))
ok(m["左手"] == "盾牌" and m["右手"] is None, "B2 盾牌固定占左手", m)
try:
    equip(st2, loop2, "巨剑")
    ok(False, "B3 有盾牌时禁止双手武器")
except ActionError as exc:
    ok("双手" in str(exc) or "盾" in str(exc), "B3 有盾牌时禁止双手武器", str(exc))
equip(st2, loop2, "盾牌")                        # 卸下
ok(equip(st2, loop2, "巨剑").get("ok"), "B4 没盾后能装双手武器")
m = slot_map(st2.read("/角色列表/甲/物品"))
ok(m["左手"] == "巨剑" and m["右手"] == "巨剑", "B5 双手武器占左右手", m)
try:
    equip(st2, loop2, "盾牌")
    ok(False, "B6 双手武器时不能再拿盾")
except ActionError:
    ok(True, "B6 双手武器时不能再拿盾")

print("\n=== C. 战斗饰品最多 2 件 ===")
st3, loop3 = make({
    "护符A": {"类别": "饰品", "装备类型": "战斗", "护甲加值": 1},
    "护符B": {"类别": "饰品", "装备类型": "战斗", "护甲加值": 1},
    "护符C": {"类别": "饰品", "装备类型": "战斗", "护甲加值": 1},
})
for name in ("护符A", "护符B"):
    ok(equip(st3, loop3, name).get("ok"), f"C1 装备「{name}」")
try:
    equip(st3, loop3, "护符C")
    ok(False, "C2 第三件战斗饰品被拒")
except ActionError as exc:
    ok("最多 2" in str(exc), "C2 第三件战斗饰品被拒", str(exc))
ac, parts, _w = armor_class(st3, "甲")
ok(ac == 12 and any("饰品" in x for x in parts),
   "C3 两件饰品护甲加值进 AC", (ac, parts))

print("\n=== D. 日常不限、不参与数值 ===")
st4, loop4 = make({
    "礼服": {"类别": "日常服装", "装备类型": "日常"},
    "项链": {"类别": "饰品", "装备类型": "日常"},
    "脚环": {"类别": "饰品", "装备类型": "日常"},
    "战斗护符": {"类别": "饰品", "装备类型": "战斗", "护甲加值": 5},
})
for name in ("礼服", "项链", "脚环", "战斗护符"):
    ok(equip(st4, loop4, name).get("ok"), f"D1 日常/战斗都能装「{name}」")
m = slot_map(st4.read("/角色列表/甲/物品"))
ok(set(m["日常"]) == {"礼服", "项链", "脚环"},
   "D2 日常不占战斗栏、不限数量", m["日常"])
ac, _parts, _w = armor_class(st4, "甲")
ok(ac == 15, "D3 只有战斗饰品 5 进 AC，日常不算", ac)

print("\n=== E. 页面 ===")
ok("equip-grid" in PAGE_HTML and "日常穿着" in PAGE_HTML,
   "E1 装备页有 8 栏网格和日常区")
ok(all(x in PAGE_HTML for x in SLOT_NAMES), "E2 八个栏名都在页面")
ok("equip_item" in PAGE_HTML and "equip-grid" in PAGE_HTML,
   "E3 装备按钮走 equip_item 引擎动作")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")