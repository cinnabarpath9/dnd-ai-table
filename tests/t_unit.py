# coding=utf-8
"""单位模型与生物解析的回归测试。"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")     # 控制台可能是 GBK，中文/符号会炸
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.rules import ABILITIES, ability_modifier, proficiency_bonus  # noqa: E402
from engine.rules.bestiary import build_unit, parse_creature  # noqa: E402
from engine.rules.unit import DamagePacket, Unit  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    # ⚠️ extra 可能是 int/list，一律 str() 一下 —— 否则调试信息本身会抛 TypeError
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


# --------------------------------------------------------------------------- #
section("A. 属性调整值与熟练加值")

for score, want in [(1, -5), (8, -1), (9, -1), (10, 0), (11, 0), (12, 1),
                    (13, 1), (20, 5), (30, 10)]:
    got = ability_modifier(score)
    ok(got == want, f"A 属性 {score} → 调整值 {want}", f"得到 {got}")

for lv, want in [(1, 2), (4, 2), (5, 3), (8, 3), (9, 4), (12, 4),
                 (13, 5), (16, 5), (17, 6), (20, 6)]:
    got = proficiency_bonus(lv)
    ok(got == want, f"A 等级 {lv} → 熟练 +{want}", f"得到 {got}")


# --------------------------------------------------------------------------- #
section("B. 数值改动的唯一入口（扣血/治疗/临时生命）")

u = Unit(id="t", name="测试", hp_max=20, hp=20)

r = u.take_damage([DamagePacket(7, "slashing")])
ok(u.hp == 13 and r.total_dealt == 7, "B1 普通扣血")

u.heal(100)
ok(u.hp == 20, "B2 治疗不超过最大生命")

u.hp = 10
u.grant_temp_hp(5)
ok(u.temp_hp == 5, "B3 给临时生命")
u.grant_temp_hp(3)
ok(u.temp_hp == 5, "B4 临时生命不叠加（取较高者）")
u.grant_temp_hp(8)
ok(u.temp_hp == 8, "B5 更高的临时生命替换低的")

u.hp, u.temp_hp = 10, 8
r = u.take_damage([DamagePacket(5, "fire")])
ok(u.temp_hp == 3 and u.hp == 10, "B6 临时生命先吸收，本体不掉血")

u.hp, u.temp_hp = 10, 3
r = u.take_damage([DamagePacket(7, "fire")])
ok(u.temp_hp == 0 and u.hp == 6, "B7 临时生命溢出部分扣本体")

u.hp = 5
r = u.take_damage([DamagePacket(99, "fire")])
ok(u.hp == 0 and r.dropped_to_zero, "B8 归零并标记")
ok(u.hp == 0 and not u.alive, "B9 归零后 alive=False")

# 抗性 / 免疫 / 易伤
u2 = Unit(id="r", name="抗性测试", hp_max=100, hp=100,
          resistances={"fire"}, immunities={"poison"}, vulnerabilities={"cold"})
u2.take_damage([DamagePacket(10, "fire")])
ok(u2.hp == 95, "B10 抗性 → 半伤（10→5）")
u2.hp = 100
u2.take_damage([DamagePacket(10, "poison")])
ok(u2.hp == 100, "B11 免疫 → 0")
u2.take_damage([DamagePacket(10, "cold")])
ok(u2.hp == 80, "B12 易伤 → 双倍（10→20）")

# 对非魔法武器的抗性
u3 = Unit(id="nm", name="非魔法抗性", hp_max=50, hp=50, resistances={"nonmagical"})
u3.take_damage([DamagePacket(10, "slashing", magical=False)])
ok(u3.hp == 45, "B13 非魔法武器抗性生效")
u3.hp = 50
u3.take_damage([DamagePacket(10, "slashing", magical=True)])
ok(u3.hp == 40, "B14 魔法武器绕过非魔法抗性")


# --------------------------------------------------------------------------- #
section("C. 状态（Condition）的机械效果")

u4 = Unit(id="c", name="状态测试", hp_max=30, hp=30, speeds={"walk": 30})
ok(u4.speed == 30, "C1 基础速度 30")

u4.add_condition("grappled")
ok(u4.speed == 0, "C2 受擒 → 速度 0")
u4.remove_condition("grappled")
ok(u4.speed == 30, "C3 解除后速度恢复")

u4.add_condition("stunned")
ok(u4.is_incapacitated, "C4 震慑 → 失能")
ok(u4.can_take("action")[0] is False, "C5 失能不能做动作")
ok(u4.can_take("reaction")[0] is False, "C6 失能不能做反应")
u4.remove_condition("stunned")

u4.add_condition("prone")
ok(not u4.is_incapacitated, "C7 倒地**不是**失能（还能动，只是有劣势）")
ok(u4.can_take("action")[0] is True, "C8 倒地仍可做动作")
u4.remove_condition("prone")

# 状态免疫
u5 = Unit(id="ci", name="免疫测试", hp_max=10, hp=10,
          condition_immunities={"poisoned"})
ok(u5.add_condition("poisoned") is False, "C9 免疫中毒 → 加不上")
ok("poisoned" not in u5.conditions, "C10 免疫的状态没被写进去")

# 状态倒计时
u6 = Unit(id="d", name="倒计时", hp_max=10, hp=10)
u6.add_condition("frightened", rounds=2)
exp = u6.tick_conditions()
ok(exp == [] and u6.conditions["frightened"] == 1, "C11 倒计时 2→1")
exp = u6.tick_conditions()
ok(exp == ["frightened"] and "frightened" not in u6.conditions, "C12 到期自动移除")

u6.add_condition("poisoned", rounds=-1)
u6.tick_conditions()
ok("poisoned" in u6.conditions, "C13 永久状态（-1）不倒计时")

# 力竭
u7 = Unit(id="e", name="力竭", hp_max=10, hp=10, speeds={"walk": 30}, exhaustion=2)
ok(u7.speed == 25, "C14 力竭 2 级 → 速度 -5（30→25）")
u7.exhaustion = 4
ok(u7.speed == 15, "C15 力竭 4 级 → 速度 -15（30→15）")


# --------------------------------------------------------------------------- #
section("D. 动作经济")

u8 = Unit(id="ae", name="动作经济", hp_max=20, hp=20)
ok(u8.can_take("action")[0], "D1 开局能做动作")
u8.spend("action")
ok(u8.can_take("action")[0] is False, "D2 用过动作后不能再做")
ok(u8.can_take("bonus_action")[0] is True, "D3 附赠动作独立于动作")
u8.spend("bonus_action")
u8.spend("reaction")
ok(u8.can_take("reaction")[0] is False, "D4 反应已用")
u8.reset_turn()
ok(u8.can_take("action")[0] and u8.can_take("bonus_action")[0], "D5 新回合重置动作")
ok(u8.can_take("reaction")[0] is True, "D6 新回合反应刷新")

u9 = Unit(id="lg", name="传奇", hp_max=100, hp=100, legendary_actions_max=3)
ok(u9.can_take("legendary")[0], "D7 传奇动作可用")
u9.spend("legendary")
u9.spend("legendary")
u9.spend("legendary")
ok(u9.can_take("legendary")[0] is False, "D8 3 次用完")
u9.reset_turn()
ok(u9.legendary_actions_used == 0, "D9 新回合传奇动作重置")

# 资源
u10 = Unit(id="rs", name="资源", hp_max=10, hp=10,
           resources={"breath": {"current": 1, "max": 1}})
ok(u10.use_resource("breath"), "D10 消耗资源成功")
ok(not u10.use_resource("breath"), "D11 资源耗尽后拒绝（不扣成负数）")
ok(u10.resources["breath"]["current"] == 0, "D12 资源计数为 0（不是 -1）")


# --------------------------------------------------------------------------- #
section("E. 从 SRD 建生物（真实数据）")

u, unparsed = build_unit("Bandit")
ok(u.name == "Bandit" and u.ac == 12 and u.hp_max == 11, "E1 强盗基础数值",
   f"AC={u.ac} HP={u.hp_max}")
ok(u.abilities.get("dexterity") == 12, "E2 属性读到了")
ok(len(u.actions) >= 2, "E3 至少有 2 个动作", f"得到 {[a.name for a in u.actions]}")

names = {a.name: a for a in u.actions}
if "Scimitar" in names:
    sc = names["Scimitar"]
    ok(sc.attacks and sc.attacks[0].attack_bonus == 3, "E4 弯刀攻击加值 +3 从描述抽出",
       f"得到 {sc.attacks[0].attack_bonus if sc.attacks else None}")
    ok(sc.attacks[0].damage_notation.startswith("1d6"), "E5 弯刀伤害骰 1d6",
       f"得到 {sc.attacks[0].damage_notation}")
    ok(sc.attacks[0].damage_type == "slashing", "E6 弯刀伤害类型 = 挥砍",
       f"得到 {sc.attacks[0].damage_type}")
    ok(sc.attacks[0].reach == 5, "E7 触及 5 尺")
else:
    ok(False, "E4~E7 找不到 Scimitar", f"动作：{list(names)}")

if "Light Crossbow" in names:
    lc = names["Light Crossbow"]
    ok(lc.attacks and lc.attacks[0].is_melee is False, "E8 轻弩是远程")
    ok(lc.attacks[0].damage_type == "piercing", "E9 轻弩伤害类型 = 穿刺（字段在 extra 里的坑）",
       f"得到 {lc.attacks[0].damage_type}")
    ok(lc.attacks[0].range_long == 320, "E10 远射程 320")
else:
    ok(False, "E8~E10 找不到 Light Crossbow", f"动作：{list(names)}")

# 龙：传奇动作 + 充能 + 双伤害类型
dragon, dun = build_unit("Adult Red Dragon")
ok(dragon.ac == 19 and dragon.hp_max == 256, "E11 成年红龙数值",
   f"AC={dragon.ac} HP={dragon.hp_max}")
ok(dragon.immunities == {"fire"}, "E12 红龙免疫火焰", f"得到 {dragon.immunities}")
ok(dragon.legendary_actions_max == 3, "E13 传奇动作 3 次/轮")
dnames = {a.name: a for a in dragon.actions}
if "Rend" in dnames:
    rd = dnames["Rend"]
    ok(rd.attacks and rd.attacks[0].attack_bonus == 14, "E14 龙爪攻击 +14",
       f"得到 {rd.attacks[0].attack_bonus if rd.attacks else None}")
    ok(rd.attacks[0].extra_damage_notation.startswith("2d4"), "E15 附加 2d4 火焰",
       f"得到 {rd.attacks[0].extra_damage_notation}")
    ok(rd.attacks[0].extra_damage_type == "fire", "E16 附加伤害类型 = 火焰")
    ok(rd.attacks[0].reach == 10, "E17 触及 10 尺")
else:
    ok(False, "E14~E17 找不到 Rend", f"动作：{list(dnames)}")

if "Fire Breath" in dnames:
    fb = dnames["Fire Breath"]
    ok(fb.save_ability == "dexterity", "E18 火焰吐息是敏捷豁免",
       f"得到 {fb.save_ability}")
    ok(fb.save_dc == 21, "E19 豁免 DC 21", f"得到 {fb.save_dc}")
    ok(fb.area_size == 60 and fb.area_shape == "cone", "E20 60 尺锥形",
       f"得到 {fb.area_size} {fb.area_shape}")
    ok(fb.recharge_on_roll == 5, "E21 充能 5-6", f"得到 {fb.recharge_on_roll}")
    ok(fb.save_success_half, "E22 豁免成功半伤")
else:
    ok(False, "E18~E22 找不到 Fire Breath", f"动作：{list(dnames)}")

ok(dragon.passive_perception > 0, "E23 被动察觉读到了")
ok(dragon.speeds.get("fly") == 80, "E24 飞行速度 80", f"得到 {dragon.speeds}")


# --------------------------------------------------------------------------- #
section("F. 解析的可信度标记（抽不到就明说，不猜）")

# 造一个只有攻击加值、没有结构化数据的动作
fake = {"name": "Mystery Strike",
        "desc": "Melee Attack Roll: +5, reach 5 ft. 9 (2d6 + 2) Slashing damage.",
        "action_type": "ACTION", "attacks": []}
rec = {"name": "Fake", "armor_class": 10, "hit_points": 1,
       "ability_scores": {k: 10 for k in ABILITIES}, "actions": [fake]}
fu, fun = parse_creature(rec)
fa = fu.actions[0]
ok(fa.structured is False, "F1 无结构化 attacks[] → 标记为不自动结算")
ok(fa.name in fun, "F2 该动作进了「未解析」清单", f"得到 {fun}")


# --------------------------------------------------------------------------- #
section("G. ⭐ 属性名中英双语（真出过的 bug）")

# 背景：`stat_data` 那棵树用中文键（力量/敏捷…），SRD 数据用英文键（strength…），
# 两个来源会混着进来。`combat_block()` 曾经只认英文，中文键直接 KeyError。
# 这个 bug 是在 demo_state（状态层 → Unit）那条路上才暴露出来的。

u_cn = Unit(id="CN", name="中文属性", hp_max=20, hp=20, level=3,
            abilities={"力量": 16, "敏捷": 14, "体质": 15,
                       "智力": 10, "感知": 12, "魅力": 10},
            save_proficiencies={"strength", "力量"})
ok(u_cn.mod("力量") == 3, "G1 中文键取调整值", u_cn.mod("力量"))
ok(u_cn.mod("strength") == 3, "G2 同一份数据用英文键也能取到", u_cn.mod("strength"))
ok(u_cn.save_total("力量") == 3 + 2, "G3 中文键的豁免总加值（含熟练 +2）",
   u_cn.save_total("力量"))
ok(u_cn.save_total("strength") == 5, "G4 英文键的豁免总加值一致")
ok(u_cn.save_total("敏捷") == 2, "G5 没熟练的项只有属性调整值")

try:
    block = u_cn.combat_block()
    ok("中文属性" in block, "G6 ⭐ combat_block() 不吃中文属性名（曾经 KeyError）")
    ok("STR" in block and "DEX" in block, "G7 豁免渲染成英文缩写", block)
except KeyError as exc:
    ok(False, "G6 ⭐ combat_block() 不吃中文属性名（曾经 KeyError）", f"KeyError {exc}")

u_en = Unit(id="EN", name="英文属性", hp_max=20, hp=20,
            abilities={"strength": 16, "dexterity": 14})
ok("STR" in u_en.combat_block(), "G8 英文键照样渲染")
ok(u_en.mod("力量") == 3, "G9 英文数据用中文名查也认")

u_mix = Unit(id="MX", name="混用", hp_max=10, hp=10, level=1,
             abilities={"力量": 16, "dexterity": 14, "体质": 15})
b = u_mix.combat_block()
ok(b.index("STR") < b.index("DEX") < b.index("CON"),
   "G10 ⭐ 中英混用时豁免顺序仍是 STR→DEX→CON", b)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")
