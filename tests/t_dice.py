# coding=utf-8
"""骰子与检定的回归测试。

跑法（在项目根 dnd-campaign/ 下）：
    python tests/run_all.py            # 全部
    python tests/run_all.py dice       # 只跑名字含 dice 的
"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import (  # noqa: E402
    Roll, RollError, RollMods, advantage_of, parse, roll, roll_d20,
)

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  PASS {label}")
    else:
        print(f"  FAIL {label}{('  |  ' + extra) if extra else ''}")
        FAILS.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
section("A. 骰式解析")

terms, mod, halves = parse("2d6+3")
ok(len(terms) == 1 and terms[0].count == 2 and terms[0].faces == 6, "A1 2d6+3 → 1 项 +3")
ok(mod == 3 and halves == 0, "A2 加值 3、无除二")

terms, mod, halves = parse("3d6+2d4+5")
ok(len(terms) == 2 and mod == 5, "A3 多骰项 3d6+2d4+5")

terms, mod, halves = parse("d20")
ok(terms[0].count == 1 and terms[0].faces == 20, "A4 省略颗数 = 1 颗")

terms, _, _ = parse("d%")
ok(terms[0].faces == 100, "A5 d% = d100")

terms, _, halves = parse("8d6/2")
ok(halves == 1, "A6 末尾 /2 计一层")

terms, _, _ = parse("2d20kh1")
ok(terms[0].keep == "kh" and terms[0].keep_n == 1, "A7 2d20kh1 保留最高")

for bad, why in [("", "空"), ("abc", "不是骰式"), ("2d6+", "残缺加号"),
                 ("0d6", "零颗"), ("1d1", "一面骰"), ("2d20kh3", "保留数超颗数")]:
    try:
        parse(bad)
        ok(False, f"A8 非法骰式应报错：{bad!r}", "没有抛异常")
    except RollError:
        ok(True, f"A8 非法骰式报错：{bad!r}（{why}）")


# --------------------------------------------------------------------------- #
section("B. 掷骰与可复现性")

r1 = roll("4d6", random.Random(42))
r2 = roll("4d6", random.Random(42))
ok(r1.detail == r2.detail and r1.total == r2.total, "B1 同种子同结果（可复现）",
   f"{r1.detail} vs {r2.detail}")

rng = random.Random(1)
r = roll("10d6", rng)
ok(r.dice_total == sum(sum(k) for k in r.kept), "B2 总点数 = 保留骰之和")
ok(all(1 <= v <= 6 for grp in r.detail for v in grp), "B3 d6 面值都在 1..6")

r = roll("2d20kh1", random.Random(7))
ok(r.kept[0][0] == max(r.detail[0]), "B4 kh1 保留最大值")
ok(len(r.dropped[0]) == 1, "B5 kh1 丢掉 1 颗")

r = roll("4d6dl1", random.Random(7))
ok(sum(r.kept[0]) == sum(r.detail[0]) - min(r.detail[0]), "B6 dl1 丢掉最小值")

r = roll("8d6/2", random.Random(3))
ok(r.total == (r.dice_total) // 2, "B7 /2 是整除后计入总数")

r = roll("2d6+3", random.Random(5))
ok(r.total == r.dice_total + 3, "B8 固定加值计入总数")


# --------------------------------------------------------------------------- #
section("C. d20 的天然值、优势/劣势")

r = roll_d20(random.Random(11), 5)
ok(r.natural is not None and 1 <= r.natural <= 20, "C1 单颗 d20 记下裸骰")
ok(r.total == r.natural, "C2 骰子本身只报裸骰（加值由 AbilityCheck 加一次）")
ok("+5" in r.summary(), "C2b 摘要里体现加值（仅供人看）")

r = roll_d20(random.Random(11), 5, RollMods(advantage=True))
ok(len(r.advantage_rolls) == 2, "C3 优势掷两颗 d20")
ok(r.natural == max(r.advantage_rolls), "C4 优势取高")
ok(r.total == r.natural, "C5 优势后骰子仍只报裸骰")

r = roll_d20(random.Random(11), 0, RollMods(disadvantage=True))
ok(r.natural == min(r.advantage_rolls), "C6 劣势取低")

r = roll_d20(random.Random(11), 0, RollMods(advantage=True, disadvantage=True))
ok(r.advantage_rolls == [], "C7 优势+劣势互相抵消（只掷一颗）")

# 天然 20 / 天然 1 的判定只认裸骰
r = Roll(notation="d20", terms=[parse("d20")[0]], modifier=100)
r.natural = 20
ok(r.is_crit, "C8 裸骰 20 → 重击（哪怕总值算上加值）")
r.natural = 19
ok(not r.is_crit, "C9 裸骰 19 + 巨额加值 → **不是**重击")
r.natural = 1
ok(r.is_crit_fail, "C10 裸骰 1 → 必失败")

mods = advantage_of(True, False, True, True)
ok(mods.resolved_advantage() == 0, "C11 advantage_of 折叠：有优有劣 → 抵消")


# --------------------------------------------------------------------------- #
section("D. 检定结果（成功/失败/边界）")

from engine.dice import AbilityCheck  # noqa: E402

# 天然 20/1 只对**攻击检定**成立：属性/技能检定和豁免只看总点数（2024）
a = roll_d20(random.Random(0), 0)
a.natural, a.kept, a.detail = 20, [[20]], [[20]]
chk = AbilityCheck(kind="check", label="测试", a=a, bonus=0, dc=100)
ok(not chk.success, "D1a 天然 20 在属性/技能检定上**不**自动成功（DC 100 → 失败）")

chk_atk = AbilityCheck(kind="attack", label="攻击", a=a, bonus=0, dc=100)
ok(chk_atk.success, "D1b 天然 20 在攻击检定上必中（即使 AC 100）")

a2 = roll_d20(random.Random(0), 50)
a2.natural, a2.kept, a2.detail = 1, [[1]], [[1]]
chk2 = AbilityCheck(kind="check", label="测试", a=a2, bonus=50, dc=5)
ok(chk2.success, "D2a 天然 1 在检定上**不**自动失败（加值足够仍成功）")

chk2_atk = AbilityCheck(kind="attack", label="攻击", a=a2, bonus=50, dc=5)
ok(not chk2_atk.success, "D2b 天然 1 在攻击检定上必失（即使加值巨大）")

a3 = roll_d20(random.Random(0), 3)
a3.natural, a3.kept, a3.detail = 12, [[12]], [[12]]
chk3 = AbilityCheck(kind="attack", label="攻击", a=a3, bonus=3, dc=15)
ok(chk3.success and chk3.total == 15, "D3 总计恰好等于 DC → 成功")
chk4 = AbilityCheck(kind="attack", label="攻击", a=a3, bonus=3, dc=16)
ok(not chk4.success, "D4 总计 DC-1 → 失败")
ok(chk3.margin == 0 and chk4.margin == -1, "D5 margin 算得对")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")
