# coding=utf-8
"""U12 回归（战斗侧）：2024 规则下**用附赠动作喝药**。

守的口径：

    · 只有"背包里真有消耗品、且附赠动作还空着"时菜单才出现「喝药」；
    · 喝药消耗**附赠动作**，一回合只能喝一瓶；
    · 回血量与药水数量由引擎结算：数量归零就从 Unit 与 stat_data 同时消失；
    · 喝药后 stat_data 的 `物品/数量` 当场同步（不是等下一轮）；
    · 存档/读档带着消耗品往返。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.models.client import Intent  # noqa: E402
from engine.persist import (  # noqa: E402
    load_game, save_game, unit_from_dict, unit_to_dict,
)
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.menu import MenuError, build_menu, render_menu  # noqa: E402
from engine.rules.turn import execute_intent  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.state.patch import escape_token  # noqa: E402

FAILS: list[str] = []
COUNT = 0

AB = {"力量": 12, "敏捷": 10, "体质": 14, "智力": 10, "感知": 16, "魅力": 12}
WHO = "甲"


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


def make_state(*, potions: int = 2, cur_hp: int = 8,
               effect: str = "2d4+2") -> StateTree:
    st = StateTree.from_template(
        {WHO: new_character(WHO, level=3, job="牧师", abilities=AB,
                            hp_max=24, ac=14)}, current=WHO)
    st.commit([{"op": "replace", "path": f"/角色列表/{WHO}/生命值/当前",
                "value": int(cur_hp)}], actor="测试", reason="先掉点血")
    if potions > 0:
        item = {"类别": "消耗品", "已装备": False, "数量": int(potions),
                "英文名": "Potion of Healing"}
        if effect:
            item["效果"] = {"治疗": effect}
        st.commit([{"op": "insert", "path": f"/角色列表/{WHO}/物品/治疗药水",
                    "value": item}], actor="测试", reason="发药")
    return st


def make_unit(st: StateTree):
    return make_hero_unit(st, WHO)


def first_use_item(menu):
    return next((o for o in menu.options if o.kind == "use_item"), None)


# --------------------------------------------------------------------------- #
section("A. 单位：消耗品从 stat_data 带进战场")

st = make_state(potions=2)
u = make_unit(st)
ok(u.consumables.get("治疗药水", {}).get("数量") == 2,
   "A1 药水与数量进了 Unit", u.consumables)
ok(u.consumables["治疗药水"].get("治疗") == "2d4+2",
   "A2 效果也带进来了")
ok(any("消耗品" in n for n in u.notes), "A3 备注里写明消耗品", u.notes)

st0 = make_state(potions=0)
u0 = make_unit(st0)
ok(u0.consumables == {}, "A4 没有消耗品就是空表（不凭空发药）")

st_other = make_state(potions=1)
st_other.commit([{"op": "insert", "path": f"/角色列表/{WHO}/物品/绳子",
                  "value": {"类别": "杂物", "数量": 5}}],
                actor="测试", reason="杂物")
u_other = make_unit(st_other)
ok(set(u_other.consumables) == {"治疗药水"},
   "A5 非消耗品不进 Unit.consumables", list(u_other.consumables))


# --------------------------------------------------------------------------- #
section("B. 菜单：只有附赠动作空着才列「喝药」")

u = make_unit(make_state())
u.reset_turn()
menu = build_menu(u, [u])
opt = first_use_item(menu)
ok(opt is not None and opt.slot == "bonus_action",
   "B1 ⭐ 菜单里有喝药，占附赠动作", opt)
ok(opt is not None and opt.item == "治疗药水",
   "B2 选项指名喝哪件", opt.item if opt else None)
ok("剩 2" in render_menu(menu), "B3 菜单文本写明还剩几瓶")

u.used["bonus_action"] = 1
menu_used = build_menu(u, [u])
ok(first_use_item(menu_used) is None,
   "B4 ⭐ 附赠动作已用 → 不再列出喝药")
u.reset_turn()

u_empty = make_unit(make_state(potions=0))
u_empty.reset_turn()
menu_empty = build_menu(u_empty, [u_empty])
ok(first_use_item(menu_empty) is None, "B5 没药 → 不列喝药")


# --------------------------------------------------------------------------- #
section("C. 结算：附赠动作、回血、数量")

u = make_unit(make_state(cur_hp=8))
u.reset_turn()
menu = build_menu(u, [u])
opt = first_use_item(menu)
out = execute_intent(u, Intent(unit=u.id, option_id=opt.id), menu, [u],
                     Random(11))
ok(out.ok, "C1 喝药执行成功", out.blocked)
ok(8 < u.hp <= 24, "C2 回血了且不超上限", u.hp)
ok("附赠动作" in out.summary(), "C3 战报写明是附赠动作")
ok(u.used.get("bonus_action") == 1, "C4 ⭐ 附赠动作被扣掉", u.used)
ok(u.consumables.get("治疗药水", {}).get("数量") == 1,
   "C5 药水数量 -1", u.consumables)

out2 = execute_intent(u, Intent(unit=u.id, option_id=opt.id),
                      menu, [u], Random(12))
ok(not out2.ok and "附赠" in out2.blocked,
   "C6 ⭐ 同一回合再用旧菜单 → 附赠动作已用，被拒", out2.blocked)

u2 = make_unit(make_state(potions=1, cur_hp=4))
u2.reset_turn()
menu2 = build_menu(u2, [u2])
opt2 = first_use_item(menu2)
out3 = execute_intent(u2, Intent(unit=u2.id, option_id=opt2.id), menu2, [u2],
                      Random(13))
ok(out3.ok and "治疗药水" not in u2.consumables,
   "C7 最后一瓶喝完，从 Unit 里移出", u2.consumables)

# 物品没写效果 → 按中文名回查 SRD
u3 = make_unit(make_state(potions=1, cur_hp=5, effect=""))
u3.reset_turn()
menu3 = build_menu(u3, [u3])
opt3 = first_use_item(menu3)
out4 = execute_intent(u3, Intent(unit=u3.id, option_id=opt3.id), menu3, [u3],
                      Random(14))
ok(out4.ok and u3.hp > 5,
   "C8 ⭐ 物品没写效果时按中文名查 SRD（不静默 0 回血）", (out4.ok, u3.hp))

# 倒地不能喝
u4 = make_unit(make_state())
u4.hp = 0                              # alive 是属性（hp>0），不能赋值
u4.reset_turn()
ok(raises(lambda: build_menu(u4, [u4]), MenuError),
   "C9 ⭐ 倒地单位没有菜单（自己喝不了药）")


# --------------------------------------------------------------------------- #
section("D. stat_data 同步：喝掉的数量当场回写")

st = make_state(potions=2)
loop = GameLoop(st, Ledger(),
                TableClient(dm=FakeModel(), planner=FakeModel(), players={}),
                rng=Random(16))
loop.set_field([make_hero_unit(st, WHO)])
unit = loop.field[0]
unit.reset_turn()
menu = build_menu(unit, loop.field)
opt = first_use_item(menu)
out = execute_intent(unit, Intent(unit=unit.id, option_id=opt.id), menu,
                     loop.field, loop.rng,
                     on_use_item=loop._consume_item_state)
item = st.read(f"/角色列表/{WHO}/物品/治疗药水")
ok(out.ok and isinstance(item, dict) and item.get("数量") == 1,
   "D1 ⭐ 喝一瓶后 stat_data 里数量当场变 1", item)
ok(any("战斗中用了" in e.reason for e in st.audit),
   "D2 数量变化有审计", [e.line() for e in st.audit][-2:])

loop._sync_hp_to_state(unit, reason="测试回写")
ok(st.read(f"/角色列表/{WHO}/生命值/当前") == int(unit.hp),
   "D3 HP 回写与单位一致",
   (st.read(f"/角色列表/{WHO}/生命值/当前"), unit.hp))

# 最后一瓶 → stat_data 那条直接删掉
unit.consumables["治疗药水"] = {"数量": 1, "治疗": "2d4+2"}
unit.reset_turn()
menu = build_menu(unit, [unit])
opt = first_use_item(menu)
out = execute_intent(unit, Intent(unit=unit.id, option_id=opt.id), menu, [unit],
                     loop.rng, on_use_item=loop._consume_item_state)
ok(out.ok and st.read(f"/角色列表/{WHO}/物品/治疗药水") is None,
   "D4 最后一瓶：stat_data 里整条删掉")


# --------------------------------------------------------------------------- #
section("E. 存档：消耗品随档往返")

u = make_unit(make_state(potions=2))
d = unit_to_dict(u)
u2 = unit_from_dict(d)
ok(u2.consumables.get("治疗药水", {}).get("数量") == 2,
   "E1 ⭐ 单位序列化带消耗品", u2.consumables)

tmp = tempfile.mkdtemp(prefix="dnd-citem-")
try:
    st = make_state(potions=2)
    loop = GameLoop(st, Ledger(),
                    TableClient(dm=FakeModel(), planner=FakeModel(), players={}),
                    rng=Random(17))
    loop.set_field([make_hero_unit(st, WHO)])
    path = os.path.join(tmp, "game.json")
    save_game(path, state=st, ledger=loop.ledger, loop=loop)
    sess = load_game(path)
    saved = next(u for u in sess.loop.field if u.name == WHO)
    ok(saved.consumables.get("治疗药水", {}).get("数量") == 2,
       "E2 ⭐ 整局存档/读档后消耗品还在", saved.consumables)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
section("F. 不止药水：动作开销按物品自己走")

st = make_state(potions=1)
st.commit([
    {"op": "insert", "path": f"/角色列表/{WHO}/物品/火油",
     "value": {"类别": "消耗品", "数量": 1, "动作": "action"}},
    {"op": "insert", "path": f"/角色列表/{WHO}/物品/铁口哨",
     "value": {"类别": "消耗品", "数量": 1, "动作": "free"}},
], actor="测试", reason="杂项补给")
u = make_unit(st)
u.reset_turn()
menu = build_menu(u, [u])
oil = next((o for o in menu.options
            if o.kind == "use_item" and o.item == "火油"), None)
ok(oil is not None and oil.slot == "action",
   "F1 ⭐ 标了「动作」的物品占动作槽（不是一律附赠）", oil)
out = execute_intent(u, Intent(unit=u.id, option_id=oil.id), menu, [u],
                     Random(31))
ok(out.ok and out.needs_dm_ruling and "需 DM 裁定" in out.summary(),
   "F2 ⭐ 没有数值效果的消耗品：照常消耗 + 标需 DM 裁定",
   out.summary())
ok(u.used.get("action") == 1, "F3 动作槽被扣", u.used)
ok("火油" not in u.consumables, "F4 用完就从 Unit 移出")

u2 = make_unit(make_state(potions=1))
u2.reset_turn()
st2 = make_state(potions=1)
st2.commit([{"op": "insert", "path": f"/角色列表/{WHO}/物品/铁口哨",
             "value": {"类别": "消耗品", "数量": 1, "动作": "free"}}],
           actor="测试", reason="口哨")
u2 = make_unit(st2)
u2.reset_turn()
menu2 = build_menu(u2, [u2])
whistle = next(o for o in menu2.options if o.item == "铁口哨")
out2 = execute_intent(u2, Intent(unit=u2.id, option_id=whistle.id),
                      menu2, [u2], Random(32))
ok(out2.ok and "free" not in u2.used,
   "F5 ⭐ 自由动作的物品不占动作槽", u2.used)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")