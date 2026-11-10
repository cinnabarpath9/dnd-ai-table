# coding=utf-8
"""2026-11-04 代码审查修复的回归（A 组：骰子 / 武器 / 属性双语）。

每一条都对应审查报告里的一个 P0/P1；**修复前这些断言会失败**：

    A. 先攻加值要进总值（原来只掷裸 d20）
    B. 武器属性读 `property.name`（原来读不存在的 `property.key`）
    C. 属性/技能检定走 `Unit.mod()`（原来中文键角色按 10 算）
    D. 有预置豁免的单位也要吃力竭
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random, roll_d20  # noqa: E402
from engine.models import FakeModel, Intent  # noqa: E402
from engine.rules.adjudicate import (  # noqa: E402
    AdjudicationError, ability_check, attack_advantage, cast_spell,
    damage_concentration_line, grid_distance_ft, resolve_attack, saving_throw,
    skill_check,
)
from engine.rules.loop import RoundLog  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.spells import get_spell  # noqa: E402
from engine.rules.turn import execute_intent  # noqa: E402
from engine.rules.unit import Action, Attack, DamagePacket  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.unit import Unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

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


AB = {"力量": 16, "敏捷": 14, "体质": 14, "智力": 10, "感知": 12, "魅力": 10}


def make_state(names=("甲", "乙"), ab=None) -> StateTree:
    return StateTree.from_template(
        {n: new_character(n, level=3, job="战士", abilities=ab or AB,
                          hp_max=24, ac=15) for n in names},
        current=names[0])


def equip(st: StateTree, name: str, weapon: str) -> None:
    st.commit([{"op": "insert", "path": f"/角色列表/{name}/物品/{weapon}",
                "value": {"类别": "武器", "已装备": True}}],
              actor="填表", reason="装备")


# --------------------------------------------------------------------------- #
section("A. 先攻：加值必须进总值")

# 两个不同敏捷的角色，多个种子对比「实际先攻序」与「裸骰 + 加值」的期望序
for seed in range(12):
    st = make_state(("甲", "乙"),
                    ab={"力量": 16, "敏捷": 20, "体质": 14,
                        "智力": 10, "感知": 12, "魅力": 10})
    loop = GameLoop(st, Ledger(), FakeModel(), rng=Random(seed))
    units = [make_hero_unit(st, n) for n in ("甲", "乙")]
    units[0].abilities["敏捷"] = 20
    units[1].abilities["敏捷"] = 10
    loop.set_field(units)
    loop.start_combat(enemies=[("Bandit", 0)])
    actual = [u.id for u, _i in loop.initiative]

    rng = Random(seed)
    expect = []
    for u in units:
        r = roll_d20(rng, u.initiative_bonus)
        expect.append((u, r.total + u.initiative_bonus))
    expect.sort(key=lambda t: (-t[1], -t[0].mod("dexterity"), t[0].id))
    ok(actual == [u.id for u, _i in expect],
       f"A1 种子 {seed}：先攻序 = 裸骰 + 敏捷加值", (actual, [u.id for u, _ in expect]))

st = make_state()
loop = GameLoop(st, Ledger(), FakeModel(), rng=Random(3))
u = make_hero_unit(st, "甲")
loop.set_field([u])
loop.start_combat(enemies=[("Bandit", 0)])
ok(u.initiative_bonus == 2, "A2 敏捷 14 的先攻加值是 +2", u.initiative_bonus)


# --------------------------------------------------------------------------- #
section("B. 武器属性：按 property.name 读（不是 property.key）")

def weapon_attack(st: StateTree, name: str, weapon: str):
    equip(st, name, weapon)
    u = make_hero_unit(st, name)
    ok(len(u.actions) == 1, f"B0 {weapon} 生成了一个攻击动作", [a.name for a in u.actions])
    return u, u.actions[0].attacks[0]


# 敏捷 16 / 力量 8：短弓必须用敏捷、有 80/320；不是 5 尺近战
st = make_state(("射手",), ab={"力量": 8, "敏捷": 16, "体质": 12,
                               "智力": 10, "感知": 12, "魅力": 10})
u, a = weapon_attack(st, "射手", "短弓")
ok(a.is_melee is False, "B1 短弓是远程武器", a.is_melee)
ok(a.range_normal == 80 and a.range_long == 320,
   "B2 短弓射程 80/320（从 SRD 记录读）", (a.range_normal, a.range_long))
ok(a.attack_bonus == 3 + 2, "B3 短弓用敏捷：+3 敏 + 2 熟练 = +5", a.attack_bonus)
ok("ammunition" in a.properties, "B4 属性名是小写 ammunition", a.properties)

st = make_state(("剑士",), ab={"力量": 16, "敏捷": 8, "体质": 14,
                               "智力": 10, "感知": 12, "魅力": 10})
u, a = weapon_attack(st, "剑士", "长剑")
ok(a.is_melee is True and a.reach == 5 and a.range_normal is None,
   "B5 长剑仍是 5 尺近战", (a.is_melee, a.reach, a.range_normal))
ok(a.attack_bonus == 3 + 2, "B6 长剑用力量 +5", a.attack_bonus)

st = make_state(("长柄",), ab={"力量": 16, "敏捷": 8, "体质": 14,
                               "智力": 10, "感知": 12, "魅力": 10})
u, a = weapon_attack(st, "长柄", "长柄刀")
ok(a.reach == 10, "B7 长柄刀触及 10 尺（Reach 属性）", a.reach)

st = make_state(("匕首",), ab={"力量": 8, "敏捷": 16, "体质": 12,
                               "智力": 10, "感知": 12, "魅力": 10})
u, a = weapon_attack(st, "匕首", "匕首")
ok(a.attack_bonus == 3 + 2, "B8 匕首有 Finesse，用敏捷 +5", a.attack_bonus)
ok(a.is_melee is True and a.reach == 5, "B9 匕首仍是近战（投掷第一版按近战）")


# --------------------------------------------------------------------------- #
section("C. 属性/技能检定：中文属性键也要算加值")

st = make_state(("中文",), ab={"力量": 16, "敏捷": 12, "体质": 12,
                               "智力": 10, "感知": 14, "魅力": 8})
u_cn = make_hero_unit(st, "中文")
u_en = Unit(id="EN", name="EN", hp=10, hp_max=10,
            abilities={"strength": 16, "dexterity": 12, "constitution": 12,
                       "intelligence": 10, "wisdom": 14, "charisma": 8})

r_cn = skill_check(u_cn, "athletics", 10, Random(5))
r_en = skill_check(u_en, "athletics", 10, Random(5))
ok(r_cn.check.bonus == 3, "C1 中文键力量 16 → 运动检定 +3", r_cn.check.bonus)
ok(r_cn.check.bonus == r_en.check.bonus,
   "C2 中英两套键给出同一个加值", (r_cn.check.bonus, r_en.check.bonus))
ok(r_cn.check.bonus_breakdown.get("力量") == 3,
   "C3 留痕里力量也是 +3（不是 0）", r_cn.check.bonus_breakdown)

r_cn2 = ability_check(u_cn, "strength", 10, Random(6))
ok(r_cn2.check.bonus == 3, "C4 属性检定同样走双语键", r_cn2.check.bonus)

r_sv = saving_throw(u_cn, "wisdom", 12, Random(7))
ok(r_sv.check.bonus == 2, "C5 感知 14 的豁免 +2", r_sv.check.bonus)
ok(r_sv.check.bonus_breakdown.get("感知") == 2,
   "C6 豁免留痕也是 +2", r_sv.check.bonus_breakdown)


# --------------------------------------------------------------------------- #
section("D. 力竭：有预置豁免的单位也要扣")

u = Unit(id="M", name="怪物", hp=10, hp_max=10,
         abilities={"dexterity": 10},
         save_bonuses={"dexterity": 5}, exhaustion=3)
ok(u.save_total("dexterity") == -1,
   "D1 预置 +5、力竭 3 级 → -1（原来提前 return 得 +5）",
   u.save_total("dexterity"))

r = saving_throw(u, "dexterity", 10, Random(9))
ok(r.check.bonus == -1 and r.check.bonus_breakdown.get("力竭") == -6,
   "D2 豁免总值与留痕一致", (r.check.bonus, r.check.bonus_breakdown))


# --------------------------------------------------------------------------- #
section("E. 多投射物：魔法飞弹 / 灼热射线要按发数结算")

mm = get_spell("Magic Missile")
sr = get_spell("Scorching Ray")
bh = get_spell("Burning Hands")
fb = get_spell("Fireball")
ok(mm.projectiles == 3 and mm.upcast_projectiles == 1,
   "E1 魔法飞弹 3 发，升环 +1 发", (mm.projectiles, mm.upcast_projectiles))
ok(mm.projectiles_at(3) == 5, "E2 3 环魔法飞弹 5 发", mm.projectiles_at(3))
ok(sr.projectiles == 3, "E3 灼热射线 3 道", sr.projectiles)


def wizard(spells=("Magic Missile",), *, slots=None):
    st = StateTree.from_template(
        {"法师": new_character("法师", level=3, job="法师",
                               abilities={"力量": 8, "敏捷": 14, "体质": 12,
                                          "智力": 16, "感知": 12, "魅力": 10},
                               hp_max=18, ac=12)})
    patches = []
    for lv, (cur, mx) in (slots or {1: (4, 4), 2: (2, 2), 3: (2, 2)}).items():
        patches.append({"op": "insert", "path": f"/角色列表/法师/施法/法术位/{lv}",
                        "value": {"当前": cur, "最大": mx}})
    for s in spells:
        patches.append({"op": "insert", "path": f"/角色列表/法师/施法/法术书/{s}",
                        "value": {"等级": 1, "准备中": True}})
    st.commit(patches, actor="填表", reason="法术准备")
    return st, make_hero_unit(st, "法师")


st, w = wizard(("Magic Missile",))
target = Unit(id="T", name="靶子", hp=100, hp_max=100)
before = w.spell_slots[1]["current"]
r = cast_spell(w, mm, [target], Random(11), slot_level=1,
               cast_ability="intelligence")
dmg = 100 - target.hp
ok(r.ok and r.slot_consumed and r.slot_level == 1,
   "E4 施法成功并扣 1 环位", (r.ok, r.slot_level))
ok(w.spell_slots[1]["current"] == before - 1, "E5 法术位真的扣了")
ok(6 <= dmg <= 15, "E6 ⭐ 1 环魔法飞弹伤害 6~15（3×(1d4+1)）", dmg)
ok(len(r.target_rows) == 3, "E7 三发各留一行", len(r.target_rows))
ok(sum(row.get("damage", 0) for row in r.target_rows) == dmg,
   "E8 留痕伤害之和 = 实际掉血", (r.target_rows, dmg))

st, w3 = wizard(("Magic Missile",), slots={3: (2, 2)})
t3 = Unit(id="T3", name="靶子3", hp=100, hp_max=100)
r3 = cast_spell(w3, mm, [t3], Random(12), slot_level=3,
                cast_ability="intelligence")
d3 = 100 - t3.hp
ok(len(r3.target_rows) == 5 and 10 <= d3 <= 25,
   "E9 ⭐ 3 环魔法飞弹 5 发、伤害 10~25", (len(r3.target_rows), d3))

# 灼热射线：3 次法术攻击，命中才掉血；伤害必须写回 AttackResult
hit_total = 0
for seed in range(12):
    st, wsr = wizard(("Scorching Ray",))
    ts = Unit(id="S", name="靶子S", hp=100, hp_max=100)
    rs = cast_spell(wsr, sr, [ts], Random(seed), slot_level=2,
                    cast_ability="intelligence")
    if any(row.get("hit") for row in rs.target_rows):
        hit_total = 100 - ts.hp
        break
ok(hit_total > 0, "E10 灼热射线至少命中一次并造成伤害", hit_total)
ok(rs.attack is not None and rs.attack.damage_total > 0,
   "E11 ⭐ 法术攻击把伤害写回 AttackResult（原来恒为 0）",
   (rs.attack.damage_total if rs.attack else None))
ok(len(rs.target_rows) == 3, "E12 三道射线各留一行", len(rs.target_rows))


# --------------------------------------------------------------------------- #
section("F. 自身范围法术：不能打到全图")

st, wbh = wizard(("Burning Hands",), slots={1: (4, 4)})
wbh.position = (0.0, 0.0)
near = Unit(id="NEAR", name="近敌", hp=50, hp_max=50, position=(10.0, 0.0))
far = Unit(id="FAR", name="远敌", hp=50, hp_max=50, position=(200.0, 0.0))
menu = build_menu(wbh, [wbh, near, far])
opt = next(o for o in menu.options
           if o.kind == "spell" and o.name == "Burning Hands")
ok("NEAR" in opt.target_ids, "F1 15 尺内的敌人进候选", opt.target_ids)
ok("FAR" not in opt.target_ids,
   "F2 ⭐ 200 尺外的敌人不进候选（原来全图）", opt.target_ids)
ok(bh.is_aoe and bh.shape_size == 15, "F3 燃烧之手是 15 尺锥", bh.shape_size)

def dist_ft(a, b) -> float:
    return grid_distance_ft(a.position, b.position)


far_hp = far.hp
r = cast_spell(wbh, bh, [near, far], Random(4), slot_level=1,
               cast_ability="intelligence", distance_of=dist_ft)
ok(far.hp == far_hp, "F4 ⭐ 结算层也按距离过滤：远敌不掉血", (far.hp, far_hp))
ok(near.hp < near.hp_max, "F5 近敌正常结算", near.hp)


# --------------------------------------------------------------------------- #
section("G. 施法顺序：先校验，再扣资源；0 环阶不能用高环法术")

st, wf = wizard(("Fireball", "Hold Person"), slots={2: (2, 2), 3: (2, 2)})
slots_before = dict((lv, v["current"]) for lv, v in wf.spell_slots.items())
r = cast_spell(wf, fb, [], Random(1), slot_level=3,
               cast_ability="intelligence")
ok(not r.ok, "G1 火球空目标被拒绝", r.blocked_reason)
ok(wf.spell_slots[3]["current"] == slots_before[3],
   "G2 ⭐ 被拒绝时法术位没扣", wf.spell_slots[3]["current"])
ok("action" not in wf.used, "G3 ⭐ 被拒绝时动作没扣", wf.used)

r2 = cast_spell(wf, get_spell("Hold Person"), [], Random(2), slot_level=2,
                cast_ability="intelligence")
ok(not r2.ok and wf.concentrating_on == "",
   "G4 ⭐ 专注法术被拒绝时不占专注", wf.concentrating_on)

t = Unit(id="G", name="靶", hp=80, hp_max=80)
r3 = cast_spell(wf, fb, [t], Random(3), slot_level=0,
                cast_ability="intelligence")
ok(not r3.ok and not r3.slot_consumed,
   "G5 ⭐ 3 环火球不能用 0 环免费放", (r3.ok, r3.slot_consumed, r3.blocked_reason))
ok(wf.spell_slots[3]["current"] == slots_before[3],
   "G6 0 环失败后 3 环位没变", wf.spell_slots[3]["current"])

r4 = cast_spell(wf, get_spell("Fireball"), [t], Random(5), slot_level=2,
                cast_ability="intelligence")
ok(not r4.ok and "环" in r4.blocked_reason,
   "G7 低于最低环阶被拒绝", r4.blocked_reason)


# --------------------------------------------------------------------------- #
section("H. 敌人 id 全局唯一：第二场战斗不能撞号")

st = make_state(("甲",))
loop = GameLoop(st, Ledger(), FakeModel(), rng=Random(1))
pc = make_hero_unit(st, "甲")
loop.set_field([pc])
loop.start_combat(enemies=[("Bandit", 1)])
first_ids = [u.id for u in loop.field]
bandit = loop.find_unit("E1")
bandit.hp = 0                                  # 第一场打赢
loop.start_combat(enemies=[("Bandit", 1)])
ids = [u.id for u in loop.field]
ok(len(ids) == len(set(ids)), "H1 ⭐ 两次开战后 id 仍唯一", ids)
ok("E2" in ids, "H2 第二场从 E2 起号（不是重复 E1）", ids)
try:
    loop.run_combat_round()
    ok(True, "H3 第二次战斗能正常跑一轮（原来抛 MenuError）")
except Exception as exc:                       # noqa: BLE001
    ok(False, f"H3 第二次战斗能正常跑一轮：{exc}")


# --------------------------------------------------------------------------- #
section("I. 死亡豁免：被别人打倒也要掷")

def dying_setup(hp=0):
    st = make_state(("甲",))
    lp = GameLoop(st, Ledger(), FakeModel(), rng=Random(0))
    u = make_hero_unit(st, "甲")
    u.hp = hp
    lp.set_field([u])
    lp.initiative = [(u, 10)]          # 手工给先攻序：只有这个濒死 PC
    return lp, u

for seed in range(8):
    lp, u = dying_setup()
    lp.rng = Random(seed)
    lg = lp.run_combat_round()
    died = lp.death.get(u.id)
    if died and (died.successes or died.failures or died.dead or died.stable):
        break
ok(died is not None and (died.successes or died.failures or died.dead),
   "I1 ⭐ 0 HP 的单位在自己的回合掷了死亡豁免",
   (seed, died))
ok(any("💀" in x for x in lg.lines), "I2 死亡豁免写进了日志", lg.lines[-3:])


# --------------------------------------------------------------------------- #
section("J. 专注：受伤的是谁，就由谁检定")

con = Unit(id="C", name="专注者", hp=80, hp_max=80, ac=10)
con.concentrating_on = "Bless"
atk = Attack(name="剑", attack_bonus=10, damage_notation="1d8",
             damage_type="slashing", reach=5, is_melee=True)
attacker = Unit(id="A", name="攻击者", hp=80, hp_max=80, ac=15,
                abilities={"strength": 16})
failed = False
for seed in range(12):
    con.hp = 80
    con.concentrating_on = "Bless"
    r = resolve_attack(attacker, con, atk, Random(seed), distance=5)
    if r.hit and not con.concentrating_on:
        failed = True
        break
ok(failed, "J1 ⭐ 受伤的专注者自己掷专注，失败就断", (seed, con.concentrating_on))
ok(any("专注" in x for x in r.reasons) if failed else True,
   "J2 专注打断写进了攻击留痕", r.reasons)

attacker.concentrating_on = "Hunter's Mark"
con.hp = 80
con.concentrating_on = "Bless"
resolve_attack(attacker, con, atk, Random(3), distance=5)
ok(attacker.concentrating_on == "Hunter's Mark",
   "J3 ⭐ 攻击者自己的专注不会被自己打出的伤害打断",
   attacker.concentrating_on)

# 直接测 helper：失败时返回一行、并清空专注
con2 = Unit(id="C2", name="专注者2", hp=80, hp_max=80)
con2.concentrating_on = "Hold Person"
line = ""
for seed in range(12):
    con2.concentrating_on = "Hold Person"
    line = damage_concentration_line(con2, 30, Random(seed))
    if line:
        break
ok(line.startswith("⚡") and con2.concentrating_on == "",
   "J4 伤害 → 专注失败 → 打断", (line, con2.concentrating_on))


# --------------------------------------------------------------------------- #
section("K. HP 回写 stat_data（角色卡 / 战报不再显示旧血）")

st = make_state(("甲",))
st.commit([{"op": "replace", "path": "/角色列表/甲/生命值/当前", "value": 5},
           {"op": "replace", "path": "/角色列表/甲/生命值/临时", "value": 0}],
          actor="测试", reason="造旧血")
lp = GameLoop(st, Ledger(), FakeModel(), rng=Random(2))
u = make_hero_unit(st, "甲")
lp.set_field([u])
u.hp = 3
lp._sync_hp_to_state(u)
ok(st.read("/角色列表/甲/生命值/当前") == 3,
   "K1 ⭐ 战斗后的 HP 写回了 stat_data",
   st.read("/角色列表/甲/生命值/当前"))
ok(any("战斗结算回写" in e.reason for e in st.audit),
   "K2 回写有审计", [e.reason for e in st.audit][-3:])

u.hp = 1
st.commit([{"op": "replace", "path": "/角色列表/甲/生命值/当前", "value": 1}],
          actor="测试", reason="造旧血2")
lp._rule_explore(u, {"id": "rest"})
ok(st.read("/角色列表/甲/生命值/当前") == u.hp,
   "K3 ⭐ 短休回血也回写到 stat_data",
   (st.read("/角色列表/甲/生命值/当前"), u.hp))


# --------------------------------------------------------------------------- #
section("L. Dodge / Disengage：标记要生效，也要清掉")

u = Unit(id="D", name="回避者", hp=30, hp_max=30)
u.used["dodging"] = 1
u.used["disengaged"] = 1
u.reset_turn()
ok("dodging" not in u.used and "disengaged" not in u.used,
   "L1 ⭐ 新回合清掉回避/脱离标记", u.used)

att = Unit(id="A", name="攻击者", hp=30, hp_max=30)
mods, reasons = attack_advantage(att, u, target_dodging=True)
ok(mods.disadvantage and any("回避" in x for x in reasons),
   "L2 ⭐ 回避目标：攻击有劣势", (mods, reasons))

u2 = Unit(id="D2", name="回避者2", hp=30, hp_max=30)
u2.used["dodging"] = 1
r = saving_throw(u2, "dexterity", 12, Random(3))
ok(r.check.a.mods.advantage, "L3 ⭐ 回避：敏捷豁免有优势", r.check.a.mods)

# 脱离后「移动」不再吃借机
enemy = Unit(id="E", name="敌", hp=30, hp_max=30, team="enemy",
             position=(30.0, 0.0))
me = Unit(id="M", name="我", hp=30, hp_max=30, team="pc",
          position=(0.0, 0.0))
me.used["disengaged"] = 1
st = make_state(("甲",))
lp = GameLoop(st, Ledger(), FakeModel(), rng=Random(4))
lp.set_field([me, enemy])
menu = build_menu(me, [me, enemy])
move_opt = next(o for o in menu.options if o.kind == "move")
out = execute_intent(me, Intent(unit="M", option_id=move_opt.id, target="E"),
                     menu, [me, enemy], Random(5))
ok(out.ok and not out.opportunity,
   "L4 ⭐ 脱离后移动不引发借机攻击", (out.ok, len(out.opportunity)))
ok(me.movement_used > 0, "L5 确实移动了", me.movement_used)


# --------------------------------------------------------------------------- #
section("M. 长射程：够得着，只是劣势")

bow = Attack(name="重弩", attack_bonus=5, damage_notation="1d10",
             damage_type="piercing", is_melee=False,
             range_normal=80, range_long=320)
shooter = Unit(id="S", name="射手", hp=30, hp_max=30, team="pc")
tgt = Unit(id="T", name="靶", hp=30, hp_max=30, team="enemy")
r = resolve_attack(shooter, tgt, bow, Random(6), distance=100)
ok(r.check.a.mods.disadvantage, "M1 ⭐ 100 尺（长射程内）能打，但带劣势",
   r.check.a.mods)
ok(any("超出普通射程" in x for x in r.reasons), "M2 劣势理由写清了", r.reasons)
try:
    resolve_attack(shooter, tgt, bow, Random(6), distance=400)
    ok(False, "M3 超出最远射程仍抛错")
except AdjudicationError:
    ok(True, "M3 超出最远射程仍抛错")

# 菜单也要把长射程目标列出来
shooter.actions.append(Action(name="射击", slot="action",
                              attacks=[bow]))
menu = build_menu(shooter, [shooter, Unit(id="F", name="远靶", hp=30,
                                          hp_max=30, team="enemy",
                                          position=(100.0, 0.0))])
atk_opt = next(o for o in menu.options if o.kind == "attack")
ok("F" in atk_opt.target_ids, "M4 ⭐ 100 尺的靶进菜单（按最远射程）",
   atk_opt.target_ids)


# --------------------------------------------------------------------------- #
section("N. 速度键：中文“步行”要映射到 walk")

st = StateTree.from_template({"矮人": new_character(
    "矮人", level=3, job="战士", abilities=AB, hp_max=24, ac=16, speed=25)})
du = make_hero_unit(st, "矮人")
ok(du.speeds.get("walk") == 25 and du.speed == 25,
   "N1 ⭐ 速度 25 尺生效（原来按 30）", (du.speeds, du.speed))


# --------------------------------------------------------------------------- #
section("O. 动作经济 / 豁免距离 / 传奇留痕 / 受缚石化 / 传奇抗性")

# 动作已用 → 再攻击必须被拒（原来 if False 会放行）
me = Unit(id="M", name="我", hp=30, hp_max=30, team="pc")
foe = Unit(id="F", name="敌", hp=30, hp_max=30, team="enemy",
           position=(5.0, 0.0))
me.actions.append(Action(name="砍", slot="action",
                         attacks=[Attack(name="剑", attack_bonus=5,
                                         damage_notation="1d8",
                                         damage_type="slashing", reach=5)]))
menu = build_menu(me, [me, foe])
atk = next(o for o in menu.options if o.kind == "attack")
me.spend("action")
out = execute_intent(me, Intent(unit="M", option_id=atk.id, target="F"),
                     menu, [me, foe], Random(7))
ok(not out.ok and "已用过" in out.blocked,
   "O1 ⭐ 动作已用后再攻击被拒（不是白嫖第二次）", (out.ok, out.blocked))

# ⭐ O8：通用动作（疾走/脱离/回避/躲藏/协助/准备/描述型）同样要"再校验"动作槽。
#     O1 当时只修了攻击/法术；敌人规划一次给两步时会白嫖第二次。
u2 = Unit(id="M2", name="我二", hp=30, hp_max=30, team="pc")
u2.actions.append(Action(name="砍", slot="action",
                         attacks=[Attack(name="剑", attack_bonus=5,
                                         damage_notation="1d8",
                                         damage_type="slashing", reach=5)]))
menu2 = build_menu(u2, [u2, foe])
by_kindLag = {}
for o in menu2.options:
    by_kindLag.setdefault(o.kind, o)
u2.spend("action")
checked_kinds = 0
for kind, label in (("dash", "疾走"), ("disengage", "脱离"), ("dodge", "回避"),
                    ("hide", "躲藏"), ("help", "协助"), ("ready_attack", "准备")):
    o = by_kindLag.get(kind)
    if o is None:
        continue
    out = execute_intent(u2, Intent(unit="M2", option_id=o.id), menu2,
                         [u2, foe], Random(7))
    checked_kinds += 1
    ok(not out.ok and "已用过" in out.blocked,
       f"O10·{kind} 动作已用后再{label}被拒", (out.ok, out.blocked))
ok(checked_kinds >= 5, "O10 至少覆盖 5 种通用动作", checked_kinds)

# 描述型（special）的每日次数：登记了资源就不能白用
u3 = Unit(id="M3", name="我三", hp=30, hp_max=30, team="pc")
u3.actions.append(Action(name="吐息", slot="action", uses_per_day=1,
                         desc="一口火，引擎不自动结算"))
u3.resources["action:吐息"] = {"current": 1, "max": 1}
menu3 = build_menu(u3, [u3, foe])
sp_opt = next(o for o in menu3.options if o.kind == "special")
# 菜单是在"还有次数"时建的；执行时次数已经被花掉（菜单过期）
u3.resources["action:吐息"]["current"] = 0
out = execute_intent(u3, Intent(unit="M3", option_id=sp_opt.id), menu3,
                     [u3, foe], Random(7))
ok(not out.ok and "次数已用完" in out.blocked,
   "O10·special ⭐ 每日次数用完 → 拒绝，不白用", (out.ok, out.blocked))
u3.resources["action:吐息"]["current"] = 1
out = execute_intent(u3, Intent(unit="M3", option_id=sp_opt.id), menu3,
                     [u3, foe], Random(7))
ok(out.ok and u3.resources["action:吐息"]["current"] == 0,
   "O10·special 有次数时可用，且真的扣掉", (out.ok,
                                            u3.resources["action:吐息"]))

# 豁免型动作不按距离：200 尺外的目标不进候选
dragon = Unit(id="DR", name="龙", hp=100, hp_max=100, team="enemy")
dragon.actions.append(Action(name="吐息", slot="action",
                             save_ability="dexterity", save_dc=15,
                             damage_notation="4d6", damage_type="fire",
                             area_size=15, area_shape="cone"))
far2 = Unit(id="FF", name="远敌", hp=40, hp_max=40, team="pc",
            position=(200.0, 0.0))
menu = build_menu(dragon, [dragon, far2])
save_opt = next(o for o in menu.options if o.kind == "save_action")
ok("FF" not in save_opt.target_ids,
   "O2 ⭐ 200 尺外不进豁免候选（按 15 尺范围）", save_opt.target_ids)

# 传奇动作要逐目标留痕 + 被打倒的登记濒死
boss = Unit(id="B", name="Boss", hp=100, hp_max=100, team="enemy",
            position=(5.0, 0.0), legendary_actions_max=2)
boss.actions.append(Action(name="尾击", slot="legendary", legendary_cost=1,
                           attacks=[Attack(name="尾击", attack_bonus=20,
                                           damage_notation="2d6",
                                           damage_type="bludgeoning", reach=10)]))
pc2 = Unit(id="P", name="甲", hp=5, hp_max=5, team="pc", is_pc=True)
st2 = make_state(("甲",))
led2 = Ledger()
lp2 = GameLoop(st2, led2, FakeModel(), rng=Random(8))
lp2.set_field([pc2, boss])
log2 = RoundLog(n=1, phase="combat")
lp2._legendary_window(pc2, log2)
attack_entries = [e for e in led2.entries
                  if e.actor == "Boss" and e.data and "hit" in e.data]
ok(attack_entries, "O3 ⭐ 传奇动作有逐目标留痕（原来只有一行摘要）",
   [(e.actor, e.data) for e in led2.entries][-3:])
ok(pc2.hp <= 0 and "P" in lp2.death,
   "O4 ⭐ 被传奇动作打倒的单位登记进死亡表", (pc2.hp, list(lp2.death)))

# 受缚：敏捷豁免劣势，不是自动失败
u3 = Unit(id="R", name="受缚者", hp=30, hp_max=30)
u3.add_condition("restrained")
r = saving_throw(u3, "dexterity", 15, Random(9))
ok(r.check.a.mods.disadvantage, "O5 ⭐ 受缚：敏捷豁免劣势", r.check.a.mods)
ok(not u3.has_condition_effect("auto_fail_str_dex_save"),
   "O6 ⭐ 受缚不再自动失败")

# 石化：不触发必重击
pet = Unit(id="PE", name="石像", hp=30, hp_max=30)
pet.add_condition("petrified")
r = resolve_attack(att, pet, Attack(name="剑", attack_bonus=5,
                                    damage_notation="1d8",
                                    damage_type="slashing", reach=5),
                   Random(10), distance=5)
ok(not r.crit, "O7 ⭐ 石化目标在 5 尺内不再必重击", r.crit)
pet.hp = pet.hp_max
dmg_res = pet.take_damage([DamagePacket(10, "fire")])
ok(pet.hp == pet.hp_max - 5 and dmg_res.resisted,
   "O7b ⭐ 石化对全伤害有抗性（10 点变 5 点）", (pet.hp, dmg_res.resisted))

# 倒地：近战攻击也有劣势（原来只处理远程）
pro = Unit(id="PR", name="倒地者", hp=30, hp_max=30)
pro.add_condition("prone")
mods, reasons = attack_advantage(pro, foe)
ok(mods.disadvantage, "O8 ⭐ 倒地者的近战攻击也有劣势", reasons)

# 传奇抗性：结论覆盖，不伪造骰面
cl = Unit(id="CL", name="传奇", hp=30, hp_max=30)
got = None
for seed in range(12):
    r = saving_throw(cl, "dexterity", 20, Random(seed), can_succeed=True)
    if r.check.forced is True:
        got = r
        break
ok(got is not None and got.check.success and got.check.a.natural <= 20,
   "O9 ⭐ 传奇抗性：成功靠 forced，不是伪造 natural",
   (got.check.a.natural if got else None, got.check.success if got else None))


print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")