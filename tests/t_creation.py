# coding=utf-8
"""U14 回归：角色创建 / 升级向导（SRD 查表 + 规则算数，零模型调用）。

守的口径（写在 `engine/creation.py` 顶部）：

    · 职业/种族必须能在本地 SRD 查到；子职不能当基础职业选；
    · 属性显式给（六项、1..30），掷骰可复现，不替玩家选；
    · HP / 生命骰 / 豁免 / 法术位从 SRD 与表格算，不手搓；
    · 法术必须在该职业表里、且不超过最高环阶；半施法/契约法术 v1 不自动配；
    · 升级只支持单职业 1~5 级；HP/生命骰/法术位按表涨，有审计。
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

from engine import demo_create  # noqa: E402
from engine.creation import (  # noqa: E402
    CLASS_CN, CreationError, class_info, create_character, level_up,
    list_options, roll_abilities, spells_for, species_info, standard_array,
)
from engine.dice import Random  # noqa: E402
from engine.rules.loop import make_hero_unit  # noqa: E402
from engine.state import StateTree  # noqa: E402
from engine.state.patch import escape_token  # noqa: E402

FAILS: list[str] = []
COUNT = 0

WIZ_AB = {"力量": 8, "敏捷": 14, "体质": 13, "智力": 15, "感知": 12, "魅力": 10}


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


def wizard(**kw) -> dict:
    args = dict(klass="Wizard", species="Elf", level=3, abilities=dict(WIZ_AB),
                skills=["奥秘", "调查"],
                spells=["Fire Bolt", "Magic Missile"])
    args.update(kw)
    return create_character("莉安", **args)


# --------------------------------------------------------------------------- #
section("A. SRD 查询：职业 / 种族 / 法术表")

w = class_info("法师")
ok(w["hit_die"] == 6 and w["caster"] == "FULL",
   "A1 法师：d6、全施法", w)
ok(w["saves"] == ["智力", "感知"], "A2 法师豁免是智力/感知", w["saves"])
f = class_info("Fighter")
ok(f["hit_die"] == 10 and "力量" in f["saves"],
   "A3 战士：d10、豁免含力量", f)
ok(raises(lambda: class_info("Champion"), CreationError),
   "A4 ⭐ 子职业不能当基础职业选")
ok(raises(lambda: class_info("驯龙师"), CreationError),
   "A5 不认识的职业 → 报错")

sp = species_info("Elf")
ok(sp["speed"] == 30, "A6 精灵速度 30", sp["speed"])
ok(any(t["名"] == "Darkvision" for t in sp["traits"]),
   "A7 精灵特性里有黑暗视觉", [t["名"] for t in sp["traits"]])
ok(raises(lambda: species_info("半精灵"), CreationError),
   "A8 不认识的种族 → 报错")

wiz1 = {s["name"] for s in spells_for("Wizard", max_level=1)}
ok("Fire Bolt" in wiz1, "A9 法师 1 环表里有 Fire Bolt")
ok(all(s["level"] <= 1 for s in spells_for("Wizard", max_level=1)),
   "A10 限定环阶真的生效")
ok(spells_for("Fighter", max_level=1) == [],
   "A11 战士没有法术表（空清单，不漏别人的法术）")
ok(spells_for("Wizard", max_level=2) != spells_for("Wizard", max_level=1),
   "A12 放宽环阶能多出法术")

opts = list_options()
ok(len(opts["classes"]) == 12 and len(opts["species"]) == 9,
   "A13 选项清单：12 职业 / 9 种族",
   (len(opts["classes"]), len(opts["species"])))
ok(len(opts["skills"]) == 18, "A14 18 项技能", len(opts["skills"]))


# --------------------------------------------------------------------------- #
section("B. 属性：显式、可复现、不越界")

ok(standard_array() == [15, 14, 13, 12, 10, 8], "B1 标准数组是 15/14/13/12/10/8")
r1 = roll_abilities(Random(2026))
r2 = roll_abilities(Random(2026))
ok(r1 == r2, "B2 ⭐ 同一个种子掷出同一组属性", (r1, r2))
ok(all(3 <= v <= 18 for v in r1.values()),
   "B3 4d6 取高 3 落在 3..18", r1)
ok(raises(lambda: wizard(abilities={"力量": 10}), CreationError),
   "B4 属性缺项 → 报错（不补默认）")
ok(raises(lambda: wizard(abilities={**WIZ_AB, "智力": 31}), CreationError),
   "B5 属性超范围 → 报错")
ok(raises(lambda: wizard(abilities={**WIZ_AB, "智力": "15"}), CreationError),
   "B6 属性是字符串 → 报错")
ok(raises(lambda: wizard(abilities={**WIZ_AB, "智力": True}), CreationError),
   "B7 属性是布尔 → 报错")


# --------------------------------------------------------------------------- #
section("C. 建卡：HP / AC / 熟练 / 法术位 / 法术书")

char = wizard()
hp = char["生命值"]
ok(hp["最大"] == 17 and hp["当前"] == 17,
   "C1 3 级法师 HP = 6+1 + 2×(3+1+1) = 17", hp)
ok(hp["生命骰"] == {"当前": 3, "总数": 3, "面数": "d6"},
   "C2 生命骰面数是 d6、数量跟等级", hp["生命骰"])
ok(char["护甲等级"] == 12, "C3 ⭐ 无甲 AC = 10 + 敏捷调整(2)",
   char["护甲等级"])
ok((char.get("熟练配置") or {}).get("技能") == {"arcana": 1, "investigation": 1},
   "C4 技能熟练落到英文键", char.get("熟练配置"))
ok((char.get("熟练配置") or {}).get("豁免") == ["智力", "感知"],
   "C5 豁免来自 SRD")
slots = (char.get("施法") or {}).get("法术位") or {}
ok(slots == {"1": {"当前": 4, "最大": 4}, "2": {"当前": 2, "最大": 2}},
   "C6 ⭐ 3 级全施法者法术位 4/2", slots)
book = (char.get("施法") or {}).get("法术书") or {}
ok(set(book) == {"Fire Bolt", "Magic Missile"}, "C7 法术书写进两条", list(book))
ok(all(("等级" in v and "准备中" in v and "学派" in v)
       for v in book.values()), "C8 法术条目字段齐全", book)
ok(char.get("速度") == {"步行": 30}, "C9 速度来自种族", char.get("速度"))
ok("Elf" in (char.get("特性") or {}), "C10 种族特性带来源分组",
   list(char.get("特性") or {}))

ok(raises(lambda: wizard(spells=["Cure Wounds"]), CreationError),
   "C11 ⭐ 不在本职业法术表里的法术 → 报错")
ok(raises(lambda: wizard(level=1, spells=["Scorching Ray"]), CreationError),
   "C12 法术环阶超过最高环阶 → 报错")
ok(raises(lambda: wizard(skills=["奥秘"]), CreationError),
   "C13 技能数量不对 → 报错")
ok(raises(lambda: wizard(skills=["奥秘", "驯龙"]), CreationError),
   "C14 不认识的技能 → 报错")
ok(raises(lambda: create_character(
        "战", klass="Fighter", species="Human", level=1,
        abilities=dict(WIZ_AB), skills=["运动", "体操"],
        spells=["Fire Bolt"]), CreationError),
   "C15 非施法职业不能选法术")
ok(raises(lambda: wizard(level=6), CreationError),
   "C16 v1 只支持 1~5 级")
ok(raises(lambda: create_character(
        "", klass="Wizard", species="Elf", abilities=dict(WIZ_AB),
        skills=["奥秘", "调查"]), CreationError),
   "C17 空名字 → 报错")
ok(raises(lambda: create_character(
        "契", klass="Warlock", species="Human", level=1,
        abilities=dict(WIZ_AB), skills=["奥秘", "调查"],
        spells=["Eldritch Blast"]), CreationError),
   "C18 邪术师契约法术位 v1 不自动配（明说）")

st = StateTree.from_template({"莉安": char}, current="莉安")
ok(st.read("/角色列表/莉安/职业") == {"法师": 3},
   "C19 建出来的角色能直接放进状态树（schema 通过）")
unit = make_hero_unit(st, "莉安")
ok(len(unit.spells) == 2 and unit.spell_slots.get(1, {}).get("max") == 4,
   "C20 ⭐ 建出来的法师能直接上桌：两个法术 + 1 环位 4",
   (len(unit.spells), unit.spell_slots))


# --------------------------------------------------------------------------- #
section("D. 升级：单职业 1~5 级")

st = StateTree.from_template({"莉安": wizard()}, current="莉安")
out = level_up(st, "莉安")
hp = st.read("/角色列表/莉安/生命值")
ok(out["level"] == 4 and hp["最大"] == 22,
   "D1 3→4 级：HP 17→22（+5）", hp)
ok(hp["生命骰"]["当前"] == 4 and hp["生命骰"]["总数"] == 4,
   "D2 生命骰跟着涨")
slots = st.read("/角色列表/莉安/施法/法术位")
ok(slots["2"]["最大"] == 3, "D3 2 环位涨到 3", slots)
ok(int(slots["1"]["当前"]) == int(slots["1"]["最大"]),
   "D4 升级后法术位按「长休」回满", slots)

out = level_up(st, "莉安", abilities={"智力": 16},
               spells=["Scorching Ray"])
ok(st.read("/角色列表/莉安/属性/智力") == 16,
   "D5 属性提升要显式给（不自动 +1）")
ok("Scorching Ray" in (st.read("/角色列表/莉安/施法/法术书") or {}),
   "D6 升级时能加新法术")
ok(any("升级" in e.reason for e in st.audit),
   "D7 升级有审计", [e.line() for e in st.audit][-2:])

ok(raises(lambda: level_up(st, "莉安"), CreationError),
   "D8 满 5 级再升 → 报错（v1 上限）")
ok(raises(lambda: level_up(st, "不存在"), CreationError),
   "D9 升不存在的人 → 报错")

st2 = StateTree.from_template(
    {"多职业": {**wizard(), "职业": {"法师": 2, "战士": 1}}}, current="多职业")
ok(raises(lambda: level_up(st2, "多职业"), CreationError),
   "D10 ⭐ 多职业在 v1 里明说「做不了」，不挑一个瞎升")


# --------------------------------------------------------------------------- #
section("E. 向导 demo：无人值守也能跑完")

rc = demo_create.auto()
ok(rc == 0, "E1 ⭐ `demo_create.auto()` 无输入跑完（建卡 + 升级）", rc)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")