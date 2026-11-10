# coding=utf-8
"""P0-2 回归：**动作是一笔事务** —— 写到一半失败，整棵树回到动作前。

守的口径：

    · 快照是深拷贝，之后 commit 不会污染它；
    · `restore()` 只在真的改了东西时记审计（普通校验失败不刷日志）；
    · `transaction()`：块内抛异常 → 还原 + 原样抛出；正常结束 → 不还原；
    · 网页动作是事务：扣钱/给货/喝药/赶路失败，不留半截状态；
    · 回滚后内存里的 PC 单位要跟状态树重新对齐（hp/ac）。
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
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateError, StateTree, new_character  # noqa: E402
from engine.web_actions import ActionError, apply_action  # noqa: E402
import engine.web_actions as web_actions  # noqa: E402

FAILS: list[str] = []
COUNT = 0
WHO = "甲"
AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


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


def make_all() -> tuple[StateTree, GameLoop, object]:
    st = StateTree.from_template(
        {WHO: new_character(WHO, level=3, job="战士", abilities=AB,
                            hp_max=24, ac=15)}, current=WHO)
    st.commit([
        {"op": "insert", "path": f"/角色列表/{WHO}/物品/长剑",
         "value": {"类别": "武器", "已装备": False}},
        {"op": "insert", "path": f"/角色列表/{WHO}/物品/治疗药水",
         "value": {"类别": "消耗品", "数量": 2}},
    ], actor="填表", reason="装备与药水")
    st.commit([{"op": "replace", "path": f"/角色列表/{WHO}/生命值/当前",
                "value": 20}], actor="填表", reason="进本前受过伤")
    loop = GameLoop(st, Ledger(),
                    TableClient(dm=FakeModel(), planner=FakeModel(), players={}),
                    rng=Random(20261110))
    unit = make_hero_unit(st, WHO)
    unit.hp = 20
    loop.set_field([unit])
    return st, loop, unit


def audit_reasons(st: StateTree) -> list[str]:
    return [e.reason for e in st.audit]


# --------------------------------------------------------------------------- #
section("A. 快照 / 还原")

st, loop, unit = make_all()
snap = st.snapshot()
old_ac = st.read(f"/角色列表/{WHO}/护甲等级")
st.commit([{"op": "replace", "path": f"/角色列表/{WHO}/护甲等级",
            "value": old_ac + 2}], actor="测试", reason="改 AC")
ok(snap[f"角色列表"][WHO]["护甲等级"] == old_ac,
   "A1 ⭐ 快照是深拷贝：之后 commit 不污染它")
ok(st.read(f"/角色列表/{WHO}/护甲等级") == old_ac + 2, "A2 当前树已改")

n_audit = len(st.audit)
ok(st.restore(snap, actor="测试", reason="回滚测试") is True,
   "A3 restore 返回 True（确实改了）")
ok(st.read(f"/角色列表/{WHO}/护甲等级") == old_ac, "A4 值回到快照")
ok(len(st.audit) == n_audit + 1 and st.audit[-1].patch == [],
   "A5 回滚记一条审计（patch 为空）", st.audit[-1].line())
ok("回滚" in st.audit[-1].reason and "恢复原值" in st.audit[-1].changes[0],
   "A6 审计写清是回滚、恢复了几处", st.audit[-1].line())

n_audit = len(st.audit)
ok(st.restore(snap) is False, "A7 再回滚同一个快照 → False（没变化）")
ok(len(st.audit) == n_audit, "A8 没变化就不记审计（不刷日志）")

ok(raises(lambda: st.restore("不是快照"), StateError),
   "A9 快照不是对象 → 报错")
ok(raises(lambda: st.restore({"角色列表": "坏"}), StateError),
   "A10 ⭐ 快照本身不合 schema → 拒绝（不把坏档当快照）")


# --------------------------------------------------------------------------- #
section("B. 事务：异常回滚，正常提交")

st2, _loop2, _u2 = make_all()
path = f"/角色列表/{WHO}/钱币/金币"
gold0 = int(st2.read(path) or 0)
with st2.transaction(actor="测试", reason="正常动作"):
    st2.commit([{"op": "replace", "path": path, "value": gold0 + 5}],
               actor="测试", reason="加钱")
ok(int(st2.read(path)) == gold0 + 5, "B1 正常结束 → 提交保留")
ok(not any("回滚" in r for r in audit_reasons(st2)), "B2 没回滚就不记回滚审计")

try:
    with st2.transaction(actor="测试", reason="会失败的动作"):
        st2.commit([{"op": "replace", "path": path, "value": gold0 + 99}],
                   actor="测试", reason="加钱")
        raise ValueError("半路炸了")
except ValueError:
    pass
ok(int(st2.read(path)) == gold0 + 5,
   "B3 ⭐ 块内抛异常 → 整棵树回到块前（不是只回滚最后一条 patch）")
ok(st2.audit[-1].patch == [] and "会失败的动作" in st2.audit[-1].reason,
   "B4 回滚审计写清是哪个动作", st2.audit[-1].line())
ok(raises(lambda: (_ for _ in ()).throw(ValueError("x"))), "B5 自检：raises 能用")


# --------------------------------------------------------------------------- #
section("C. 网页动作：成功照常，失败不留半截")

st3, loop3, unit3 = make_all()
ok(apply_action(st3, loop3, WHO, {"kind": "equip_item", "name": "长剑"})["ok"],
   "C1 装备成功")
ok(st3.read(f"/角色列表/{WHO}/物品/长剑/已装备") is True, "C2 已装备写进去了")

# 注入失败：先提交（已装备=True），再抛
orig_toggle = web_actions._toggle_bool


def bad_toggle(st, path, *, actor="", reason=""):
    out = orig_toggle(st, path, actor=actor, reason=reason)
    raise RuntimeError("提交之后炸了")


web_actions._toggle_bool = bad_toggle
try:
    err = None
    try:
        apply_action(st3, loop3, WHO, {"kind": "equip_item", "name": "长剑"})
    except ActionError as exc:
        err = str(exc)
finally:
    web_actions._toggle_bool = orig_toggle
ok(err is not None and "已回滚" in err, "C3 ⭐ 失败转成「已回滚」错误", err)
ok(st3.read(f"/角色列表/{WHO}/物品/长剑/已装备") is True,
   "C4 ⭐ 半截状态被还原（回到这次动作之前的 True）")
ok(any("失败，整体回滚" in r for r in audit_reasons(st3)),
   "C5 回滚有审计", audit_reasons(st3)[-2:])

# 喝药：提交后同步 unit.hp，再抛 → 状态与单位都要回滚
orig_use = web_actions.use_consumable


def bad_use(state, character, name, **kw):
    out = orig_use(state, character, name, **kw)
    unit3.hp = 999
    raise RuntimeError("喝完才炸")


web_actions.use_consumable = bad_use
try:
    err = None
    try:
        apply_action(st3, loop3, WHO, {"kind": "use_item", "name": "治疗药水"})
    except ActionError as exc:
        err = str(exc)
finally:
    web_actions.use_consumable = orig_use
qty = st3.read(f"/角色列表/{WHO}/物品/治疗药水/数量")
ok(err is not None and qty == 2,
   "C6 ⭐ 喝药失败：数量回到 2（没有白喝）", qty)
ok(unit3.hp == 20,
   "C7 ⭐ 内存 Unit 也回到回滚后的 HP（不留下 999）", unit3.hp)

# 校验失败（没写任何东西）：不该产生回滚审计
n = len(st3.audit)
ok(raises(lambda: apply_action(st3, loop3, WHO, {"kind": "不存在的动作"}),
          ActionError),
   "C8 不认识的动作为 ActionError")
ok(len(st3.audit) == n and not any("整体回滚" in r
                                   for r in audit_reasons(st3)[n:]),
   "C9 ⭐ 纯校验失败不刷回滚日志")

# 战斗中：动作在入口就被拒，也不该留回滚
loop3.start_combat(enemies=[("Bandit", 1)], distance=20)
n = len(st3.audit)
ok(raises(lambda: apply_action(st3, loop3, WHO,
                               {"kind": "equip_item", "name": "长剑"}),
          ActionError),
   "C10 战斗中换装被拒")
ok(len(st3.audit) == n, "C11 被拒的动作没写状态、也没写回滚审计")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")