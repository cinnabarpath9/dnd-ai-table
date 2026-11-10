# coding=utf-8
"""DM 临场造物进引擎：不是画在卡上的数字，是参与掷骰/AC 的值。

这一版接**物品**（武器/护甲/盾牌）与 **DM 技能**（H1：attack/save/heal）：
    · DM 造物写进当局背包/动作表，`来源=DM`；
    · 装备后 `make_hero_unit` 用 `攻击加值/伤害骰` 建 Attack；
    · 护甲用 `护甲加值/护甲类型` 重算 AC；
    · 技能按类型建 Attack / 豁免动作 / 治疗动作，菜单与回合照常结算；
    · 新开一局是新的 StateTree，这些不跟过去。
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

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, Intent  # noqa: E402
from engine.rules.armor import armor_class  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.turn import execute_intent  # noqa: E402
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


def fresh(name="甲"):
    st = StateTree.from_template({name: new_character(name, level=3)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, name)])
    return st, loop


print("=== A. DM 造武器 → 引擎照常掷骰 ===")
st, loop = fresh()
r = apply_action(st, loop, "甲", {
    "kind": "dm_create_item", "name": "绝世好剑", "category": "武器",
    "攻击加值": "100", "伤害骰": "2d6+5", "伤害类型": "挥砍",
    "描述": "DM 临场写的剑。", "机制": "命中 +100。", "背景": "没有出处。"})
ok(r.get("ok") and r.get("value", {}).get("来源") == "DM",
   "A1 DM 造物写进当局背包", r.get("message"))
item = st.read("/角色列表/甲/物品/绝世好剑")
ok(item and item.get("攻击加值") == 100 and item.get("伤害骰") == "2d6+5",
   "A2 数值真的写进物品", item)
r = apply_action(st, loop, "甲", {"kind": "equip_item", "name": "绝世好剑"})
ok(r.get("ok"), "A3 能装备 DM 造物", r.get("message"))
u = loop.find_unit("甲")
atk = u.actions[0].attacks[0] if u and u.actions and u.actions[0].attacks else None
ok(atk is not None and atk.attack_bonus == 100,
   "A4 引擎 Attack 用 DM 的 100，不是 SRD 加值", atk)
ok(atk is not None and atk.damage_notation == "2d6+5"
   and atk.damage_type == "slashing",
   "A5 伤害骰/类型按 DM 写的进引擎", (atk.damage_notation, atk.damage_type) if atk else None)
ok(st.read("/世界/物品志/绝世好剑", default={}).get("背景") == "没有出处。"
   and item.get("攻击加值") == 100,
   "A6 背景只是卡面文本，机械字段仍是 DM 数值", item)

r = apply_action(st, loop, "甲", {
    "kind": "set_item_stats", "name": "绝世好剑", "攻击加值": "50",
    "伤害骰": "1d8+10"})
ok(r.get("ok"), "A7 能改已有物品数值", r.get("message"))
u = loop.find_unit("甲")
atk2 = u.actions[0].attacks[0] if u and u.actions and u.actions[0].attacks else None
ok(atk2 is not None and atk2.attack_bonus == 50
   and atk2.damage_notation == "1d8+10",
   "A8 改完立即重算 Attack", (atk2.attack_bonus, atk2.damage_notation) if atk2 else None)

print("\n=== B. DM 造护甲 → 引擎重算 AC ===")
st2, loop2 = fresh("乙")
r = apply_action(st2, loop2, "乙", {
    "kind": "dm_create_item", "name": "龙鳞重甲", "category": "护甲",
    "护甲加值": "20", "护甲类型": "重甲"})
ok(r.get("ok"), "B1 DM 造护甲进背包", r.get("message"))
r = apply_action(st2, loop2, "乙", {"kind": "equip_item", "name": "龙鳞重甲"})
ok(r.get("ok"), "B2 能装备 DM 护甲", r.get("message"))
ac, parts, warnings = armor_class(st2, "乙")
ok(ac == 20 and not warnings, "B3 AC 按 DM 的 20 算", (ac, warnings, parts))
u2 = loop2.find_unit("乙")
ok(u2 is not None and u2.ac == 20, "B4 内存单位 AC 也同步", u2.ac if u2 else None)

print("\n=== C. 当局边界与非法值 ===")
try:
    apply_action(st2, loop2, "乙", {
        "kind": "dm_create_item", "name": "坏剑", "category": "武器",
        "攻击加值": "不是数字"})
    ok(False, "C1 非数字攻击加值直接报错")
except ActionError:
    ok(True, "C1 非数字攻击加值直接报错")
try:
    apply_action(st2, loop2, "乙", {
        "kind": "dm_create_item", "name": "龙鳞重甲", "category": "护甲"})
    ok(False, "C2 同名造物不覆盖已有物品")
except ActionError:
    ok(True, "C2 同名造物不覆盖已有物品")
fresh_st = StateTree.from_template({"乙": new_character("乙", level=3)})
ok(fresh_st.read("/角色列表/乙/物品/龙鳞重甲") is None,
   "C3 新开一局是新的状态树，DM 造物不跟过去")

print("\n=== D. DM 造攻击技能 → 引擎照常掷骰 ===")
st, loop = fresh("甲")
r = apply_action(st, loop, "甲", {
    "kind": "dm_create_action", "name": "跃斩", "类型": "attack",
    "攻击加值": "7", "伤害骰": "2d6+4", "伤害类型": "挥砍",
    "描述": "先跃起，再借落势斩下。", "机制": "命中后可把目标击退 5 尺。",
    "背景": "北境佣兵的招牌。"})
ok(r.get("ok") is True and r.get("value", {}).get("来源") == "DM",
   "D1 DM 技能写进当局动作表", r.get("message"))
act = st.read("/角色列表/甲/动作/跃斩")
ok(act and act.get("攻击加值") == 7 and act.get("伤害骰") == "2d6+4",
   "D2 机械字段真的进状态树", act)
ok("命中后" in st.read("/世界/技能志/跃斩", default={}).get("机制", ""),
   "D3 机制只进技能志（文字）", st.read("/世界/技能志/跃斩"))
u = loop.find_unit("甲")
ua = next((a for a in u.actions if a.name == "跃斩"), None)
ok(ua is not None and ua.attacks and ua.attacks[0].attack_bonus == 7
   and ua.attacks[0].damage_notation == "2d6+4"
   and ua.attacks[0].damage_type == "slashing",
   "D4 ⭐ 引擎 Action 用的是 DM 的数值",
   (ua.attacks[0] if ua and ua.attacks else None))

loop.start_combat(enemies=[("Bandit", 1)], distance=20)
pc = loop.find_unit("甲")
foe = next(x for x in loop.field if x.team == "enemy")
pc.position, foe.position = (5.0, 0.0), (0.0, 0.0)
menu = build_menu(pc, loop.field)
opt = next((o for o in menu.options if o.kind == "attack" and "跃斩" in o.name), None)
ok(opt is not None, "D5 菜单里出现 DM 技能",
   [o.name for o in menu.options if o.kind == "attack"])
hp0 = foe.hp
res = execute_intent(pc, Intent(unit=pc.id, option_id=opt.id, target=foe.id),
                     menu, loop.field, Random(11))
ok(res.ok and res.attacks and res.attacks[0].hit,
   "D6 ⭐ 攻击检定按引擎掷（种子 11 = d20 15）",
   (res.ok, res.blocked, res.attacks))
dmg = res.attacks[0].damage_total if res.attacks else 0
ok(6 <= dmg <= 16 and foe.hp == max(0, hp0 - dmg),
   "D7 伤害按 2d6+4 结算并扣血", (dmg, hp0, foe.hp))
ok(pc.used.get("action") == 1, "D8 动作槽被扣掉", pc.used)
menu2 = build_menu(pc, loop.field)
ok(not any(o.kind == "attack" and "跃斩" in o.name for o in menu2.options),
   "D9 动作已用：菜单不再列这条", [o.name for o in menu2.options])

print("\n=== E. DM 造豁免技能（范围伤害） ===")
st, loop = fresh("乙")
r = apply_action(st, loop, "乙", {
    "kind": "dm_create_action", "name": "烈焰环", "类型": "save",
    "豁免": "敏捷", "DC": "40", "半伤": True,
    "范围": "20", "范围形状": "cone",
    "伤害骰": "2d6+2", "伤害类型": "火焰"})
ok(r.get("ok") is True, "E1 豁免型技能写入", r.get("message"))
loop.start_combat(enemies=[("Bandit", 2)], distance=20)
pc = loop.find_unit("乙")
foes = [x for x in loop.field if x.team == "enemy"]
pc.position = (10.0, 0.0)
foes[0].position, foes[1].position = (0.0, 0.0), (5.0, 0.0)
menu = build_menu(pc, loop.field)
opt = next((o for o in menu.options
            if o.kind == "save_action" and o.name == "烈焰环"), None)
ok(opt is not None and set(opt.target_ids) == {f.id for f in foes},
   "E2 菜单给出范围内全部候选", opt.target_ids if opt else None)
hp0 = [f.hp for f in foes]
res = execute_intent(pc, Intent(unit=pc.id, option_id=opt.id, target=foes[0].id),
                     menu, loop.field, Random(5))
ok(res.ok and bool(res.save_results), "E3 豁免动作结算", (res.ok, res.blocked))
ok(all(row.get("success") is False
       for row in res.save_results[0].targets),
   "E4 DC 40 全失败（走满伤分支）",
   [row.get("save_summary") for row in res.save_results[0].targets])
ok(all(f.hp < h for f, h in zip(foes, hp0)),
   "E5 ⭐ 范围内每个目标都扣血", [(f.hp, h) for f, h in zip(foes, hp0)])
ok(pc.used.get("action") == 1, "E6 动作槽被扣", pc.used)

print("\n=== F. DM 造治疗技能 ===")
st = StateTree.from_template({"丙": new_character("丙", level=3, hp_max=30)})
loop = GameLoop(st, Ledger(), FakeModel())
loop.set_field([make_hero_unit(st, "丙")])
r = apply_action(st, loop, "丙", {
    "kind": "dm_create_action", "name": "回春", "类型": "heal",
    "治疗骰": "2d4+2", "射程": "30", "描述": "掌心泛起暖光。"})
ok(r.get("ok") is True, "F1 治疗型技能写入", r.get("message"))
loop.start_combat(enemies=[("Bandit", 1)], distance=30)
pc = loop.find_unit("丙")
foe = next(x for x in loop.field if x.team == "enemy")
pc.take_damage([DamagePacket(5, "")])
hp0 = pc.hp
menu = build_menu(pc, loop.field)
opt = next((o for o in menu.options
            if o.kind == "heal_action" and o.name == "回春"), None)
ok(opt is not None and pc.id in opt.target_ids,
   "F2 治疗选项把自己列为目标", opt.target_ids if opt else None)
res = execute_intent(pc, Intent(unit=pc.id, option_id=opt.id, target=pc.id),
                     menu, loop.field, Random(3))
ok(res.ok and 4 <= pc.hp - hp0 <= 10,
   "F3 ⭐ 治疗骰结算回血", (hp0, pc.hp))
res2 = execute_intent(pc, Intent(unit=pc.id, option_id=opt.id, target=foe.id),
                      menu, loop.field, Random(3))
ok(not res2.ok and "只能治疗" in res2.blocked,
   "F4 ⭐ 不能拿治疗技能打敌人", res2.blocked)

print("\n=== G. 写入口校验 / 读入口宽容 ===")
st, loop = fresh("丁")
bad_cases = [
    ({"name": "无伤", "类型": "attack", "攻击加值": "5"}, "缺伤害骰"),
    ({"name": "错骰", "类型": "attack", "伤害骰": "2d"}, "骰式看不懂"),
    ({"name": "带范围", "类型": "attack", "伤害骰": "1d6", "范围": "10"},
     "攻击型不能带范围"),
    ({"name": "塞治疗", "类型": "attack", "伤害骰": "1d6", "治疗骰": "1d4"},
     "攻击型不能带治疗骰"),
    ({"name": "无DC", "类型": "save", "豁免": "敏捷"}, "豁免型缺 DC"),
    ({"name": "无治疗", "类型": "heal"}, "治疗型缺治疗骰"),
    ({"name": "乱类型", "类型": "abc"}, "类型不认识"),
]
for payload, label in bad_cases:
    try:
        apply_action(st, loop, "丁",
                     {"kind": "dm_create_action", **payload})
        ok(False, f"G·{label} 必须被拒")
    except ActionError as exc:
        ok(True, f"G·{label} → ActionError", str(exc))
ok((st.read("/角色列表/丁/动作") or {}) == {},
   "G2 被拒的写入一条都没落库", st.read("/角色列表/丁/动作"))
apply_action(st, loop, "丁", {"kind": "dm_create_action", "name": "重名",
                              "类型": "attack", "伤害骰": "1d6"})
try:
    apply_action(st, loop, "丁", {"kind": "dm_create_action", "name": "重名",
                                  "类型": "attack", "伤害骰": "1d6"})
    ok(False, "G3 同名技能必须被拒")
except ActionError as exc:
    ok(True, "G3 同名技能 → ActionError", str(exc))
try:
    apply_action(st, loop, "丁", {"kind": "set_action_stats", "name": "不存在",
                                  "类型": "attack", "伤害骰": "1d6"})
    ok(False, "G4 改不存在的技能必须被拒")
except ActionError as exc:
    ok(True, "G4 改不存在的技能 → ActionError", str(exc))

# 手改的存档（绕过写入口）：允许写进状态树，但读入口要降级并说清原因
st.commit([{"op": "insert", "path": "/角色列表/丁/动作/半成品",
            "value": {"类型": "attack", "来源": "DM"}}],
          actor="测试", reason="造一条缺字段的技能")
u2 = make_hero_unit(st, "丁")
half = next((a for a in u2.actions if a.name == "半成品"), None)
ok(half is not None and half.structured is False,
   "G5 ⭐ 缺字段的技能降级成描述型（不静默按错规则打）", half)
ok(any("半成品" in n and "看不懂" in n for n in u2.notes),
   "G6 降级原因写进单位备注", u2.notes)

print("\n=== H. 改技能即时生效 / 当局边界 / 网页快照 ===")
st, loop = fresh("戊")
apply_action(st, loop, "戊", {"kind": "dm_create_action", "name": "刺击",
                              "类型": "attack", "伤害骰": "1d6",
                              "攻击加值": "5"})
r = apply_action(st, loop, "戊", {"kind": "set_action_stats", "name": "刺击",
                                  "类型": "attack", "伤害骰": "1d6",
                                  "攻击加值": "9"})
ok(r.get("ok") is True, "H1 改已存在的技能", r.get("message"))
u = loop.find_unit("戊")
act2 = next((a for a in u.actions if a.name == "刺击"), None)
ok(act2 is not None and act2.attacks[0].attack_bonus == 9,
   "H2 ⭐ 改完立即重算动作", act2.attacks[0] if act2 and act2.attacks else None)
ok(StateTree.from_template({"戊": new_character("戊", level=3)})
   .read("/角色列表/戊/动作/刺击") is None,
   "H3 新开一局：DM 技能不跟过去")

from engine.demo_web import sheet_snapshot  # noqa: E402
snap = sheet_snapshot(loop, "戊").get("sheet") or {}
names = {a.get("name") for a in (snap.get("dm_actions") or [])}
ok("刺击" in names, "H4 ⭐ 角色卡快照带当局技能", names)

# 存读档不能丢字段：治疗动作的 heal_notation 曾经不在序列化里
from engine.persist import unit_from_dict, unit_to_dict  # noqa: E402
st2 = StateTree.from_template({"己": new_character("己", level=3, hp_max=20)})
loop2 = GameLoop(st2, Ledger(), FakeModel())
loop2.set_field([make_hero_unit(st2, "己")])
apply_action(st2, loop2, "己", {"kind": "dm_create_action", "name": "回春",
                                "类型": "heal", "治疗骰": "2d4+2"})
u_heal = make_hero_unit(st2, "己")
back = unit_from_dict(unit_to_dict(u_heal))
heal_act = next((a for a in back.actions if a.name == "回春"), None)
ok(heal_act is not None and heal_act.heal_notation == "2d4+2",
   "H5 ⭐ 存读档不丢治疗骰（heal_notation）",
   getattr(heal_act, "heal_notation", None))

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
