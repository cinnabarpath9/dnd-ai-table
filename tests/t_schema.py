# coding=utf-8
"""Batch A 回归：stat_data schema（类型/范围/默认/只读）与 commit 回滚。"""

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

from engine.state import StateError, StateTree, new_character  # noqa: E402
from engine.state.schema import SCHEMA_VERSION, validate_stats  # noqa: E402
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


AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


def tree() -> StateTree:
    return StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士", abilities=dict(AB),
                             hp_max=28, ac=16)},
        current="甲")


# --------------------------------------------------------------------------- #
section("B9-1 正常角色通过 schema")

st = tree()
ok(validate_stats(st.stats) == [], "B9-1a 新角色无问题", validate_stats(st.stats))
st.commit([{"op": "delta", "path": "/角色列表/甲/生命值/当前", "value": -3}],
          actor="测试", reason="扣血")
ok(st.read("/角色列表/甲/生命值/当前") == 25, "B9-1b 合法 delta 仍然可用")
ok(SCHEMA_VERSION == 1, "B9-1c schema 版本 = 1")


# --------------------------------------------------------------------------- #
section("B9-2 类型错：写坏前拦住并回滚")

st2 = tree()
try:
    st2.commit([{"op": "replace", "path": "/角色列表/甲/生命值/当前",
                 "value": "25"}], actor="测试", reason="坏值")
    ok(False, "B9-2a 字符串生命值必须被拒")
except StateError as exc:
    ok(True, "B9-2a ⭐ 字符串生命值 → StateError")
ok(st2.read("/角色列表/甲/生命值/当前") == 28,
   "B9-2b ⭐ 失败后原值没变（整体回滚）", st2.read("/角色列表/甲/生命值/当前"))

try:
    st2.commit([{"op": "replace", "path": "/角色列表/甲/护甲等级",
                 "value": True}], actor="测试", reason="bool")
    ok(False, "B9-2c bool 当 AC 必须被拒")
except StateError:
    ok(True, "B9-2c bool 不是整数（AC 被拒）")


# --------------------------------------------------------------------------- #
section("B9-3 范围错")

try:
    tree().commit([{"op": "replace", "path": "/角色列表/甲/属性/力量",
                    "value": 99}], actor="测试", reason="超范围")
    ok(False, "B9-3a 属性 99 必须被拒")
except StateError:
    ok(True, "B9-3a 属性超出 1..30 → 拒绝")

try:
    tree().commit([{"op": "replace", "path": "/角色列表/甲/生命值/当前",
                    "value": -1}], actor="测试", reason="负数")
    ok(False, "B9-3b 负 HP 必须被拒")
except StateError:
    ok(True, "B9-3b 负生命值 → 拒绝")


# --------------------------------------------------------------------------- #
section("B9-4 结构错：物品 / 法术位 / 生命骰")

try:
    tree().commit([{"op": "insert", "path": "/角色列表/甲/物品/长剑",
                    "value": "一把剑"}], actor="测试", reason="物品不是对象")
    ok(False, "B9-4a 物品必须是对象")
except StateError:
    ok(True, "B9-4a 物品写成字符串 → 拒绝")

try:
    tree().commit([{"op": "insert", "path": "/角色列表/甲/施法/法术位/1",
                    "value": "很多"}], actor="测试", reason="法术位不是对象")
    ok(False, "B9-4b 法术位必须是对象")
except StateError:
    ok(True, "B9-4b 法术位写成字符串 → 拒绝")

try:
    tree().commit([{"op": "replace", "path": "/角色列表/甲/生命值/生命骰/面数",
                    "value": "d7"}], actor="测试", reason="骰子面数非法")
    ok(False, "B9-4c 骰子面数必须合法")
except StateError:
    ok(True, "B9-4c d7 不是合法生命骰 → 拒绝")


# --------------------------------------------------------------------------- #
section("B9-5 兼容性：未知字段放行、缺字段不补、世界根不受影响")

st3 = tree()
st3.commit([{"op": "insert", "path": "/角色列表/甲/扩展", "value": {}}],
           actor="测试", reason="扩展容器")
st3.commit([{"op": "insert", "path": "/角色列表/甲/扩展/家乡",
             "value": "北境"}], actor="测试", reason="扩展字段")
ok(st3.read("/角色列表/甲/扩展/家乡") == "北境",
   "B9-5a 未知字段放行（不在 schema 里也不拒写）")

st4 = StateTree.from_template({"乙": {"生命值": {"当前": 10, "最大": 10}}},
                              world=new_world(date="1492-09-17", period="午后"))
ok(validate_stats(st4.stats) == [], "B9-5b 缺字段的旧档/测试档不报错")
st4.commit([{"op": "replace", "path": "/世界/时间/日期", "value": "1492-09-17"}],
           actor="测试", reason="世界根")
ok(st4.read("/世界/时间/日期") == "1492-09-17",
   "B9-5c 世界根不受角色 schema 影响")


# --------------------------------------------------------------------------- #
section("B9-6 commit_diff 也走同一道闸")

st5 = tree()
bad = st5.snapshot()
bad["角色列表"]["甲"]["生命值"]["当前"] = "坏值"
try:
    st5.commit_diff(bad, actor="测试", reason="整树坏值")
    ok(False, "B9-6a commit_diff 坏值必须被拒")
except StateError:
    ok(True, "B9-6a ⭐ commit_diff 走 schema → 拒绝")
ok(st5.read("/角色列表/甲/生命值/当前") == 28,
   "B9-6b commit_diff 失败后原树不变")


# --------------------------------------------------------------------------- #
section("B9-7 存档也带 schema 版本")

from engine.longrun import build_table_loop  # noqa: E402
from engine.persist import SaveError, dump_game, load_from_dict  # noqa: E402

_st, _led, _loop = build_table_loop(seed=1, names=("A", "B"))
doc = dump_game(state=_st, ledger=_led, loop=_loop)
ok(doc.get("schema_version") == SCHEMA_VERSION,
   "B9-7a 存档写入 schema_version", doc.get("schema_version"))
bad = dict(doc)
bad["schema_version"] = 999
try:
    load_from_dict(bad)
    ok(False, "B9-7b 不匹配的 schema 版本必须拒绝")
except SaveError:
    ok(True, "B9-7b ⭐ schema 版本不匹配 → SaveError")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")