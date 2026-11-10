# coding=utf-8
"""H3 回归：装备特效（免死 / 临时生命 / 回合回复）+ 各栏 AC 加值。

守四件事：
    · 写入口：不合法的"触发×效果"组合直接报错，不给人"写了就有效"的错觉；
    · 斗篷例子：致命伤害 → 免死 + 回复，每场一次，开战重置；
    · 回合/开战触发：回复、临时生命真的进单位数值；
    · 头盔/下装/靴子的 `护甲加值` 进 AC（以前只占栏）。
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
from engine.rules.effect_catalog import (  # noqa: E402
    character_budget, validate_item_entries,
)
from engine.rules.item_effects import (  # noqa: E402
    coerce_effect, coerce_item_effects,
)
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.unit import DamagePacket  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.web_actions import ActionError, apply_action  # noqa: E402

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


def fresh(name="甲", hp_max=20):
    st = StateTree.from_template({name: new_character(name, level=3,
                                                      hp_max=hp_max)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, name)])
    return st, loop


def give(st, loop, who, payload, *, equip=True):
    r = apply_action(st, loop, who, {"kind": "dm_create_item", **payload})
    if equip:
        apply_action(st, loop, who, {"kind": "equip_item",
                                     "name": payload["name"]})
    return r


print("=== A. 写入口：只认配对好的触发×效果 ===")
bad = [
    ({"名称": "怪", "触发": "致命伤害", "效果": "回复", "数值": 3},
     "致命伤害不能配回复"),
    ({"名称": "空血", "触发": "战斗开始", "效果": "临时生命"}, "临时生命缺数值"),
    ({"名称": "错触发", "触发": "挨打", "效果": "免死"}, "触发不认识"),
    ({"名称": "错效果", "触发": "回合开始", "效果": "吸血"}, "效果不认识"),
    ({"名称": "", "触发": "致命伤害", "效果": "免死"}, "缺名称"),
]
for spec, label in bad:
    try:
        coerce_effect(spec)
        ok(False, f"A·{label} 必须被拒")
    except ValueError as exc:
        ok(True, f"A·{label} → ValueError", str(exc))
cloak = coerce_effect({"名称": "余荫", "触发": "致命伤害", "效果": "免死",
                        "数值": 8})
ok(cloak["保留生命"] == 1 and cloak["每场"] == 1,
   "A6 免死默认保留 1 点、每场 1 次", cloak)
multi = coerce_item_effects([{"名称": "a", "触发": "回合开始",
                               "效果": "回复", "数值": 1},
                              {"名称": "b", "触发": "战斗开始",
                               "效果": "临时生命", "数值": 2}])
ok(len(multi) == 2 and multi[0]["每场"] == 0,
   "A7 一个物品可以挂多条特效；回复默认为不限次数", multi)

print("\n=== B. 斗篷例子：一次免死 + 回复，每场一次 ===")
st, loop = fresh()
give(st, loop, "甲", {
    "name": "生命之神的斗篷", "category": "饰品",
    "词条": ["ward_grace"]})
u = loop.find_unit("甲")
ok(len(u.item_effects) == 1 and u.item_effects[0]["效果"] == "免死"
   and u.item_effects[0]["物品"] == "生命之神的斗篷",
   "B1 装词条后特效进单位（来源物品）", u.item_effects)
u.hp = 5
res = u.take_damage([DamagePacket(30, "")])
ok(not res.dropped_to_zero and u.hp == 9,
   "B2 ⭐ 致命伤被免死挡住：保留 1 点再回复 8", (u.hp, res.notes))
u.hp = 5
res2 = u.take_damage([DamagePacket(30, "")])
ok(res2.dropped_to_zero and u.hp == 0,
   "B3 ⭐ 同一场第二次致命伤不再免死（每场 1 次）", (u.hp, res2.notes))

loop.start_combat(enemies=[("Bandit", 1)], distance=20)
u.hp = 5
res3 = u.take_damage([DamagePacket(30, "")])
ok(not res3.dropped_to_zero and u.hp == 9,
   "B4 ⭐ 新一场战斗充能回满，免死又能用了", (u.hp, res3.notes))
loop.start_combat(enemies=[("Bandit", 1)], distance=20)
u.hp = 0
res4 = u.take_damage([DamagePacket(5, "")])
ok(res4.dropped_to_zero and u.hp == 0
   and u.item_charges["生命之神的斗篷:生命之神的余荫"]["current"] == 1,
   "B5 ⭐ 已经 0 HP 再受伤，不免死也不烧次数", (u.hp, res4.notes))

print("\n=== C. 回合开始回复 / 战斗开始临时生命 ===")
st2, loop2 = fresh("乙")
give(st2, loop2, "乙", {
    "name": "常青苔戒", "category": "饰品",
    "特效": [{"名称": "苔藓再生", "触发": "回合开始", "效果": "回复",
              "数值": 3, "每场": 0, "冷却": 2}]})
u2 = loop2.find_unit("乙")
u2.hp = 10
u2.reset_turn()
ok(u2.hp == 13, "C1 ⭐ 回合开始回复 3", u2.hp)
u2.hp = 10
u2.reset_turn()
ok(u2.hp == 10, "C2 ⭐ 冷却 2 回合：下一回合不触发", (u2.hp, u2.item_charges))
u2.hp = 10
u2.reset_turn()
ok(u2.hp == 10, "C3 ⭐ 冷却第 2 回合仍不触发", (u2.hp, u2.item_charges))
u2.hp = 10
u2.reset_turn()
ok(u2.hp == 13, "C4 冷却走完后又能回", (u2.hp, u2.item_charges))

st3, loop3 = fresh("丙")
give(st3, loop3, "丙", {
    "name": "军旗披风", "category": "饰品",
    "特效": [{"名称": "战前鼓舞", "触发": "战斗开始", "效果": "临时生命",
              "数值": 5, "每场": 1}]})
loop3.start_combat(enemies=[("Bandit", 1)], distance=20)
u3 = loop3.find_unit("丙")
ok(u3.temp_hp == 5, "C5 ⭐ 开战获得临时生命 5", u3.temp_hp)
u3.temp_hp = 0
loop3.start_combat(enemies=[("Bandit", 1)], distance=20)
ok(u3.temp_hp == 5, "C6 下一场战斗重置充能后再次触发", u3.temp_hp)

print("\n=== D. 头盔/下装/靴子的护甲加值进 AC ===")
st4, loop4 = fresh("丁")
give(st4, loop4, "丁", {"name": "铁盔", "category": "护甲",
                         "护甲类型": "轻甲", "护甲加值": "1"})
give(st4, loop4, "丁", {"name": "皮靴", "category": "护甲",
                         "护甲类型": "轻甲", "护甲加值": "2"})
ac, parts, warnings = armor_class(st4, "丁")
ok(ac == 13, "D1 ⭐ 头盔 +1、靴子 +2 加进 AC", (ac, parts, warnings))
ok(any("铁盔" in p for p in parts) and any("皮靴" in p for p in parts),
   "D2 AC 明细写清是哪件加的", parts)

print("\n=== E. 写入口拒绝坏特效 / 存读档 ===")
st5, loop5 = fresh("戊")
try:
    apply_action(st5, loop5, "戊", {
        "kind": "dm_create_item", "name": "坏斗篷", "category": "饰品",
        "特效": [{"名称": "怪", "触发": "致命伤害", "效果": "回复",
                  "数值": 3}]})
    ok(False, "E1 坏组合必须被拒")
except ActionError as exc:
    ok(True, "E1 坏组合 → ActionError", str(exc))
ok(st5.read("/角色列表/戊/物品/坏斗篷") is None, "E2 没留下半件物品")

from engine.persist import unit_from_dict, unit_to_dict  # noqa: E402
back = unit_from_dict(unit_to_dict(u))
ok(back.item_effects and back.item_charges,
   "E3 ⭐ 存读档保留装备特效与充能", (back.item_effects, back.item_charges))

print("\n=== F. 词条库：只认库里的词条，且品级有预算 ===")
ok(validate_item_entries(["ward_grace"]) == (["ward_grace"], []),
   "F1 合法词条通过")
_, errs = validate_item_entries(["no_such_entry"])
ok(any("没有这个词条" in e for e in errs), "F2 不认识的 id 被拒", errs)
_, errs = validate_item_entries(["swift_10"])
ok(any("生物词条" in e for e in errs), "F3 生物词条不能装物品", errs)
_, errs = validate_item_entries(["ward_grace", "ward_grace"])
ok(any("重复" in e for e in errs), "F4 同一条不能挂两次", errs)
_, errs = validate_item_entries(["ward_grace", "ward_minor"])
ok(any("超过上限" in e for e in errs), "F5 品级合计超 6 被拒", errs)
ok(character_budget(3) == 5, "F6 等级 3 的品级预算 = 5", character_budget(3))

print("\n=== G. DM 自写词条：只在本战役有效，且不能绕过品级下限 ===")
st6, loop6 = fresh("己")
r = apply_action(st6, loop6, "己", {
    "kind": "dm_create_entry", "id": "x_life_blessing", "名称": "圣徽余晖",
    "品级": 3, "适用": "装备", "触发": "致命伤害", "效果": "免死",
    "数值": 0, "保留生命": 1, "每场": 1, "说明": "本战役的教会圣物。"})
ok(r.get("ok") is True and "本战役" in r.get("message", ""),
   "G1 自写词条写入当局", r.get("message"))
ok((st6.read("/世界/词条库/x_life_blessing") or {}).get("名称") == "圣徽余晖",
   "G2 词条进世界树（跟存档）")
give(st6, loop6, "己", {"name": "圣徽斗篷", "category": "饰品",
                         "词条": ["x_life_blessing"]})
u6 = loop6.find_unit("己")
ok(u6 and any(e.get("词条") == "x_life_blessing" for e in u6.item_effects),
   "G3 ⭐ 自写词条能被物品选中并结算", u6.item_effects)
try:
    apply_action(st6, loop6, "己", {
        "kind": "dm_create_entry", "id": "x_cheap_ward", "名称": "便宜免死",
        "品级": 1, "适用": "装备", "触发": "致命伤害", "效果": "免死",
        "数值": 0})
    ok(False, "G4 自称常见的免死必须被拒")
except ActionError as exc:
    ok(True, "G4 ⭐ 品级下限挡住「便宜免死」", str(exc))
try:
    apply_action(st6, loop6, "己", {
        "kind": "dm_create_entry", "id": "ward_grace", "名称": "撞名",
        "品级": 4, "适用": "装备", "触发": "致命伤害", "效果": "免死"})
    ok(False, "G5 不能覆盖内置词条 id")
except ActionError as exc:
    ok(True, "G5 内置 id 被拒", str(exc))
ok(StateTree.from_template({"己": new_character("己", level=3)})
   .read("/世界/词条库") is None,
   "G6 新开一局：自写词条不跟过去")

print("\n=== H. 生物词条：坐骑速度 / 厚皮 ===")
st7 = StateTree.from_template({"庚": new_character("庚", level=3)})
loop7 = GameLoop(st7, Ledger(), FakeModel())
loop7.set_field([make_hero_unit(st7, "庚")])
apply_action(st7, loop7, "庚", {
    "kind": "dm_set_npc_stats", "name": "千里马", "身份": "坐骑",
    "等级": 1, "护甲等级": 11, "生命值": 13,
    "属性": {"敏捷": 14}, "动作": {"踢": {"类型": "attack",
                                          "伤害骰": "1d6"}}})
r = apply_action(st7, loop7, "庚", {
    "kind": "dm_set_npc_entries", "name": "千里马",
    "词条": ["swift_10", "tough_8"]})
ok(r.get("ok") is True, "H1 给生物挂词条", r.get("message"))
from engine.rules.npc_units import build_npc_unit  # noqa: E402
horse = build_npc_unit(st7, "千里马", team="ally", uid="N9")
ok(horse.speed == 40 and horse.hp_max == 21,
   "H2 ⭐ 速度 +10、生命 +8 进了单位", (horse.speed, horse.hp_max))
try:
    apply_action(st7, loop7, "庚", {
        "kind": "dm_set_npc_entries", "name": "千里马",
        "词条": ["ward_grace"]})
    ok(False, "H3 装备词条不能挂生物")
except ActionError as exc:
    ok(True, "H3 装备词条挂生物 → ActionError", str(exc))

print("\n=== I. 装备预算：等级低的角色穿不上满身传说 ===")
st8, loop8 = fresh("辛")
give(st8, loop8, "辛", {"name": "传说斗篷", "category": "饰品",
                         "词条": ["ward_grace"]})
ok(bool(st8.read("/角色列表/辛/物品/传说斗篷/已装备")),
   "I1 传说斗篷（4 点）≤ 等级 3 的预算 5，能穿")
try:
    apply_action(st8, loop8, "辛", {
        "kind": "dm_create_item", "name": "守御纹章", "category": "饰品",
        "词条": ["guard_2"]})
    apply_action(st8, loop8, "辛", {"kind": "equip_item", "name": "守御纹章"})
    ok(False, "I2 再穿 2 点必须超预算")
except ActionError as exc:
    ok(True, "I2 ⭐ 4+2 > 5 → 拒绝装备", str(exc))
apply_action(st8, loop8, "辛", {"kind": "equip_item", "name": "传说斗篷"})
apply_action(st8, loop8, "辛", {"kind": "equip_item", "name": "守御纹章"})
ok(True, "I3 脱下传说斗篷后 2 点就能穿")

print("\n=== J. 换装不丢本场修正 ===")
st9, loop9 = fresh("壬")
loop9.set_scene_context(scene_id="s1", effects=[
    {"name": "高地", "kind": "attack", "value": 2, "target": "party",
     "scope": "scene"}])
give(st9, loop9, "壬", {"name": "旧戒指", "category": "饰品"})
u9 = loop9.find_unit("壬")
ok(any(m.get("name") == "高地" for m in u9.mods),
   "J1 ⭐ 换装后本场「高地 +2」还在", [m.get("name") for m in u9.mods])

print("\n=== K. 已装备物品改词条要复核总预算 ===")
st10, loop10 = fresh("癸")
give(st10, loop10, "癸", {"name": "传说斗篷", "category": "饰品",
                           "词条": ["ward_grace"]})
give(st10, loop10, "癸", {"name": "守御戒", "category": "饰品",
                           "词条": ["guard_1"]})
try:
    apply_action(st10, loop10, "癸", {
        "kind": "set_item_stats", "name": "守御戒",
        "词条": ["ward_minor"]})
    ok(False, "K1 已装备改到 T3 必须超预算")
except ActionError as exc:
    ok(True, "K1 ⭐ 4+3 > 5 → 改词条被拒", str(exc))
ok(st10.read("/角色列表/癸/物品/守御戒/词条") == ["guard_1"],
   "K2 被拒后旧词条没被改掉")

# --------------------------------------------------------------------------- #
# 外审 P2-4：坏存档（cooldown 非数字 / 条目非 dict）不能让回合开始抛异常
from engine.rules.item_effects import trigger_turn_start  # noqa: E402
from engine.rules.unit import Unit  # noqa: E402

for bad, label in (({"x": {"cooldown": "bad"}}, "cooldown 是字符串"),
                   ({"x": "not-a-dict"}, "条目不是 dict")):
    u = Unit(id="P", name="甲", team="pc", hp_max=10, hp=10, ac=12)
    u.item_charges = dict(bad)
    u.item_effects = [{"触发": "回合开始", "效果": "回复", "数值": 1,
                       "物品": "戒指", "名称": "回春"}]
    try:
        trigger_turn_start(u)
        ok(True, f"K3 坏存档（{label}）不打断回合开始")
    except Exception as exc:                        # noqa: BLE001
        ok(False, f"K3 坏存档（{label}）不打断回合开始",
           f"{type(exc).__name__}: {exc}")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")


