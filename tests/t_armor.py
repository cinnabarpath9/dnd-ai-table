# coding=utf-8
"""Batch E 回归：护甲 AC 公式 + 装备换甲/盾牌重算。"""

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

from engine.models import FakeModel  # noqa: E402
from engine.rules.armor import armor_class, armor_profile  # noqa: E402
from engine.rules.loop import GameLoop  # noqa: E402
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


def section(t: str) -> None:
    print(f"\n=== {t} ===")


AB = {"力量": 16, "敏捷": 14, "体质": 14, "智力": 10, "感知": 12, "魅力": 10}


def st_with(items: dict) -> StateTree:
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士", abilities=dict(AB),
                             hp_max=30, ac=10)}, current="甲")
    for name, item in items.items():
        st.commit([{"op": "insert", "path": f"/角色列表/甲/物品/{name}",
                    "value": item}], actor="测试", reason="造装备")
    return st


# --------------------------------------------------------------------------- #
section("E-1 AC 公式（敏捷 14 → +2）")

def ac(items):
    st = st_with(items)
    val, _parts, _warn = armor_class(st, "甲")
    return val


ok(ac({}) == 12, "E-1a 无甲 = 10+2 = 12")
ok(ac({"皮甲": {"类别": "护甲", "已装备": True}}) == 13,
   "E-1b 皮甲 11+2 = 13")
ok(ac({"链甲衫": {"类别": "护甲", "已装备": True}}) == 15,
   "E-1c 链甲衫 13+2 = 15")
ok(ac({"鳞甲": {"类别": "护甲", "已装备": True}}) == 16,
   "E-1d 鳞甲 14+2（中甲上限） = 16")
ok(ac({"链甲": {"类别": "护甲", "已装备": True}}) == 16,
   "E-1e 链甲 16，不加敏捷")
ok(ac({"板甲": {"类别": "护甲", "已装备": True}}) == 18,
   "E-1f 板甲 18")
ok(ac({"链甲衫": {"类别": "护甲", "已装备": True},
       "盾牌": {"类别": "盾牌", "已装备": True}}) == 17,
   "E-1g 盾牌 +2 = 17")

ok(armor_profile("链甲衫") is not None
   and armor_profile("Chain Shirt") is not None,
   "E-1h 中英文护甲名都能查到")
ok(armor_profile("不存在的甲") is None, "E-1i 不存在的甲返回 None")


# --------------------------------------------------------------------------- #
section("E-2 多甲/力量不足的警告")

st = st_with({"皮甲": {"类别": "护甲", "已装备": True},
              "链甲": {"类别": "护甲", "已装备": True}})
val, _parts, warn = armor_class(st, "甲")
ok(val == 13 and any("多件护甲" in w for w in warn),
   "E-2a ⭐ 多件护甲只按第一件 + 警告", (val, warn))

st2 = StateTree.from_template(
    {"甲": new_character("甲", level=1, job="战士",
                         abilities={"力量": 8, "敏捷": 10, "体质": 10,
                                    "智力": 10, "感知": 10, "魅力": 10},
                         ac=10)}, current="甲")
st2.commit([{"op": "insert", "path": "/角色列表/甲/物品/板甲",
             "value": {"类别": "护甲", "已装备": True}}],
           actor="测试", reason="重甲")
_va, _pa, wa = armor_class(st2, "甲")
ok(any("力量不足" in w for w in wa), "E-2b 力量不足有警告", wa)


# --------------------------------------------------------------------------- #
section("E-3 网页换甲：卸旧甲、算新 AC、单位同步")

st3 = st_with({"链甲衫": {"类别": "护甲", "已装备": True},
               "链甲": {"类别": "护甲", "已装备": False},
               "盾牌": {"类别": "盾牌", "已装备": False}})
loop = GameLoop(st3, Ledger(), FakeModel(name="t"))
out = apply_action(st3, loop, "甲", {"kind": "equip_item", "name": "链甲"})
ok(out["ok"] is True and out.get("ac") == 16,
   "E-3a ⭐ 换链甲后 AC 16", out)
ok(st3.read("/角色列表/甲/物品/链甲衫/已装备") is False,
   "E-3b ⭐ 旧甲自动卸下")
ok(st3.read("/角色列表/甲/护甲等级") == 16, "E-3c AC 写回状态")

out = apply_action(st3, loop, "甲", {"kind": "equip_item", "name": "盾牌"})
ok(out["ok"] is True and out.get("ac") == 18,
   "E-3d ⭐ 加盾牌 AC 18", out)
ok(st3.read("/角色列表/甲/护甲等级") == 18, "E-3e 盾牌也写回")

out = apply_action(st3, loop, "甲", {"kind": "equip_item", "name": "盾牌"})
ok(out["ok"] is True and out.get("ac") == 16,
   "E-3f 卸盾回 16", out)

st4 = st_with({"神秘护甲": {"类别": "护甲", "已装备": False}})
loop4 = GameLoop(st4, Ledger(), FakeModel(name="t"))
try:
    apply_action(st4, loop4, "甲", {"kind": "equip_item", "name": "神秘护甲"})
    ok(False, "E-3g 非 SRD 护甲必须拒绝")
except ActionError:
    ok(True, "E-3g ⭐ 非 SRD 护甲 → ActionError")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")