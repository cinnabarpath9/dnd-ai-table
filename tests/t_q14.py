# coding=utf-8
"""Q14 收尾：词汇表单一来源、组装函数共用、触及、同名充能、内置 id 兜底。"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.rules import effect_catalog as cat  # noqa: E402
from engine.rules import item_effects as ie  # noqa: E402
from engine.rules.homebrew import build_action  # noqa: E402
from engine.state import StateTree, World, WorldError  # noqa: E402
from engine.state.tree import StateTree as _ST  # noqa: E402,F401

FAILS: list[str] = []
COUNT = 0


def ok(cond, label, extra=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(label)
        print(f"  ✗ {label}  extra={extra!r}")


def read(p):
    return io.open(p, encoding="utf-8").read()


# ---- 词汇表 ----
ok(ie.TRIGGERS is cat.TRIGGERS, "A1 ⭐ 触发词只有一份（item_effects 直接引用）")
ok(ie.EFFECTS is cat.EFFECTS, "A2 效果词只有一份")
ok(ie.VALID_PAIRS is cat.VALID_PAIRS, "A3 合法组合只有一份")
src = read(os.path.join(ROOT, "engine/rules/item_effects.py"))
ok("_TRIGGER_ALIASES" not in src and "_EFFECT_ALIASES" not in src,
   "A4 item_effects 不再自带别名表")

norm = cat.validate_entry({"名称": "别名测试", "品级": 3, "适用": "装备",
                           "触发": "lethal", "效果": "death_ward", "数值": 0})
ok(norm["触发"] == "致命伤害" and norm["效果"] == "免死",
   "A5 ⭐ 写入口也认英文别名（以前只认中文）", norm)


# ---- 组装函数共用 ----
ok("apply_unit_traits" in read(os.path.join(ROOT, "engine/rules/loop.py")),
   "B1 make_hero_unit 用公共组装")
ok("apply_unit_traits" in read(os.path.join(ROOT, "engine/rules/npc_units.py")),
   "B2 build_npc_unit 用公共组装")


class FakeUnit:
    def __init__(self):
        self.item_effects = []
        self.mods = []
        self.notes = []
        self.hp_max = 10
        self.hp = 10
        self.item_charges = {}


u = FakeUnit()
ie.apply_unit_traits(u, [{"名称": "免死甲", "物品": "甲"}],
                     [{"kind": "ac", "value": 1}], 5)
ok(u.hp_max == 15 and u.hp == 15, "B3 生命加值生效")
ok(u.mods == [{"kind": "ac", "value": 1}], "B4 常驻修正挂上")
ok(any("免死甲" in n for n in u.notes), "B5 说明写进 notes", u.notes)


# ---- 同名充能 ----
effs = [{"名称": "回气", "物品": "护符", "每场": 1},
        {"名称": "回气", "物品": "护符", "每场": 2}]
charges = ie.build_charges(effs)
ok(len(charges) == 2, "C1 ⭐ 同名特效不再互相覆盖", list(charges))
ok(effs[0].get("_charge_key") != effs[1].get("_charge_key"),
   "C2 两条各有 key", [e.get("_charge_key") for e in effs])

cu = FakeUnit()
cu.item_charges = {
    "旧剑:回气": {"max": 1, "current": 1, "cooldown": 0},
}
renamed = {"名称": "改过名", "物品": "旧剑", "每场": 1}
ok(ie.available(cu, renamed) is True,
   "C3 ⭐ 旧档/改名后按物品唯一记录找回（不再静默 False）", cu.item_charges)
cu2 = FakeUnit()
cu2.item_charges = {"旧剑:甲": {"max": 1, "current": 1, "cooldown": 0},
                    "旧剑:乙": {"max": 1, "current": 1, "cooldown": 0}}
ok(ie.available(cu2, {"名称": "第三个", "物品": "旧剑"}) is False,
   "C4 物品下有多条时不乱认（宁可 False）")

# ---- 触及 ----
act, notes = build_action("长柄刀", {"类型": "attack", "攻击加值": 5,
                                     "伤害骰": "1d10", "伤害类型": "挥砍",
                                     "触及": 10}, level=3,
                          abilities={"力量": 16})
ok(act.attacks and act.attacks[0].reach == 10, "D1 ⭐ 自定义武器「触及」生效",
   act.attacks[0].reach if act.attacks else None)
ok(not any("未知字段" in n for n in notes), "D2 触及不再被判未知字段", notes)

# ---- 内置 id 兜底 ----
st = StateTree.from_template({}, current="")
w = World(st)
builtin_id = cat.ENTRIES[0]["id"]
try:
    w.register_entry(builtin_id, {"名称": "冒充"})
    ok(False, "E1 内置 id 必须拒绝")
except WorldError as exc:
    ok(builtin_id in str(exc), "E1 ⭐ 内置 id 拒绝并点名", str(exc))
w.register_entry("my_custom_1", {"名称": "自写", "品级": 1, "适用": "通用",
                                 "触发": "常驻", "kind": "ac", "数值": 1})
ok("my_custom_1" in w.entries(), "E2 自写 id 正常写入")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for x in FAILS:
        print(f"  ✗ {x}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")