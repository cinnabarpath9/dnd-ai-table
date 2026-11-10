# coding=utf-8
"""P5-A 回归：座位层（TableClient / HumanSeat）+ 同桌轮（run_explore_round_all）。

守的是这四件事：

    A. 座位分发**按角色/按人**走，没配的座位**明说**，不静默串味；
    B. 真人座位的输入**校验**到位（非法 id / 非法目标 / 空输入都要重问），
       它给不出合法意图时**返回失败**，由循环兜底 —— 不假装人做了决定；
    C. 同桌轮里**每个 PC 都轮到**，调用数是 1（DM）+ N（PC），台账一人一条；
    D. 存档 / 读档接得上同桌轮（座位账也一起回来）。

全程零网络、零花费：所有座位要么是 `FakeModel`，要么是喂预设答案的 `HumanSeat`。
"""

from __future__ import annotations

import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.models import (  # noqa: E402
    FakeModel, HumanSeat, ModelRequest, TableClient, parse_intent_reply,
)
from engine.persist import SaveError, load_game, save_game  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.menu import Menu, Option  # noqa: E402
from engine.state import DM, Ledger, StateTree, new_character  # noqa: E402

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

#: 探索轮的选项形状（来自 `GameLoop._explore_options()`：没有 slot / requires_target）
EXPLORE_OPTS = [
    {"id": "look", "kind": "observe", "name": "观察", "summary": "观察周围"},
    {"id": "talk", "kind": "social", "name": "交谈", "summary": "和某人说话"},
    {"id": "search", "kind": "investigate", "name": "搜索", "summary": "搜索某处"},
]


def explore_req(actor="布兰", opts=None) -> ModelRequest:
    return ModelRequest(role="player", actor=actor, task="explore_action",
                        prompt="（上下文）", expect="intent",
                        allowed={"options": list(opts or EXPLORE_OPTS)})


def combat_opts(with_target=True) -> list[dict]:
    out = [
        {"id": "a1", "kind": "attack", "slot": "action", "name": "攻击",
         "summary": "用硬头锤打一下", "requires_target": True},
        {"id": "a2", "kind": "end_turn", "slot": "free", "name": "结束回合",
         "summary": "结束回合", "requires_target": False},
    ]
    if with_target:
        out[0]["targets"] = ["E1", "E2"]
    return out


def combat_req(actor="布兰", opts=None) -> ModelRequest:
    return ModelRequest(role="player", actor=actor, task="combat_turn",
                        prompt="（上下文）", expect="intent",
                        allowed={"options": list(opts or combat_opts())})


def answers(seq: list[str]):
    """按顺序作答；答完就抛 EOF（模拟真人关掉了输入）。"""
    box = list(seq)

    def ask(_prompt: str) -> str:
        if not box:
            raise EOFError("输入流结束")
        return box.pop(0)

    return ask


def make_table(count: int = 4):
    names = ["甲", "乙", "丙", "丁"][:count]
    st = StateTree.from_template(
        {n: new_character(n, level=3, job="战士", abilities=AB,
                          hp_max=20, ac=14) for n in names},
        current=names[0])
    st.commit([{"op": "insert", "path": f"/角色列表/{n}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}} for n in names],
              actor="填表", reason="装备长剑")
    led = Ledger()
    cli = FakeModel(name="fake-table")
    loop = GameLoop(st, led, cli, rng=Random(424242))
    loop.set_field([make_hero_unit(st, n) for n in names])
    return names, st, led, cli, loop


# --------------------------------------------------------------------------- #
section("A. 牌桌座位：按角色/按人分发，没配的座位明说")

dm_seat = FakeModel(name="dm-seat")
pl_seat = FakeModel(name="planner-seat")
a_seat = FakeModel(name="a-seat")
table = TableClient(dm=dm_seat, planner=pl_seat,
                    players={"甲": a_seat}, name="table")

r = table.complete(ModelRequest(role="dm", actor="dm", task="narrate",
                                prompt="x", expect="narration"))
ok(r.ok and "假模型" in r.text, "A1 DM 请求由 DM 座位回答")
ok(len(dm_seat.calls) == 1 and len(a_seat.calls) == 0,
   "A2 ⭐ 只进了 DM 座位（没有发给别人）", (len(dm_seat.calls), len(a_seat.calls)))
ok(len(table.calls) == 1, "A3 牌桌自己也记一笔总账", len(table.calls))

r = table.complete(combat_req("甲"))
ok(r.ok and "a1" in r.text, "A4 玩家请求按**角色名**进各自座位", r.text)
ok(len(a_seat.calls) == 1 and len(dm_seat.calls) == 1,
   "A5 甲座位的账涨了、DM 座位没涨")

r = table.complete(ModelRequest(role="planner", actor="enemy_planner",
                                task="plan_all_enemies", prompt="x"))
ok(r.ok and len(pl_seat.calls) == 1,
   "A6 敌方规划进 planner 座位（不是顺手用 DM）", len(pl_seat.calls))

r = table.complete(combat_req("不存在的人"))
ok(not r.ok and "没有为" in r.error,
   "A7 ⭐ 没配座位**明说**（不静默降级成 DM）", r.error)

table_d = TableClient(dm=FakeModel(name="dm2"), default=FakeModel(name="兜底"))
r = table_d.complete(combat_req("路人"))
ok(r.ok and "兜底" in r.model,
   "A8 配了 default 才走兜底，且能看出是兜底回的", r.model)

table_off = TableClient(dm=(dm_off := FakeModel(name="dm3")))
table_off.enabled = False
before = len(dm_off.calls)
r = table_off.complete(ModelRequest(role="dm", actor="dm", task="narrate",
                                    prompt="x", expect="narration"))
ok(not r.ok and "禁用" in r.error,
   "A9 ⭐ 牌桌总闸门关掉 → 一律不分发（座位上一次都没被调）",
   (r.error, len(dm_off.calls)))
ok(len(dm_off.calls) == before, "A10 关掉之后座位调用数没变", len(dm_off.calls))

summary = table.seat_summary()
ok("dm" in summary and "pc:甲" in summary and "planner" in summary,
   "A11 座位账能分开看（谁花了多少）", summary)


# --------------------------------------------------------------------------- #
section("B. 真人座位：合法就收，不合法就重问，给不出就失败")

h = HumanSeat(ask=answers(["look 我贴着门听一下"]))
r = h.complete(explore_req())
ok(r.ok and r.data == {"id": "look", "say": "我贴着门听一下"},
   "B1 探索：选项 + 台词解析正确", r.data)

h = HumanSeat(ask=answers(["nope", "look"]))
r = h.complete(explore_req())
ok(r.ok and r.data.get("id") == "look", "B2 先给错再给对 → 认对的", r.data)

h = HumanSeat(max_tries=3, ask=answers(["x", "y", "z"]))
r = h.complete(explore_req())
ok(not r.ok and "连续 3 次" in r.error,
   "B3 ⭐ 三次都不合法 → 返回失败（交给引擎兜底，不假装人决定了）", r.error)

printed: list[str] = []
h = HumanSeat(ask=answers(["?", "?", "look"]), out=printed.append)
r = h.complete(explore_req())
ok(r.ok and r.data.get("id") == "look", "B4 `?` 之后还能正常作答")
ok(sum(1 for s in printed if "轮到" in s) == 3,
   "B5 ⭐ `?` 只是重印菜单，**不算**一次尝试", len(printed))

h = HumanSeat(ask=answers([]))
r = h.complete(explore_req())
ok(not r.ok and "EOFError" in r.error,
   "B6 输入流断了 → 明确失败（不卡死循环）", r.error)

h = HumanSeat(max_tries=2, ask=answers(["", ""]))
r = h.complete(explore_req())
ok(not r.ok and "空输入" in r.error, "B7 空输入也算没给 → 连续两次后失败", r.error)

# 战斗：需要目标
h = HumanSeat(ask=answers(["a1 E1 我劈他"]))
r = h.complete(combat_req())
ok(r.ok and r.data == {"actions": [{"id": "a1", "target": "E1",
                                 "say": "我劈他"}]},
   "B8 战斗：选项 + 目标 + 台词 → 台词也保留", r.data)

m = Menu(unit_id="布兰", unit_name="布兰", team="pc")
m.options.append(Option(id="a1", kind="attack", slot="action", name="攻击",
                        summary="打", target_ids=["E1"], requires_target=True))
ints, probs = parse_intent_reply(r, {"布兰": m})
ok(len(ints) == 1 and ints[0].target == "E1" and not probs,
   "B9 ⭐ 真人座位交出来的意图能被引擎解析器接受", (ints, probs))

h = HumanSeat(ask=answers(["a1 E9", "a1 E2"]))
r = h.complete(combat_req())
ok(r.ok and r.data["actions"][0]["target"] == "E2",
   "B10 目标不合法 → 重问，最后用合法的", r.data)

h = HumanSeat(ask=answers(["a1"]))
r = h.complete(combat_req())
ok(not r.ok, "B11 需要目标却没给 → 不给过")

h = HumanSeat(ask=answers(["a2"]))
r = h.complete(combat_req())
ok(r.ok and r.data == {"actions": [{"id": "a2"}]},
   "B12 不需要目标 → 不带 target 键", r.data)

h = HumanSeat(ask=answers(["a1，E1"]))
r = h.complete(combat_req())
ok(r.ok and r.data["actions"][0]["target"] == "E1",
   "B13 中文逗号分隔也认（a1，E1）", r.data)

h = HumanSeat(ask=answers(["x"]))
r = h.complete(ModelRequest(role="planner", actor="enemy_planner",
                            task="plan_all_enemies", prompt="x",
                            allowed={"units": [{"unit": "E1", "options": []}]}))
ok(not r.ok and "多单位" in r.error,
   "B14 真人座位不替敌方做多单位规划", r.error)

h = HumanSeat(ask=answers(["x"]))
r = h.complete(ModelRequest(role="player", actor="布兰", task="combat_turn",
                            prompt="x", allowed={"options": []}))
ok(not r.ok and "菜单为空" in r.error, "B15 没有菜单 → 明确失败", r.error)

h = HumanSeat(ask=answers(["x"]))
r = h.complete(ModelRequest(role="dm", actor="dm", task="narrate", prompt="x",
                            expect="narration"))
ok(not r.ok and "只处理意图" in r.error,
   "B16 非意图请求（比如 DM 叙事）→ 明确拒绝", r.error)


# --------------------------------------------------------------------------- #
section("C. 同桌轮：一幕里每个 PC 都轮到")

names, st, led, cli, loop = make_table(4)
lg = loop.run_explore_round_all(situation="你们站在哨塔前。")
ok(lg.calls == 5, "C1 调用数 = 1（DM）+ 4（每个 PC）", lg.calls)
ok(all(f"【{n}】" in lg.text() for n in names),
   "C2 ⭐ 四个人都轮到了", [n for n in names if f"【{n}】" not in lg.text()])
ok(lg.problems == [], "C3 没有校验问题", lg.problems)
ok(sum(1 for e in led.entries if e.kind == "对话") == 5,
   "C4 台账：DM 1 条 + 每人 1 条公开台词",
   sum(1 for e in led.entries if e.kind == "对话"))
ok(sum(1 for e in led.entries if e.kind == "检定" and e.section == "检定") == 4,
   "C5 四条引擎裁定（骰子是引擎掷的）",
   sum(1 for e in led.entries if e.kind == "检定" and e.section == "检定"))
# P5-G2 起：观察/搜索的**结果**只给行动者（DM 也看得到），公开那句只写"做了什么"
ok(sum(1 for e in led.entries if e.section == "你的私有") >= 1,
   "C5b 观察/搜索的结果进「你的私有」，不公开",
   sum(1 for e in led.entries if e.section == "你的私有"))
ok(lg.prompt_tokens > 0 and lg.completion_tokens > 0,
   "C6 轮次记了 token 账", (lg.prompt_tokens, lg.completion_tokens))

# 倒地的人不进这一轮
loop.field[1].hp = 0
lg2 = loop.run_explore_round_all()
ok(lg2.calls == 4 and f"【{names[1]}】已倒地" in lg2.text(),
   "C7 ⭐ 倒地的人被跳过，而且**说出来**了", lg2.text())
loop.field[1].hp = 20

# 只让指定的人动
lg3 = loop.run_explore_round_all(pc_names=[names[0], names[2]])
ok(lg3.calls == 3, "C8 指定两个人 → 3 次调用（DM + 2）", lg3.calls)
ok(f"【{names[2]}】" in lg3.text() and f"【{names[3]}】" not in lg3.text(),
   "C9 没被点名的人这一轮不行动")

lg4 = loop.run_explore_round_all(pc_names=["查无此人"])
ok(any("找不到" in p for p in lg4.problems),
   "C10 ⭐ 指定的人不在场上 → 记进 problems（不静默）", lg4.problems)

# 旧口径不变（回归钉着）
lg5 = loop.run_explore_round(pc_name=names[0])
ok(lg5.calls == 2 and f"【{names[1]}】" not in lg5.text(),
   "C11 ⭐ 老的 run_explore_round 仍是 1 DM + 1 PC（P3 口径没被改坏）",
   lg5.calls)

# 日志实时外送（界面钩子）
seen: list[str] = []
_, _, _, _, loop_s = make_table(2)
loop_s.on_line = seen.append
lg6 = loop_s.run_explore_round_all()
ok(seen == lg6.lines and len(seen) >= 4,
   "C12 ⭐ on_line 逐条外送，顺序与日志一致", len(seen))


# --------------------------------------------------------------------------- #
section("D. 混座：三个模型座位 + 一个真人座位，跑同一套循环")

_names_m, st_m, led_m, _, loop_m = make_table(4)
table_m = TableClient(
    dm=FakeModel(name="dm-m"),
    planner=FakeModel(name="pl-m"),
    players={"甲": FakeModel(name="甲-m"), "乙": FakeModel(name="乙-m"),
             "丙": FakeModel(name="丙-m"),
             "丁": HumanSeat(name="丁-人", ask=answers(["look 我看看四周"]))},
)
loop_m.client = table_m
lgm = loop_m.run_explore_round_all()
ok(lgm.calls == 5 and lgm.problems == [],
   "D1 ⭐ 混座跑完一幕（4 人各一次），没有问题", (lgm.calls, lgm.problems))
seen_actors = [c.actor for c in table_m.calls if c.role == "player"]
ok(sorted(seen_actors) == sorted(["甲", "乙", "丙", "丁"]),
   "D2 每个 PC 的请求都进了自己的座位", seen_actors)
ok(len(table_m.players["丁"].calls) == 1
   and "丁-人" in table_m.players["丁"].calls[0].model,
   "D3 真人座位的调用也记了账（谁问的都看得出）")

# 战斗轮里真人座位也要能接
def pick_combat(req):
    opts = (req.allowed or {}).get("options") or []
    act = next((o for o in opts if o.get("requires_target")
                and o.get("targets")), None)
    if act:
        return f"{act['id']} {act['targets'][0]}"
    end = next((o for o in opts if o.get("kind") == "end_turn"), None)
    return str((end or (opts[0] if opts else {})).get("id") or "")

loop_m.start_combat(enemies=[("Bandit", 2)], distance=20)
loop_m.client = TableClient(
    dm=FakeModel(name="dm-m2"), planner=FakeModel(name="pl-m2"),
    players={"甲": FakeModel(name="甲-m2"), "乙": FakeModel(name="乙-m2"),
             "丙": FakeModel(name="丙-m2"),
             "丁": HumanSeat(name="丁-人", auto=pick_combat)},
)
rc = loop_m.run_combat_round()
human_calls = [c for c in loop_m.client.calls
               if c.role == "player" and c.actor == "丁"]
ok(rc is not None and len(human_calls) >= 1,
   "D4 ⭐ 战斗轮里真人座位照样轮得到", len(human_calls))
ok(any(e.section == "战斗" for e in led_m.entries),
   "D5 战斗写进了台账")


# --------------------------------------------------------------------------- #
section("E. 存档 / 读档：同桌轮和座位账一起回来")

_names2, st2, led2, _, loop2 = make_table(3)
table2 = TableClient(dm=FakeModel(name="dm-s"), planner=FakeModel(name="pl-s"),
                     players={n: FakeModel(name=f"{n}-s") for n in ["甲", "乙", "丙"]})
loop2.client = table2
loop2.run_explore_round_all(situation="存档前的一幕。")
calls_before = len(table2.calls)

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "table.json")
    save_game(path, state=st2, ledger=led2, loop=loop2, note="P5 同桌")
    ok(os.path.exists(path), "E1 存档文件写出来了")

    sess = load_game(path, client=TableClient(
        dm=FakeModel(name="dm-l"), players={"甲": FakeModel(name="甲-l"),
                                            "乙": FakeModel(name="乙-l"),
                                            "丙": FakeModel(name="丙-l")}))
    ok(len(sess.loop.client.calls) == calls_before,
       "E2 ⭐ 读档后调用记账接着算（没归零）",
       (len(sess.loop.client.calls), calls_before))
    ok(sess.loop.round == loop2.round, "E3 轮次接得上",
       (sess.loop.round, loop2.round))
    lg_after = sess.loop.run_explore_round_all()
    ok(lg_after.calls == 4 and lg_after.problems == [],
       "E4 ⭐ 读档后还能继续跑同桌轮", (lg_after.calls, lg_after.problems))

    # 不传客户端 → 默认 FakeModel（零花费），也要能跑
    sess2 = load_game(path)
    lg2 = sess2.loop.run_explore_round_all()
    ok(lg2.calls == 4, "E5 不传客户端时退回 FakeModel 照样跑（零花费）", lg2.calls)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")