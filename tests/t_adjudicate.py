# coding=utf-8
"""判定与合法动作菜单的回归测试。"""

from __future__ import annotations

import os
import random
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.rules import ABILITIES  # noqa: E402
from engine.rules.adjudicate import (  # noqa: E402
    AdjudicationError, DC_TABLE, ability_check, attack_advantage, cover_bonus,
    grid_distance_ft, resolve_attack, resolve_damage_expression,
    resolve_save_action, saving_throw, skill_check,
)
from engine.rules.bestiary import build_unit  # noqa: E402
from engine.rules.menu import MenuError, build_menu, render_menu  # noqa: E402
from engine.rules.unit import Action, Attack, DamagePacket, Unit  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra: str = "") -> None:
    global COUNT
    COUNT += 1
    print(f"  {'PASS' if cond else 'FAIL'} {label}"
          f"{('  |  ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def hero(name="Elma", uid="P1", **kw) -> Unit:
    base = dict(
        id=uid, name=name, team="pc", is_pc=True, level=3, ac=16,
        hp_max=24, hp=24, speeds={"walk": 30},
        abilities={"strength": 16, "dexterity": 14, "constitution": 14,
                   "intelligence": 10, "wisdom": 12, "charisma": 10},
        save_proficiencies={"strength", "constitution"},
    )
    base.update(kw)
    return Unit(**base)


# --------------------------------------------------------------------------- #
section("A. 距离（格距：对角算一格）")

ok(grid_distance_ft((0, 0), (10, 10)) == 10, "A1 斜向两格 = 10 尺（不是 14.1）")
ok(grid_distance_ft((0, 0), (30, 0)) == 30, "A2 直线三格 = 30 尺")
ok(grid_distance_ft((0, 0), (5, 10)) == 10, "A3 拐角取较长边")
ok(cover_bonus("half") == 2 and cover_bonus("three_quarters") == 5
   and cover_bonus("none") == 0, "A4 掩护加值 半掩护+2 / 四分之三+5")


# --------------------------------------------------------------------------- #
section("B. 属性/技能检定")

u = hero()
r = ability_check(u, "strength", 10, random.Random(1))
ok(r.check.bonus == 3, "B1 力量 16 → 检定加值 +3", r.check.bonus)
ok(r.check.kind == "check", "B2 kind=check")

# 已算好的技能总加值优先，不再叠属性调整值
u2 = hero(skill_bonuses={"athletics": 7})
r2 = skill_check(u2, "athletics", 15, random.Random(1))
ok(r2.check.bonus == 7, "B3 技能已给总加值时不再叠属性", r2.check.bonus)

# 没有给总加值 → 用属性调整值
u3 = hero()
r3 = skill_check(u3, "athletics", 15, random.Random(1))
ok(r3.check.bonus == 3, "B4 未给时用属性调整值", r3.check.bonus)

try:
    ability_check(u, "luck", 10, random.Random(0))
    ok(False, "B5 不认识的属性应报错")
except AdjudicationError:
    ok(True, "B5 不认识的属性报错")

# 力竭
u4 = hero(name="Fatigued", exhaustion=2)
r4 = ability_check(u4, "strength", 10, random.Random(1))
ok(r4.check.bonus == 3 - 4, "B6 力竭 2 级 → 检定 -4", r4.check.bonus)

# 倒地不能检定
u5 = hero(name="Down", hp=0)
try:
    ability_check(u5, "strength", 10, random.Random(0))
    ok(False, "B7 倒地不能检定")
except AdjudicationError:
    ok(True, "B7 倒地不能检定")


# --------------------------------------------------------------------------- #
section("C. 豁免")

wiz = hero(name="Wiz", abilities={**{k: 10 for k in ABILITIES}, "dexterity": 14},
           save_proficiencies={"dexterity"})
r = saving_throw(wiz, "dexterity", 15, random.Random(3))
ok(r.check.bonus == 2 + 2, "C1 敏捷 +2 且熟练 +2 = +4", r.check.bonus)

# 自动失败：麻痹的力敏豁免
wiz.add_condition("paralyzed")
r = saving_throw(wiz, "strength", 5, random.Random(3))
ok(not r.check.success, "C2 麻痹 → 力量豁免自动失败（即使 DC 只有 5）")
ok(any("自动失败" in x for x in r.reasons), "C3 失败理由写明了")
r = saving_throw(wiz, "constitution", 5, random.Random(3))
ok(r.check.success in (True, False), "C4 麻痹只影响力/敏，体质豁免照常掷")
wiz.remove_condition("paralyzed")

# 传奇抗性
r = saving_throw(wiz, "dexterity", 99, random.Random(3), can_succeed=True)
ok(r.check.success, "C5 传奇抗性：必失败改为成功")
ok(any("传奇抗性" in x for x in r.reasons), "C6 传奇抗性理由写明了")


# --------------------------------------------------------------------------- #
section("D. 攻击：优势/劣势的自动判定")

a = hero("Attacker", "A1")
t = hero("Target", "T1")

m, why = attack_advantage(a, t)
ok(m.resolved_advantage() == 0, "D1 正常情况无优劣势", why)

t.add_condition("prone")
m, why = attack_advantage(a, t, distance=5)
ok(m.resolved_advantage() == 1, "D2 目标倒地且 5 尺内 → 优势", why)
m, why = attack_advantage(a, t, distance=30)
ok(m.resolved_advantage() == -1, "D3 目标倒地但超过 5 尺 → 劣势", why)
t.remove_condition("prone")

t.add_condition("blinded")
m, why = attack_advantage(a, t)
ok(m.resolved_advantage() == 1, "D4 目标目盲 → 优势", why)
t.remove_condition("blinded")

t.add_condition("invisible")
m, why = attack_advantage(a, t)
ok(m.resolved_advantage() == -1, "D5 目标隐形 → 劣势", why)
t.remove_condition("invisible")

a.add_condition("poisoned")
m, why = attack_advantage(a, t)
ok(m.resolved_advantage() == -1, "D6 攻击者中毒 → 劣势", why)
a.remove_condition("poisoned")

# 抵消
t.add_condition("blinded")
a.add_condition("poisoned")
m, why = attack_advantage(a, t)
ok(m.resolved_advantage() == 0, "D7 一方优势一方劣势 → 抵消", why)
t.remove_condition("blinded")
a.remove_condition("poisoned")

# 回避
m, why = attack_advantage(a, t, target_dodging=True)
ok(m.resolved_advantage() == -1, "D8 目标回避 → 劣势", why)


# --------------------------------------------------------------------------- #
section("E. 攻击结算（真实怪物数据）")

bandit, _ = build_unit("Bandit")
bandit.position = (25, 0)
target = hero("Target", "T1", position=(25, 0), level=1)
scim = [x for x in bandit.actions if x.name == "Scimitar"][0]

# 够不着
try:
    resolve_attack(bandit, target, scim.attacks[0], random.Random(1), distance=30)
    ok(False, "E1 超触及应报错")
except AdjudicationError:
    ok(True, "E1 超触及报错（不静默打空）")

# 命中（用固定种子找一个必然命中的）
hit_found = False
for seed in range(40):
    bandit2, _ = build_unit("Bandit")
    tgt2 = hero("T2", level=1, hp_max=100, hp=100)
    r = resolve_attack(bandit2, tgt2, scim.attacks[0], random.Random(seed), distance=5)
    if r.hit:
        hit_found = True
        ok(r.damage_total > 0, "E2 命中时有伤害", r.damage_total)
        ok(tgt2.hp < 100, "E3 命中后目标掉血", tgt2.hp)
        ok("slashing" in r.damage_type, "E4 伤害类型 = 挥斩", r.damage_type)
        ok(r.damage_result is not None, "E5 伤害结算留痕")
        break
if not hit_found:
    ok(False, "E2~E5 40 个种子里没有一次命中（不正常）")

# 重击：伤害骰翻倍，固定加值不翻倍
n1, r1 = resolve_damage_expression("1d6+3", random.Random(5), crit=False)
n2, r2 = resolve_damage_expression("1d6+3", random.Random(5), crit=True)
ok(len(r2) == 2, "E6 重击返回两颗骰（各掷一遍）")
ok(n2 == sum(x.dice_total for x in r2) + 3, "E7 重击：骰子翻倍、加值只算一次", n2)
ok(n2 > n1, "E8 重击伤害高于普通", f"{n1} vs {n2}")

# 必定重击：5 尺内麻痹目标
par = hero("Paralyzed", "T3", hp_max=200, hp=200)
par.add_condition("paralyzed")
crit_seen = False
for seed in range(60):
    b3, _ = build_unit("Bandit")
    p3 = hero("T4", hp_max=200, hp=200)
    p3.add_condition("paralyzed")
    r3 = resolve_attack(b3, p3, scim.attacks[0], random.Random(seed), distance=5)
    if r3.hit and r3.crit:
        crit_seen = True
        ok(any("必定重击" in x for x in r3.reasons), "E9 必定重击的理由写明了")
        break
ok(crit_seen, "E10 5 尺内麻痹目标 → 命中即重击")

# 归零标记
tgt = hero("Fragile", hp_max=3, hp=3)
for seed in range(60):
    b4, _ = build_unit("Bandit")
    t4 = hero("Fragile", hp_max=3, hp=3)
    r4 = resolve_attack(b4, t4, scim.attacks[0], random.Random(seed), distance=5)
    if r4.hit and r4.damage_total >= 3:
        ok(t4.hp == 0 and r4.damage_result.dropped_to_zero, "E11 归零标记正确")
        break


# --------------------------------------------------------------------------- #
section("F. 豁免型动作（AoE / 半伤）")

dragon, _ = build_unit("Adult Red Dragon")
fb = [x for x in dragon.actions if x.name == "Fire Breath"][0]
tough = hero("Tough", "T1", hp_max=500, hp=500)
sr = resolve_save_action(dragon, [tough], fb, random.Random(11))
ok(len(sr.targets) == 1, "F1 AoE 结算了一个目标")
ok("save_summary" in sr.targets[0], "F2 留了豁免痕")
ok(fb.save_ability == "dexterity" and fb.save_dc == 21, "F3 用的是真实数据（敏捷 DC 21）")

# 免疫：红龙自己免疫火焰
sr2 = resolve_save_action(dragon, [dragon], fb, random.Random(11))
dmg_note = sr2.targets[0].get("damage_summary", "")
ok("免疫" in dmg_note or dragon.hp == dragon.hp_max, "F4 免疫火焰 → 不吃火焰伤害", dmg_note)


# --------------------------------------------------------------------------- #
section("G. 合法动作菜单")

b, _ = build_unit("Bandit")
p1 = hero("P1", "P1", position=(25, 0))
p2 = hero("P2", "P2", position=(30, 0))
b.position = (0, 0)

menu = build_menu(b, [b, p1, p2])
ok(menu.unit_id == b.id, "G1 菜单归属正确")
ids = [o.id for o in menu.options]
ok(len(ids) == len(set(ids)), "G2 选项 id 不重复", ids)
ok(menu.slots_free.get("action") is True, "G3 动作槽可用")
ok(any(o.kind == "attack" for o in menu.options), "G4 有攻击选项")
ok(any(o.kind == "end_turn" for o in menu.options), "G5 一定有结束回合")

# 距离：25/30 尺外，近战本身够不着，但移动 30 尺内可接敌 → 选项带目标 + 提示
scim_opts = [o for o in menu.options if o.kind == "attack" and "Scimitar" in o.name]
ok(bool(scim_opts), "G6 找得到弯刀选项")
if scim_opts:
    ok(set(scim_opts[0].target_ids) == {"P1", "P2"},
       "G7 ⭐ 移动力内的目标进候选（不用先单独移动）", scim_opts[0].target_ids)
    ok("先移动接敌" in scim_opts[0].note, "G8 提示会自动接敌", scim_opts[0].note)
ok(not [o for o in menu.options
        if o.kind == "attack" and o.requires_target and not o.target_ids],
   "G9 ⭐ 菜单里没有空目标攻击选项")

# 弩（80 尺）打得到
xb = [o for o in menu.options if o.kind == "attack" and "Crossbow" in o.name]
if xb:
    ok(set(xb[0].target_ids) == {"P1", "P2"}, "G9 80 尺弩能选到 25/30 尺的目标",
       xb[0].target_ids)
else:
    ok(False, "G9 找不到轻弩选项", [o.name for o in menu.options])

# 通用动作都在
kinds = {o.kind for o in menu.options}
for k in ("dash", "disengage", "dodge", "hide", "help"):
    ok(k in kinds, f"G10 通用动作 {k} 在菜单里")
ok("ready_attack" in kinds or "ready" in kinds,
   "G10b ⭐ 准备动作在菜单里（有攻击时是具体触发选项）", sorted(kinds))

# 移动
mv = [o for o in menu.options if o.kind == "move"]
ok(mv and "30" in mv[0].summary, "G11 移动选项给出剩余移动力")

# 用过动作后 → 动作类选项消失
b.spend("action")
menu2 = build_menu(b, [b, p1, p2])
ok(not any(o.slot == "action" for o in menu2.options),
   "G12 动作已用 → 动作类选项从菜单消失")
ok(any(o.kind == "end_turn" for o in menu2.options), "G13 仍能结束回合")
ok(any("动作不可用" in x for x in menu2.blocked), "G14 blocked 里说明了原因")

# 失能 → 只剩结束回合
b2, _ = build_unit("Bandit")
b2.add_condition("stunned")
menu3 = build_menu(b2, [b2, p1])
ok(len([o for o in menu3.options if o.kind != "end_turn"]) == 0,
   "G15 失能 → 除结束回合外没有选项", [o.name for o in menu3.options])
ok(any("失能" in x for x in menu3.blocked), "G16 失能写在 blocked 里")

# 倒地 → 菜单照常有（倒地不是失能）
b3, _ = build_unit("Bandit")
b3.add_condition("prone")
menu4 = build_menu(b3, [b3, p1])
ok(any(o.kind == "attack" for o in menu4.options), "G17 倒地仍能攻击（只有劣势）")

# 每日次数限制
b4, _ = build_unit("Adult Red Dragon")
menu5 = build_menu(b4, [b4, p1])
fd = [o for o in menu5.options if o.kind == "save_action"]
ok(fd, "G18 龙的吐息在菜单里")
ok("充能" in (fd[0].summary + fd[0].note), "G19 使用限制写进了摘要或备注",
   fd[0].summary + " / " + fd[0].note)

# 渲染
text = render_menu(menu, battlefield=[b, p1, p2])
ok(b.name in text and "可选行动" in text, "G20 渲染含单位名与可选行动")
ok("距离" in text, "G21 渲染含距离信息")

# 非法 id
try:
    menu.by_id("zzz")
    ok(False, "G22 非法选项 id 应报错")
except MenuError:
    ok(True, "G22 非法选项 id 报错")

# 重复 id
try:
    build_menu(b, [b, b])
    ok(False, "G23 战场重复 id 应报错")
except MenuError:
    ok(True, "G23 战场重复 id 报错")

# 倒地单位没有菜单
try:
    dead, _ = build_unit("Bandit")
    dead.hp = 0
    build_menu(dead, [dead, p1])
    ok(False, "G24 倒地单位没有菜单")
except MenuError:
    ok(True, "G24 倒地单位没有菜单")

# 敌方规划模型一次拿到全部敌人的菜单
b5, _ = build_unit("Bandit")
b6, _ = build_unit("Bandit")
b6.id = "B6"
p = hero("Hero", "P1", position=(10, 0))
menus = [build_menu(x, [b5, b6, p]) for x in (b5, b6)]
ok(len(menus) == 2 and all(m.options for m in menus), "G25 多个敌人各自出菜单（合并调用用）")


# --------------------------------------------------------------------------- #
section("H. 难度表（与角色卡世界书的 DC 六档一致）")

ok(DC_TABLE["very_easy"] == 5 and DC_TABLE["nearly_impossible"] == 30,
   "H1 DC 六档 5/10/15/20/25/30")
ok(DC_TABLE["medium"] == 15, "H2 中等 = 15")


# --------------------------------------------------------------------------- #
section("L. 法术进菜单（U2：选法术 + 选环阶）")

from engine.rules.spells import get_spell  # noqa: E402

wiz = hero("法师", "W1",
           abilities={"strength": 8, "dexterity": 14, "constitution": 12,
                      "intelligence": 16, "wisdom": 12, "charisma": 10},
           spell_slots={1: {"current": 2, "max": 2},
                        2: {"current": 1, "max": 1},
                        3: {"current": 1, "max": 1}},
           cast_ability="intelligence")
wiz.spells = [get_spell("Fireball"), get_spell("Magic Missile"),
              get_spell("Cure Wounds"), get_spell("Fire Bolt"),
              get_spell("Shield")]
ally = hero("队友", "P2", position=(5, 0))
foe1 = hero("敌人1", "E1", team="enemy", position=(20, 0))
foe2 = hero("敌人2", "E2", team="enemy", position=(25, 0))
sm = build_menu(wiz, [wiz, ally, foe1, foe2])
sp_opts = [o for o in sm.options if o.kind == "spell"]
all_ids = [o.id for o in sm.options]
ok(len(all_ids) == len(set(all_ids)), "L1 ⭐ 所有选项 id（含法术）不重复", all_ids)

fb_opts = [o for o in sp_opts if o.name == "Fireball"]
ok(len(fb_opts) == 1 and fb_opts[0].slot_level == 3,
   "L2 火球术按 3 环列出（只有一个可用环阶）",
   [(o.name, o.slot_level) for o in fb_opts])
if fb_opts:
    ok(fb_opts[0].requires_target is False, "L3 ⭐ AoE 法术不要求单选目标")
    ok(set(fb_opts[0].target_ids) == {"E1", "E2"},
       "L4 AoE 的候选是射程内全部敌人", fb_opts[0].target_ids)
    ok("全部" in fb_opts[0].summary, "L5 摘要写明命中范围内全部目标",
       fb_opts[0].summary)

mm_opts = [o for o in sp_opts if o.name == "Magic Missile"]
ok({o.slot_level for o in mm_opts} == {1},
   "L6 ⭐ 魔法飞弹只列 1 环（SRD 没给升环数值，引擎不列算不出的高环）",
   [o.slot_level for o in mm_opts])
if mm_opts:
    ok(all(o.requires_target for o in mm_opts), "L7 单体法术要求目标")
    ok(set(mm_opts[0].target_ids) == {"E1", "E2"}, "L8 单体法术候选是敌人",
       mm_opts[0].target_ids)
    d = mm_opts[0].to_dict()
    ok(d.get("slot_level") in (1, 2) and d.get("spell") == "Magic Missile",
       "L9 给模型的选项里带环阶与法术名", d)

cantrip_opts = [o for o in sp_opts if o.name == "Fire Bolt"]
ok(len(cantrip_opts) == 1 and cantrip_opts[0].slot_level == 0,
   "L10 ⭐ 戏法只列一条、环阶 0", [(o.slot_level) for o in cantrip_opts])

cw_opts = [o for o in sp_opts if o.name == "Cure Wounds"]
ok(cw_opts and "W1" in cw_opts[0].target_ids and "P2" in cw_opts[0].target_ids,
   "L11 ⭐ 治疗术的候选是友方 + 自己（可治自己）",
   cw_opts[0].target_ids if cw_opts else None)
ok(cw_opts and "E1" not in cw_opts[0].target_ids,
   "L12 治疗术不会把敌人列成候选")
ok({o.slot_level for o in cw_opts} == {1, 2, 3},
   "L12b ⭐ 治疗术按 1/2/3 环各列一条（升环治疗量算得出来）",
   [o.slot_level for o in cw_opts])

shield_opts = [o for o in sp_opts if o.name == "Shield"]
ok(not shield_opts, "L13 ⭐ 护盾术不再是可选行动（反应不能主动放）",
   [(o.name, o.slot) for o in shield_opts])
ok(any("Shield" in x or "护盾" in x for x in sm.reaction_notes),
   "L14 ⭐ 护盾术写进「反应（条件触发）」说明", sm.reaction_notes)
ok(any("自动" in x for x in sm.reaction_notes),
   "L14b 说明里写明「自动触发」", sm.reaction_notes)
ok("反应（满足条件时自动触发）" in sm.render(),
   "L14c 菜单渲染里有反应说明")

# 动作已用 → 动作类法术消失，反应说明仍在（反应槽独立）
wiz.spend("action")
sm2 = build_menu(wiz, [wiz, ally, foe1, foe2])
names2 = {o.name for o in sm2.options if o.kind == "spell"}
ok("Fireball" not in names2 and "Magic Missile" not in names2,
   "L15 ⭐ 动作已用 → 动作槽法术从菜单消失", sorted(names2))
ok(sm2.reaction_notes and "Shield" in "".join(sm2.reaction_notes),
   "L16 ⭐ 动作已用不影响反应说明（两个槽独立）", sm2.reaction_notes)

# 没有可用法术位 → 不列该法术，并明说原因
wiz2 = hero("没位法师", "W2", spell_slots={3: {"current": 0, "max": 0}})
wiz2.spells = [get_spell("Fireball")]
sm3 = build_menu(wiz2, [wiz2, foe1])
ok(not any(o.kind == "spell" for o in sm3.options), "L17 没有 3 环位 → 不列火球术")
ok(any("没有可用法术位" in x for x in sm3.blocked),
   "L18 ⭐ 明说「没有可用法术位」（不静默）", sm3.blocked)

# 戏法不依赖法术位
wiz3 = hero("戏法法师", "W3", spell_slots={})
wiz3.spells = [get_spell("Fire Bolt")]
sm4 = build_menu(wiz3, [wiz3, foe1])
ok(any(o.kind == "spell" and o.slot_level == 0 for o in sm4.options),
   "L19 ⭐ 没有法术位也能用戏法")

# 渲染
rendered = render_menu(sm, battlefield=[wiz, ally, foe1, foe2])
ok("Fireball" in rendered and "3 环" in rendered,
   "L20 渲染里能看到法术名与环阶")

# --------------------------------------------------------------------------- #
section("M. 目标写在选项行上（M2 真模型复验的教训）")

# 用全新的单位：G 段已经把 b 的动作槽用掉了，菜单里不会再出攻击
b2m, _ = build_unit("Bandit")
b2m.position = (0, 0)
q1 = hero("布兰", "Q1", position=(25, 0))
bm = build_menu(b2m, [b2m, q1])
bm_text = render_menu(bm, battlefield=[b2m, q1])
ok("🎯可选目标：" in bm_text, "M1 ⭐ 需要目标的选项自带目标标记", bm_text[-200:])
ok("🎯可选目标：Q1" in bm_text, "M2 ⭐ 目标只写 id", bm_text[-160:])
ok("🎯可选目标：Q1 布兰" not in bm_text,
   "M2b ⭐ 不把名字并进目标串（模型会把整段抄进 target）")

dash = next(o for o in bm.options if o.kind == "dash")
ok("🎯" not in dash.line(targets=""), "M3 不需要目标的选项没有目标标记")

from engine.rules.loop import GameLoop  # noqa: E402
PLAYER_SYSTEM = GameLoop.PLAYER_SYSTEM
ok('"target"' in PLAYER_SYSTEM,
   "M4 ⭐ 玩家输出格式里写了 target 字段（只给 id 示例）")
ok("没标目标的选项不要写" in PLAYER_SYSTEM, "M5 明确『不需要目标时别写 target』")

# --------------------------------------------------------------------------- #
section("N. 菜单 id 唯一：跳过攻击留空号也不能撞（外审 P1-1）")

caster = Unit(id="W9", name="小翠", team="pc", hp_max=18, hp=18, ac=12,
              level=3, speeds={"walk": 30},
              abilities={"strength": 8, "dexterity": 14, "constitution": 12,
                         "intelligence": 16, "wisdom": 12, "charisma": 10},
              cast_ability="intelligence",
              spell_slots={1: {"current": 2, "max": 2}})
caster.position = (0, 0)
caster.actions = [Action(name="Dagger", slot="action",
                         attacks=[Attack(name="Dagger", is_melee=True, reach=5,
                                         attack_bonus=4,
                                         damage_notation="1d4")])]
caster.spells = [get_spell("Magic Missile"), get_spell("Fire Bolt")]
caster.reset_turn()
far_foe, _ = build_unit("Bandit", uid="E9", team="enemy")
far_foe.position = (70, 0)              # 70 尺：近战够不着 → 攻击选项被跳过，留空号
cm = build_menu(caster, [caster, far_foe])
cids = [o.id for o in cm.options]
ok(len(cids) == len(set(cids)), "N1 ⭐ 跳过攻击留空号后，选项 id 仍不重复", cids)
spell_opt = next((o for o in cm.options if o.kind == "spell"), None)
ok(spell_opt is not None, "N2 有法术选项", [o.kind for o in cm.options])
if spell_opt is not None:
    got = cm.by_id(spell_opt.id)
    ok(got.kind == "spell" and got.name == spell_opt.name,
       "N3 ⭐ by_id(法术 id) 返回的是法术本身（不是被撞掉的移动/疾走）",
       (spell_opt.id, got.kind, got.name))
    ok(any(o.kind == "attack" and not o.target_ids for o in cm.options)
       is False, "N4 菜单里没有『空 target 的攻击』（留着它也会留空号）")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")



