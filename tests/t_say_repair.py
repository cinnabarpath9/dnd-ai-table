# coding=utf-8
"""PC 的 say 太长把 JSON 写断：补齐格式、继续执行、不再多花一次调用。"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.models import FakeModel, TableClient  # noqa: E402
from engine.models.client import repair_truncated_json  # noqa: E402
from engine.state.assembler import Assembled, Block  # noqa: E402
from engine.rules.loop import GameLoop  # noqa: E402
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.turn import DeathState  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.rules.bestiary import build_unit  # noqa: E402
from engine.rules.loop import make_hero_unit  # noqa: E402
from engine.dice import Random  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond, label, extra=""):
    global COUNT
    COUNT += 1
    print(f"  {'PASS' if cond else 'FAIL'} {label}"
          f"{('  |  ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


TRUNC = ('{"id":"a1","target":"E1","say":"小翠压低剑锋，照着脚边那只巨鼠'
         '狠狠劈下去："还')

# ---- A. 纯函数：补齐只补格式，不改内容 ----
obj = repair_truncated_json(TRUNC)
ok(isinstance(obj, dict) and obj.get("id") == "a1", "A1 截断的单个对象能补齐", obj)
ok(obj.get("target") == "E1", "A2 target 没被补丢", obj)
ok(obj.get("_truncated") is True, "A3 标记了截断（日志能看见）", obj)
ok(obj.get("say", "").startswith("小翠压低剑锋"), "A4 say 原样保留（不续写）",
   obj.get("say"))
ok(repair_truncated_json('{"id": "a1", "target": "E1", "say": "好"}').get("_truncated")
   is None, "A5 本来就合法的不打截断标记")
ok(repair_truncated_json('[{"unit":"E1"}]') is None, "A6 数组不归它管（交给打捞）")
ok(repair_truncated_json("不是 JSON") is None, "A7 完全不是 JSON 时不瞎补")

# ---- B. 接线：一次调用解决问题，不请求纠错 ----
st = StateTree.from_template(
    {"小翠": new_character("小翠", level=3, job="战士", hp_max=24, ac=15)},
    current="小翠")
st.commit([{"op": "insert", "path": "/角色列表/小翠/物品/长剑",
            "value": {"类别": "武器", "已装备": True}}], actor="填表", reason="装备")
pc = make_hero_unit(st, "小翠")
enemy, _unparsed = build_unit("Bandit", uid="E1", team="enemy")
table = TableClient(dm=FakeModel(), planner=FakeModel(),
                    players={"小翠": FakeModel(script=[TRUNC])})
loop = GameLoop(st, Ledger(), table, rng=Random(1))
loop.set_field([pc, enemy])
menu = build_menu(pc, loop.field)
loop._cur = SimpleNamespace(problems=[], calls=0, prompt_tokens=0,
                            completion_tokens=0)
reply = loop._call_checked(
    role="player", actor="小翠", task="combat_turn",
    assembled=Assembled(viewer="小翠", task="combat_turn",
                        blocks=[Block("说明", "测试", why="测试")]),
    expect="intent", menus={"小翠": menu})
ok(reply.ok and isinstance(reply.data, dict), "B1 补齐后按合法回应返回", reply.data)
ok(str(reply.data.get("id")) == "a1", "B2 动作 id 在", reply.data)
ok(loop._cur.calls == 1, "B3 ⭐ 只花了一次调用（没有为截断再花一次）",
   loop._cur.calls)
ok(any("已按 JSON 补齐" in p for p in loop._cur.problems),
   "B4 ⭐ 问题列表里如实记了『补齐』", loop._cur.problems)

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")