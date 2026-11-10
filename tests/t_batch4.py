# coding=utf-8
"""Batch 4 回归：patch 空值 / 存档字段 / 模型默认关 / 意图单位 / 出口闸嵌套。"""

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

from engine.models import FakeModel, TableClient  # noqa: E402
from engine.models.client import (  # noqa: E402
    ModelClient, ModelReply, ModelRequest, parse_intent_reply,
)
from engine.models.seats import HumanSeat  # noqa: E402
from engine.netguard import GUARD  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.spells import Spell, get_spell  # noqa: E402
from engine.rules.unit import Action, Unit  # noqa: E402
from engine.state import StateTree, new_character  # noqa: E402
from engine.persist import (  # noqa: E402
    _action_from_dict, _action_to_dict, _spell_from_dict, _spell_to_dict,
)
from engine.state.patch import PatchOpError, apply_patch  # noqa: E402
from engine.state.world import new_world  # noqa: E402

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


AB = {"力量": 10, "敏捷": 14, "体质": 12, "智力": 10, "感知": 10, "魅力": 10}

# --------------------------------------------------------------------------- #
section("B4-1 patch：缺 value / null 不许静默写 None")

doc = {"角色列表": {"甲": {"生命值": {"当前": 20}}}}
for label, op in (
    ("缺 value", {"op": "replace", "path": "/角色列表/甲/生命值/当前"}),
    ("value=null", {"op": "replace", "path": "/角色列表/甲/生命值/当前",
                    "value": None}),
    ("add 缺 value", {"op": "add", "path": "/角色列表/甲/新字段"}),
):
    try:
        apply_patch(doc, [op])
        ok(False, f"B4-1 {label} 必须被拒")
    except PatchOpError:
        ok(True, f"B4-1 {label} → PatchOpError")
ok(doc["角色列表"]["甲"]["生命值"]["当前"] == 20,
   "B4-1 失败后原值没被写坏", doc)
doc2 = apply_patch(doc, [{"op": "replace", "path": "/角色列表/甲/生命值/当前",
                          "value": 15}])
ok(doc2["角色列表"]["甲"]["生命值"]["当前"] == 15
   and doc["角色列表"]["甲"]["生命值"]["当前"] == 20, "B4-1 正常 replace 仍然可用（不改原对象）")


# --------------------------------------------------------------------------- #
section("B4-2 存档：法术投射物 / 戏法成长 / 动作射程")

missile = get_spell("Magic Missile")
d = _spell_to_dict(missile)
back = _spell_from_dict(d)
ok(back.projectiles == missile.projectiles
   and back.upcast_projectiles == missile.upcast_projectiles,
   "B4-2a ⭐ 投射物字段随档往返",
   (missile.projectiles, back.projectiles))
fire = get_spell("Fire Bolt")
back_fire = _spell_from_dict(_spell_to_dict(fire))
ok(back_fire.level_options == fire.level_options
   and back_fire.damage_at(0, 5) == "2d10",
   "B4-2b ⭐ 戏法成长档随档往返", back_fire.level_options)
act = Action(name="吐息", slot="action", save_ability="dexterity", save_dc=15,
             damage_notation="4d6", damage_type="fire", area_size=30,
             area_shape="cone", range_ft=60, structured=True)
back_act = _action_from_dict(_action_to_dict(act))
ok(back_act.range_ft == 60,
   "B4-2c ⭐ 动作射程随档往返（不再变 0 = 不限距离）", back_act.range_ft)


# --------------------------------------------------------------------------- #
section("B4-3 模型默认关：新客户端不会真发")


class Bare(ModelClient):
    def _complete(self, req):
        return ModelReply(text="{}")


bare = Bare(name="真模型占位")
ok(bare.enabled is False, "B4-3a ⭐ ModelClient 默认 enabled=False")
r = bare.complete(ModelRequest(role="dm", actor="dm", task="x", prompt="p"))
ok(r.ok is False and bare.calls[-1].attempts == 0,
   "B4-3b 默认关：一次都不发，尝试数 0", (r.ok, bare.calls[-1].attempts))
ok(FakeModel().enabled is True and TableClient(dm=FakeModel()).enabled is True
   and HumanSeat(auto=["look"]).enabled is True,
   "B4-3c 假模型/桌子/真人座位显式打开")


# --------------------------------------------------------------------------- #
section("B4-4 对象形意图：多单位不许猜单位")

a = Unit(id="A", name="甲", hp=10, hp_max=10, team="enemy", ac=10,
         position=(0.0, 0.0))
a.actions.append(Action(name="砍", slot="action", attacks=[]))
b = Unit(id="B", name="乙", hp=10, hp_max=10, team="enemy", ac=10,
         position=(5.0, 0.0))
b.actions.append(Action(name="砍", slot="action", attacks=[]))
menus = {"A": build_menu(a, [a, b], include_friendly_targets=False),
         "B": build_menu(b, [a, b], include_friendly_targets=False)}
reply = ModelReply(text='{"actions": []}', data={"actions": []}, model="fake")
intents, problems = parse_intent_reply(reply, menus)
ok(intents == [] and any("缺少 unit" in p for p in problems),
   "B4-4a ⭐ 多单位对象形：报缺 unit，不张冠李戴", problems)

single = {"A": menus["A"]}
reply2 = ModelReply(text='{"actions": []}', data={"actions": []}, model="fake")
intents2, problems2 = parse_intent_reply(reply2, single)
ok(intents2 == [] and not any("缺少 unit" in p for p in problems2),
   "B4-4b 单单位对象形仍允许（空 actions 只是没动作）", problems2)


# --------------------------------------------------------------------------- #
section("B4-5 出口闸：嵌套安装要按深度卸")

was = GUARD.active
if not was:
    GUARD.install()
GUARD.install()
ok(GUARD.active is True, "B4-5a 装两次仍 active")
GUARD.uninstall()
ok(GUARD.active is True, "B4-5b ⭐ 卸一次（内层）后外层闸还在")
GUARD.uninstall()
if was:
    GUARD.install()          # 环境变量装的那层：恢复原状
ok(GUARD.active is was, "B4-5c 恢复原状", (GUARD.active, was))


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")