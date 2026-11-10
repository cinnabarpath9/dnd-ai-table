# coding=utf-8
"""法术层回归：法术解析 + 施法结算。"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.rules.adjudicate import (  # noqa: E402
    break_concentration, cast_spell, concentration_check, grid_distance_ft,
    spell_attack_bonus, spell_save_dc,
)
from engine.rules.spells import (  # noqa: E402
    get_spell, list_spells, parse_spell,
)
from engine.rules.unit import Unit  # noqa: E402

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


def wizard(name="罗恩", uid="W1", **kw) -> Unit:
    """3 级法师：智力 16、魅力 10（用智力施法）。带 1~3 环法术位。"""
    base = dict(
        id=uid, name=name, team="pc", is_pc=True, level=3, ac=12,
        hp_max=18, hp=18, speeds={"walk": 30},
        abilities={"力量": 8, "敏捷": 14, "体质": 12,
                   "智力": 16, "感知": 12, "魅力": 10},
        save_proficiencies={"智力", "感知"},
        spell_slots={1: {"current": 4, "max": 4},
                     2: {"current": 2, "max": 2},
                     3: {"current": 0, "max": 0}},
    )
    base.update(kw)
    return Unit(**base)


def dummy(name="靶子", uid="T1", hp=100, ac=10) -> Unit:
    return Unit(id=uid, name=name, team="enemy", hp_max=hp, hp=hp, ac=ac)


# --------------------------------------------------------------------------- #
section("A. 法术解析（真实 SRD 数据）")

fb = get_spell("Fireball")
ok(fb.name == "Fireball" and fb.level == 3, "A1 火球术 3 环", (fb.name, fb.level))
ok(fb.damage_roll == "8d6", "A2 伤害骰 8d6", fb.damage_roll)
ok(fb.damage_type == "fire", "A3 伤害类型火", fb.damage_type)
ok(fb.save_ability == "dexterity", "A4 敏捷豁免", fb.save_ability)
ok(fb.save_success_half, "A5 豁免成功半伤")
ok(fb.shape_type == "sphere" and fb.shape_size == 20, "A6 20 尺球体",
   (fb.shape_type, fb.shape_size))
ok(fb.range_ft == 150, "A7 射程 150 尺", fb.range_ft)
ok(not fb.concentration, "A8 火球不需要专注")
ok(fb.structured, "A9 火球是结构化可自动结算的", fb.unstructured_reason)

# 升环：SRD 直接给 casting_options，用它最准
ok(fb.damage_at(4) == "9d6", "A10 ⭐ 4 环升环 = 9d6（用 SRD 的 casting_options）",
   fb.damage_at(4))
ok(fb.damage_at(5) == "10d6", "A11 5 环 = 10d6", fb.damage_at(5))
ok(fb.damage_at(3) == "8d6", "A12 3 环 = 8d6")

# 法术攻击型
aa = get_spell("Acid Arrow")
ok(aa.attack_roll, "A13 酸箭是法术攻击检定")
ok(aa.damage_roll == "4d4", "A14 酸箭伤害 4d4", aa.damage_roll)
ok(aa.damage_type == "acid", "A15 酸属性")

# 无判定型（直接结算）
mm = get_spell("Magic Missile")
ok(mm.level == 1, "A16 魔法飞弹 1 环", mm.level)
ok(not mm.attack_roll and not mm.save_ability, "A17 魔法飞弹无攻击检定也无豁免")

# 治疗（只能从文本抽）
cw = get_spell("Cure Wounds")
ok(cw.heal_roll == "2d8", "A18 ⭐ 治疗术 2d8（从 desc 抽出来的）", cw.heal_roll)
ok(cw.heal_adds_modifier, "A19 治疗术要加施法属性调整值")

# 专注
hold = get_spell("Hold Person")
ok(hold.concentration, "A20 定身人类需要专注")
ok(not hold.structured, "A21 ⭐ 控制法术标记为「需 DM 裁定」",
   hold.unstructured_reason)
ok("DM 裁定" in hold.unstructured_reason, "A22 理由写明了", hold.unstructured_reason)

# 施法时间 / 动作槽
hw = get_spell("Healing Word")
ok(hw.slot == "bonus_action", "A23 ⭐ 治愈真言是附赠动作", hw.slot)
ok(get_spell("Shield").slot == "reaction", "A24 ⭐ 护盾术是反应",
   get_spell("Shield").slot)

# 戏法
ok(get_spell("Fire Bolt").is_cantrip, "A25 火焰箭是戏法")

# 列表与过滤
wiz1 = list_spells(level=1, klass="Wizard")
ok(len(wiz1) > 5, "A26 按职业+环阶过滤能查出法师 1 环法术", len(wiz1))
ok(all(s.level == 1 for s in wiz1), "A27 过滤结果环阶都对")
struct = list_spells(only_structured=True)
ok(0 < len(struct) < 339, "A28 只取可自动结算的法术", f"{len(struct)}/339")

# 缺字段要报错
ok(raises(lambda: parse_spell({}), Exception), "A29 缺 name 报错")

# 升环治疗：SRD 把治疗量放在 casting_options 的 damage_roll 里
ok(cw.heal_at(2) == "4d8",
   "A30 ⭐ 治疗术升 2 环 = 4d8（不是兜底算法的 3d8）", cw.heal_at(2))
ok(cw.heal_at(3) == "6d8", "A31 3 环 = 6d8", cw.heal_at(3))


# --------------------------------------------------------------------------- #
section("B. 施法：能不能施（资源约束由引擎把关）")

w = wizard()
t = dummy()

# 施法属性用智力（法师）。wizard() 默认 3 环是 0 —— 先验"该拒绝"
r = cast_spell(w, fb, [t], Random(1), cast_ability="intelligence")
ok(not r.ok and "法术位不足" in r.blocked_reason,
   "B1 ⭐ 3 环位为 0 → 拒绝施放火球（引擎守住资源约束）", r.blocked_reason)
ok(not r.slot_consumed, "B2 被拒绝时没有扣法术位")
ok(w.spell_slots[3]["current"] == 0, "B3 法术位仍是 0（没被扣成负数）")

w2 = wizard()
w2.spell_slots[3] = {"current": 1, "max": 1}
r2 = cast_spell(w2, fb, [t], Random(1), cast_ability="intelligence")
ok(r2.ok and r2.slot_consumed, "B4 有 3 环位时施放成功", r2.blocked_reason)
ok(w2.spell_slots[3]["current"] == 0, "B5 3 环法术位 1 → 0")

# 法术位不够
w3 = wizard()
w3.spell_slots = {1: {"current": 0, "max": 0}}
r3 = cast_spell(w3, fb, [t], Random(1), cast_ability="intelligence")
ok(not r3.ok and "法术位不足" in r3.blocked_reason, "B6 ⭐ 法术位不足 → 直接拒绝",
   r3.blocked_reason)

# 失能不能施法
w4 = wizard()
w4.spell_slots[3] = {"current": 1, "max": 1}
w4.add_condition("stunned")
r4 = cast_spell(w4, fb, [t], Random(1), cast_ability="intelligence")
ok(not r4.ok and "失能" in r4.blocked_reason, "B7 ⭐ 失能 → 拒绝施法",
   r4.blocked_reason)

# 倒地不能施法
w5 = wizard(hp=0)
r5 = cast_spell(w5, fb, [t], Random(1), cast_ability="intelligence")
ok(not r5.ok and "倒地" in r5.blocked_reason, "B8 倒地 → 拒绝")

# 戏法不扣法术位
w6 = wizard()
w6.actions = []
fb2 = get_spell("Fire Bolt")
r6 = cast_spell(w6, fb2, [t], Random(1), cast_ability="intelligence")
ok(r6.ok and not r6.slot_consumed, "B9 ⭐ 戏法不消耗法术位", r6.blocked_reason)

# 动作经济：动作已用 → 不能施"动作"法术
w7 = wizard()
w7.spell_slots[3] = {"current": 1, "max": 1}
w7.spend("action")
r7 = cast_spell(w7, fb, [t], Random(1), cast_ability="intelligence")
ok(not r7.ok and "已用过" in r7.blocked_reason, "B10 ⭐ 动作已用 → 拒绝施法",
   r7.blocked_reason)

# 附赠动作法术用附赠槽
w8 = wizard()
r8 = cast_spell(w8, hw, [t], Random(1), cast_ability="intelligence")
ok(r8.ok, "B11 附赠动作法术能施", r8.blocked_reason)
ok(not w8.can_take("bonus_action")[0], "B12 ⭐ 附赠动作被占掉了")
ok(w8.can_take("action")[0], "B13 动作还能用（两个槽独立）")

# 升环
w9 = wizard()
w9.spell_slots[1] = {"current": 2, "max": 2}
r9 = cast_spell(w9, mm, [t], Random(1), slot_level=1, cast_ability="intelligence")
ok(r9.ok and w9.spell_slots[1]["current"] == 1, "B14 用 1 环位施魔法飞弹",
   r9.blocked_reason)


# --------------------------------------------------------------------------- #
section("C. 施法结算：三条判定路")

# 1) 豁免型 AoE
w10 = wizard()
t1, t2 = dummy("目标1", "T1", hp=100), dummy("目标2", "T2", hp=100)
w10.spell_slots[3] = {"current": 1, "max": 1}
res = cast_spell(w10, fb, [t1, t2], Random(7), cast_ability="intelligence")
ok(res.ok, "C1 火球施放", res.blocked_reason)
ok(res.save is not None, "C2 走了豁免路（不是攻击路）")
ok(len(res.save.targets) == 2, "C3 两个目标各自结算", len(res.save.targets))
ok(t1.hp < 100 or t2.hp < 100, "C4 ⭐ 至少有一个目标掉血了",
   (t1.hp, t2.hp))
ok(res.save.save_dc == 8 + 2 + 3, "C5 豁免 DC = 8 + 熟练2 + 智力3 = 13",
   res.save.save_dc)
print("    " + res.summary().replace("\n", "\n    "))

# 半伤检查：找一个豁免成功的案例
found_half = False
for seed in range(60):
    a, b = dummy("A", "A", hp=200), dummy("B", "B", hp=200)
    ww = wizard()
    ww.spell_slots[3] = {"current": 1, "max": 1}
    rr = cast_spell(ww, fb, [a, b], Random(seed), cast_ability="intelligence")
    if rr.save:
        for row in rr.save.targets:
            if row.get("success") and row.get("damage_note", "").startswith("豁免成功"):
                found_half = True
                break
    if found_half:
        break
ok(found_half, "C6 ⭐ 豁免成功时明确写了「半伤」")

# 2) 法术攻击型
w11 = wizard()
t3 = dummy("靶子", "T3", hp=100, ac=10)
hit_found = False
for seed in range(60):
    tt = dummy("靶子", "T3", hp=100, ac=10)
    ww = wizard()
    ww.spell_slots[1] = {"current": 4, "max": 4}
    rr = cast_spell(ww, aa, [tt], Random(seed), cast_ability="intelligence")
    if rr.attack and rr.attack.hit:
        hit_found = True
        ok(rr.attack.check.kind == "attack", "C7 走了攻击检定路")
        ok(tt.hp < 100, "C8 ⭐ 命中后目标掉血", tt.hp)
        break
ok(hit_found, "C9 60 个种子里有命中（法术攻击 +5 vs AC 10）")

# 3) 无判定直伤
w12 = wizard()
t4 = dummy("靶子", "T4", hp=100, ac=99)   # AC 极高，说明不是靠攻击命中
w12.spell_slots[1] = {"current": 2, "max": 2}
r12 = cast_spell(w12, mm, [t4], Random(3), slot_level=1, cast_ability="intelligence")
ok(r12.ok and t4.hp < 100, "C10 ⭐ 无判定法术直接结算（AC 99 也照打）",
   (r12.ok, t4.hp))
ok(r12.attack is None and r12.save is None, "C11 既没攻击也没豁免")

# 治疗
w13 = wizard()
hurt = dummy("受伤的", "H1", hp=100)
hurt.hp = 40
r13 = cast_spell(w13, cw, [hurt], Random(5), cast_ability="intelligence")
ok(r13.ok and hurt.hp > 40, "C12 ⭐ 治疗术真的回了血", (r13.ok, hurt.hp))
ok(hurt.hp <= 100, "C13 治疗不超过最大生命")
print("    " + r13.summary().replace("\n", "\n    "))


# --------------------------------------------------------------------------- #
section("D. 专注")

w14 = wizard()
hold = get_spell("Hold Person")
w14.spell_slots[2] = {"current": 2, "max": 2}
r14 = cast_spell(w14, hold, [t1], Random(1), cast_ability="intelligence")
ok(r14.ok and w14.concentrating_on == "Hold Person",
   "D1 ⭐ 专注型法术开始专注（状态挂在施法者身上）",
   (r14.ok, w14.concentrating_on))
ok(r14.needs_dm_ruling, "D2 控制效果标记需 DM 裁定")
ok(r14.concentration_started == "Hold Person", "D3 留痕里有开始专注")

# 再施一个专注法术 → 打断旧的（要新回合，否则动作经济会先拦住）
w14.reset_turn()
r15 = cast_spell(w14, hold, [t1], Random(1), cast_ability="intelligence")
ok(any("打断" in n for n in r15.notes), "D4 ⭐ 再施专注法术会打断旧专注", r15.notes)
ok(w14.concentrating_on == "Hold Person", "D5 新专注接管")
ok(w14.spell_slots[2]["current"] == 0, "D6 两次施法扣了两个 2 环位",
   w14.spell_slots[2])

# 专注检定
w15 = wizard()
w15.concentrating_on = "Bless"
chk = concentration_check(w15, 8, Random(3))
ok(chk.check.kind == "save", "D5 专注检定是豁免")
ok(chk.check.dc == 10, "D6 ⭐ 受 8 点伤害 → DC max(10, 4) = 10", chk.check.dc)
chk2 = concentration_check(w15, 30, Random(3))
ok(chk2.check.dc == 15, "D7 ⭐ 受 30 点伤害 → DC 15（伤害一半）", chk2.check.dc)

# 打断
was = break_concentration(w15, reason="被打中")
ok(was == "Bless" and w15.concentrating_on == "", "D8 打断专注返回被中断的法术名",
   was)


# --------------------------------------------------------------------------- #
section("E. 成分与封口")

w16 = wizard()
w16.add_condition("stunned")     # 失能 + cant_speak
t5 = dummy()
w16.spell_slots[1] = {"current": 1, "max": 1}
r16 = cast_spell(w16, mm, [t5], Random(1), cast_ability="intelligence")
ok(not r16.ok, "E1 失能状态不能施法", r16.blocked_reason)

w17 = wizard()
r17 = cast_spell(w17, mm, [t5], Random(1), cast_ability="intelligence",
                 has_verbal=False)
ok(not r17.ok and "言语成分" in r17.blocked_reason, "E2 ⭐ 缺言语成分 → 拒绝",
   r17.blocked_reason)

w18 = wizard()
r18 = cast_spell(w18, mm, [t5], Random(1), cast_ability="intelligence",
                 has_somatic=False)
ok(not r18.ok and "姿势成分" in r18.blocked_reason, "E3 缺姿势成分 → 拒绝",
   r18.blocked_reason)


# --------------------------------------------------------------------------- #
section("F. 施法者数值")

w19 = wizard()
ok(spell_save_dc(w19, "intelligence") == 8 + 2 + 3, "F1 法术豁免 DC = 8+熟练+属性",
   spell_save_dc(w19, "intelligence"))
ok(spell_attack_bonus(w19, "intelligence") == 2 + 3, "F2 法术攻击加值 = 熟练+属性",
   spell_attack_bonus(w19, "intelligence"))
ok(spell_save_dc(w19, "charisma") == 8 + 2 + 0, "F3 换魅力施法时 DC 也换",
   spell_save_dc(w19, "charisma"))


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")
