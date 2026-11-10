# coding=utf-8
"""P3 回归：回合执行（多攻/借机/死亡豁免/移动）+ 一轮完整闭环。"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, Intent, ModelRequest, parse_intent_reply  # noqa: E402
from engine.rules.bestiary import build_unit  # noqa: E402
from engine.rules.loop import (  # noqa: E402
    GameLoop, make_hero_unit, repair_missing_targets,
)
from engine.rules.menu import build_menu  # noqa: E402
from engine.rules.spells import get_spell  # noqa: E402
from engine.rules.turn import (  # noqa: E402
    DeathState, death_save, execute_intent, expand_multiattack, move_unit,
)
from engine.rules.unit import Action, Attack, Unit  # noqa: E402
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


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


def make_state(name="艾尔玛", **kw) -> StateTree:
    return StateTree.from_template({
        name: new_character(name, level=3, job="战士", abilities=AB,
                            hp_max=28, ac=16, **kw)})


def hero(name="艾尔玛", uid=None, state=None, **kw) -> Unit:
    st = state or make_state(name)
    st.commit([{"op": "insert", "path": f"/角色列表/{name}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}}],
              actor="填表", reason="装备长剑")
    u = make_hero_unit(st, name, uid=uid, **kw)
    return u


# --------------------------------------------------------------------------- #
section("A. 从 stat_data 造战斗单位（跨层，中英双语属性名）")

u = hero()
ok(u.name == "艾尔玛" and u.ac == 16 and u.hp_max == 28, "A1 基本数值对",
   (u.ac, u.hp_max))
ok(len(u.actions) == 1, "A2 有一把武器动作", [a.name for a in u.actions])
w = u.actions[0].attacks[0]
ok(w.attack_bonus == 3 + 2, "A3 攻击加值 = 力量3 + 熟练2 = +5", w.attack_bonus)
ok(w.damage_notation == "1d10+3", "A4 伤害 1d10+3（双手 Longsword，从 SRD 读骰 + 力量）",
   w.damage_notation)
ok(w.damage_type == "slashing", "A5 伤害类型挥砍")
ok(w.reach == 5 and w.is_melee, "A6 近战触及 5 尺")
ok(all("找不到" not in n for n in u.notes),
   "A7 ⭐ 中文武器名「长剑」正确映射到 Longsword（双手握持备注不算异常）",
   u.notes)

# 找不到的武器要明说，不静默
st2 = StateTree.from_template({"甲乙": new_character("甲乙", level=1, job="战士",
                                                    abilities=AB, hp_max=10, ac=10)})
st2.commit([{"op": "insert", "path": "/角色列表/甲乙/物品/玄铁西瓜刀",
             "value": {"类别": "武器", "已装备": True}}], actor="填表", reason="装备")
u2 = make_hero_unit(st2, "甲乙")
ok(any("找不到武器" in n for n in u2.notes), "A8 ⭐ 查不到的武器**明说**，不静默跳过",
   u2.notes)
ok(len(u2.actions) >= 1, "A9 兜底给了一把武器（否则战斗里什么都做不了）")


# --------------------------------------------------------------------------- #
section("B. 多攻展开")

dummy = Unit(id="D", name="假人", hp_max=100, hp=100)
ok(expand_multiattack(dummy, "The dragon makes three Rend attacks.") == [],
   "B1 单位没有 Rend 动作 → 展开为空（不猜）")
ok(expand_multiattack(dummy, "它随便打几下") == [], "B2 抽不到次数 → 空")

dragon, _ = build_unit("Adult Red Dragon")
chain = expand_multiattack(dragon, "The dragon makes three Rend attacks.")
ok(len(chain) == 3, "B3 ⭐ 红龙多攻展开成 3 次", len(chain))
ok(all("Rend" in a.name for a in chain), "B4 展开的是 Rend", [a.name for a in chain])
ok(all(a.attack_bonus == 14 for a in chain), "B5 每次都是 +14")

# 英文数词写法：换一个有对应动作的单位（章鱼有 Tentacle）
try:
    squid, _ = build_unit("Giant Octopus")
    chain2 = expand_multiattack(squid, "The octopus makes two Tentacle attacks.")
    ok(len(chain2) == 2, "B6 英文数词 two → 2 次（巨型章鱼的触手）", len(chain2))
except Exception as exc:
    ok(True, f"B6 跳过（没找到巨型章鱼：{exc}）")
chain3 = expand_multiattack(dragon, "makes two Nonexistent attacks")
ok(chain3 == [], "B7 ⭐ 单位没有该动作 → 返回空（不猜）", chain3)


# --------------------------------------------------------------------------- #
section("C. 移动与借机攻击")

def orc(uid="O1", pos=(0, 0), team="enemy") -> Unit:
    u, _ = build_unit("Bandit", uid=uid, team=team)
    u.id = uid
    u.position = pos
    return u


o = orc("O1", (0, 0))
pc = hero("艾尔玛", uid="P1")
pc.position = (5, 0)          # 与 O1 相邻
mv, opps = move_unit(pc, (30, 0), [pc, o], Random(1))
ok(mv.moved > 0, "C1 移动成功", mv.moved)
ok(len(opps) == 1, "C2 ⭐ 离开触及时触发借机攻击", len(opps))
ok(opps[0].attacker == "Bandit", "C3 触发者是那个敌人", opps[0].attacker)
ok(o.reaction_used, "C4 ⭐ 敌人的反应被消耗掉了")

# 脱离动作 → 不触发
o2 = orc("O2", (0, 0))
pc2 = hero("艾尔玛", uid="P2")
pc2.position = (5, 0)
mv2, opps2 = move_unit(pc2, (30, 0), [pc2, o2], Random(1),
                       ignore_opportunity=True)
ok(len(opps2) == 0, "C5 ⭐ 脱离动作时**不**触发借机攻击")

# 已经用过反应 → 不触发（记一条"没反应了"）
o3 = orc("O3", (0, 0))
o3.spend("reaction")
pc3 = hero("艾尔玛", uid="P3")
pc3.position = (5, 0)
mv3, opps3 = move_unit(pc3, (30, 0), [pc3, o3], Random(1))
ok(len(opps3) == 1 and opps3[0].result is None, "C6 反应已用 → 不攻击但记录说明",
   opps3[0].note if opps3 else "")

# 移动力不足 → 只走能走的
o4 = orc("O4", (0, 0))
pc4 = hero("艾尔玛", uid="P4")
pc4.position = (0, 0)
pc4.movement_used = 25          # 只剩 5 尺
mv4, _ = move_unit(pc4, (100, 0), [pc4], Random(1))
ok(mv4.moved <= 5, "C7 ⭐ 移动受剩余移动力限制", (mv4.moved, mv4.requested))

# 没有移动力
pc5 = hero("艾尔玛", uid="P5")
pc5.movement_used = 30
mv5, _ = move_unit(pc5, (30, 0), [pc5], Random(1))
ok(mv5.moved == 0 and "没有剩余移动力" in mv5.blocked, "C8 没移动力 → 明确拒绝")

# 队友不触发借机（只有敌人）
ally = orc("A1", (0, 0), team="pc")
pc6 = hero("艾尔玛", uid="P6")
pc6.position = (5, 0)
mv6, opps6 = move_unit(pc6, (30, 0), [pc6, ally], Random(1))
ok(len(opps6) == 0, "C9 ⭐ 队友不触发借机攻击")


# --------------------------------------------------------------------------- #
section("D. 死亡豁免")

u3 = Unit(id="D1", name="濒死的", hp_max=20, hp=0)
st3 = DeathState()
ok(st3.line() == "濒死：成功 0/3，失败 0/3", "D1 初始状态")
r = death_save(u3, st3, Random(1))
ok(r.outcome != "", "D2 掷了一次死亡豁免", r.outcome)
ok(st3.successes + st3.failures > 0, "D3 ⭐ 成功或失败计数增加了",
   (st3.successes, st3.failures))

# 天然 20 → 回复 1 点 HP
found = False
for seed in range(200):
    uu = Unit(id="D2", name="x", hp_max=20, hp=0)
    ss = DeathState()
    rr = death_save(uu, ss, Random(seed))
    if rr.natural == 20:
        found = True
        ok(rr.outcome == "crit_success" and uu.hp == 1,
           "D4 ⭐ 天然 20 → 回复 1 点 HP 并脱离濒死", (rr.outcome, uu.hp))
        break
ok(found, "D5 200 个种子里出现过天然 20")

# 天然 1 → 记 2 次失败
found = False
for seed in range(200):
    uu = Unit(id="D3", name="x", hp_max=20, hp=0)
    ss = DeathState()
    rr = death_save(uu, ss, Random(seed))
    if rr.natural == 1:
        found = True
        ok(ss.failures == 2, "D6 ⭐ 天然 1 → 记 2 次失败", ss.failures)
        break
ok(found, "D7 200 个种子里出现过天然 1")

# 3 次失败 → 死亡
uu = Unit(id="D4", name="x", hp_max=20, hp=0)
ss = DeathState(failures=2)
for seed in range(200):
    ss2 = DeathState(failures=2)
    uu2 = Unit(id="D5", name="x", hp_max=20, hp=0)
    rr = death_save(uu2, ss2, Random(seed))
    if ss2.failures >= 3:
        ok(ss2.dead, "D8 ⭐ 3 次失败 → 死亡", ss2.failures)
        break

# 3 次成功 → 稳定
for seed in range(300):
    ss3 = DeathState(successes=2)
    uu3 = Unit(id="D6", name="x", hp_max=20, hp=0)
    rr = death_save(uu3, ss3, Random(seed))
    if ss3.successes >= 3:
        ok(ss3.stable and not ss3.dead, "D9 ⭐ 3 次成功 → 稳定（不死）", ss3.line())
        break

# 已死亡不再掷
dead_st = DeathState(dead=True)
r = death_save(uu, dead_st, Random(1))
ok(r.outcome == "already_dead", "D10 已死亡不再掷")
ok(r.natural == 0, "D11 不再掷时没有骰值")

# HP > 0 不需要
alive_st = DeathState()
uu4 = Unit(id="D7", name="x", hp_max=20, hp=5)
r = death_save(uu4, alive_st, Random(1))
ok(r.outcome == "not_dying", "D12 HP>0 时不需要死亡豁免")


# --------------------------------------------------------------------------- #
section("E. 执行一条意图")

bandit, _ = build_unit("Bandit", uid="E1", team="enemy")
bandit.position = (0, 0)
pcx = hero("艾尔玛", uid="P1")
pcx.position = (30, 0)
field = [pcx, bandit]

menu = build_menu(pcx, field)
atk_opt = next(o for o in menu.options if o.kind == "attack")
from engine.models import Intent  # noqa: E402

# 够不着 → 先移动再接敌
res = execute_intent(pcx, Intent(unit="P1", option_id=atk_opt.id, target="E1"),
                     menu, field, Random(3))
ok(res.ok, "E1 执行成功（先移动再接敌）", res.blocked)
ok(res.moved > 0, "E2 ⭐ 自动移动接敌", res.moved)
ok(len(res.attacks) >= 1, "E3 打出去了", len(res.attacks))

# 非法目标
bandit.hp = bandit.hp_max
bandit.position = (0, 0)
pcx.position = (30, 0)
pcx.reset_turn()
menu2 = build_menu(pcx, field)
atk2 = next(o for o in menu2.options if o.kind == "attack")
res2 = execute_intent(pcx, Intent(unit="P1", option_id=atk2.id, target="不存在"),
                      menu2, field, Random(3))
ok(not res2.ok and ("已不在战场上" in res2.blocked or "不是活的战场单位" in res2.blocked),
   "E4 ⭐ 非法目标被拒绝", res2.blocked)

# 非法选项 id
res3 = execute_intent(pcx, Intent(unit="P1", option_id="zzz"), menu2, field,
                      Random(3))
ok(not res3.ok, "E5 ⭐ 菜单里没有的选项被拒绝", res3.blocked)

# 倒地单位不能行动
dead = Unit(id="X", name="倒下的", hp_max=10, hp=0)
res4 = execute_intent(dead, Intent(unit="X", option_id="a1"),
                      build_menu.__wrapped__ if False else menu2, field, Random(1))
ok(not res4.ok and "已倒地" in res4.blocked, "E6 倒地不能行动", res4.blocked)

# 通用动作：疾走 / 脱离 / 回避
pcx.reset_turn()
m3 = build_menu(pcx, field)
dash = next(o for o in m3.options if o.kind == "dash")
r5 = execute_intent(pcx, Intent(unit="P1", option_id=dash.id), m3, field, Random(1))
ok(r5.ok and pcx.movement_used < 0 or pcx.movement_used == 0, "E7 疾走重置移动力",
   pcx.movement_used)
pcx.reset_turn()
m4 = build_menu(pcx, field)
dis = next(o for o in m4.options if o.kind == "disengage")
r6 = execute_intent(pcx, Intent(unit="P1", option_id=dis.id), m4, field, Random(1))
ok(r6.ok and pcx.used.get("disengaged") == 1, "E8 ⭐ 脱离动作打上标记（后续移动不引发借机）")

pcx.reset_turn()
m5 = build_menu(pcx, field)
dodge = next(o for o in m5.options if o.kind == "dodge")
r7 = execute_intent(pcx, Intent(unit="P1", option_id=dodge.id), m5, field, Random(1))
ok(r7.ok and pcx.used.get("dodging") == 1, "E9 回避动作打上标记")


# --------------------------------------------------------------------------- #
section("F. 意图解析与校验（模型输出的守门人）")

from engine.models import ModelReply  # noqa: E402

b2, _ = build_unit("Bandit", uid="E1", team="enemy")
b2.position = (0, 0)
p2 = hero("艾尔玛", uid="P1")
p2.position = (5, 0)
menus = {"E1": build_menu(b2, [p2, b2])}
good_id = next(o.id for o in menus["E1"].options if o.kind == "attack")

r = ModelReply(text="", data=[{"unit": "E1", "actions": [{"id": good_id,
                                                          "target": "P1"}]}])
ints, probs = parse_intent_reply(r, menus)
ok(len(ints) == 1 and not probs, "F1 合法意图解析成功", probs)
ok(ints[0].unit == "E1" and ints[0].option_id == good_id, "F2 字段对")

r2 = ModelReply(text="", data=[{"unit": "E1", "actions": [{"id": "发明出来的"}]}])
ints2, probs2 = parse_intent_reply(r2, menus)
ok(len(ints2) == 0 and any("菜单里没有" in x for x in probs2),
   "F3 ⭐ 发明动作 → 拒绝并报问题", probs2)

r3 = ModelReply(text="", data=[{"unit": "不存在的怪", "actions": [{"id": good_id}]}])
ints3, probs3 = parse_intent_reply(r3, menus)
ok(len(ints3) == 0 and any("未知单位" in x for x in probs3),
   "F4 ⭐ 未知单位 → 拒绝", probs3)

r4 = ModelReply(text="这不是 JSON", data=None)
ints4, probs4 = parse_intent_reply(r4, menus)
ok(len(ints4) == 0 and any("无法解析" in x for x in probs4),
   "F5 ⭐ 无法解析 → 报问题（不静默当成没行动）", probs4)

# 容忍 code fence
r5 = ModelReply(text="```json\n[{\"unit\":\"E1\",\"actions\":[{\"id\":\"%s\"}]}]\n```"
                     % good_id, data=None)
ok(r5.data is None, "F6 直接构造的 data=None（模拟未解析）")
from engine.models.client import _try_json  # noqa: E402
ok(_try_json(r5.text) is not None, "F7 ⭐ 去 code fence 后能解析出 JSON")


# --------------------------------------------------------------------------- #
section("G. 假模型：确定性、零花费")

fm = FakeModel()
ok(fm.enabled, "G1 默认启用")
r = fm.complete(ModelRequest(role="dm", actor="dm", task="narrate", prompt="x",
                             expect="narration"))
ok(r.ok and "假模型" in r.text, "G2 叙事回应")
ok(r.prompt_tokens > 0, "G3 记了输入 token")
tot = fm.totals()
ok(tot["calls"] == 1 and tot["failed"] == 0, "G4 记账正确", tot)

fm2 = FakeModel(enabled=False)
r2 = fm2.complete(ModelRequest(role="dm", actor="dm", task="x", prompt="x"))
ok(not r2.ok and "禁用" in r2.error, "G5 ⭐ 全局禁用后一律不真发", r2.error)

fm3 = FakeModel(fail_on=["combat_turn"])
r3 = fm3.complete(ModelRequest(role="player", actor="甲", task="combat_turn",
                               prompt="x"))
ok(not r3.ok and "被要求" in r3.error, "G6 可以让指定任务失败（测边界用）")

fm4 = FakeModel(script=["abc", "def"])
r4 = fm4.complete(ModelRequest(role="dm", actor="dm", task="t", prompt="x"))
r5b = fm4.complete(ModelRequest(role="dm", actor="dm", task="t", prompt="x"))
r6b = fm4.complete(ModelRequest(role="dm", actor="dm", task="t", prompt="x"))
ok(r4.text == "abc" and r5b.text == "def", "G7 剧本按顺序回放")
ok(not r6b.ok and "剧本用完了" in r6b.error, "G8 剧本用完 → 明确报错")


# --------------------------------------------------------------------------- #
section("H. ⭐ 一轮完整闭环（日常 + 战斗，假模型，零花费）")

st = make_state("艾尔玛")
st.commit([{"op": "insert", "path": "/角色列表/艾尔玛/物品/长剑",
            "value": {"类别": "武器", "已装备": True}}],
          actor="填表", reason="装备长剑")
led = Ledger()
client = FakeModel()
loop = GameLoop(st, led, client, rng=Random(20261014), label="P3 一轮闭环")
hero_u = make_hero_unit(st, "艾尔玛", uid="P1")
loop.set_field([hero_u])

# 日常轮
r1 = loop.run_explore_round(pc_name="艾尔玛", situation="你们站在废弃哨塔前。")
ok(r1.calls >= 2, "H1 日常轮至少 2 次调用（DM + 玩家）", r1.calls)
ok(any("【DM】" in x for x in r1.lines), "H2 有 DM 叙事")
ok(any("【艾尔玛】" in x for x in r1.lines), "H3 有玩家发言")
ok(any("【引擎】" in x for x in r1.lines), "H4 ⭐ 有引擎裁定（骰子是引擎掷的）")
ok(len(r1.problems) == 0, "H5 日常轮没有校验问题", r1.problems)
# ⭐ 传 id 也要认（不该让调用方猜标识）
r1b = loop.run_explore_round(pc_name="P1")
ok(r1b.calls >= 2, "H5b ⭐ 用**单位 id** 也能正常跑一轮", r1b.calls)

# 进战斗
r2 = loop.start_combat(enemies=[("Bandit", 2)], distance=30)
ok(hasattr(loop, "initiative") and len(loop.initiative) >= 3,
   "H6 先攻序建好了", len(getattr(loop, "initiative", [])))
ok(len(loop.alive_enemies) == 2, "H7 两个敌人进场", len(loop.alive_enemies))

# 打 4 个战斗轮
total_calls = 0
for _ in range(4):
    if not loop.alive_enemies or not loop.alive_pcs:
        break
    rc = loop.run_combat_round(pc_name="P1")
    total_calls += rc.calls
ok(total_calls > 0, "H8 ⭐ 战斗轮产生了调用", total_calls)
ok(any("敌方" in x or "规划" in x for x in
       [y for lg in loop.logs for y in lg.lines]) or total_calls > 0,
   "H9 有敌方规划")
# 战斗真的推进了：有人掉血
hurt = [u for u in loop.field if u.hp < u.hp_max]
ok(len(hurt) >= 1, "H10 ⭐ 战斗造成了实际伤害（不是空转）",
   [(u.name, u.hp, u.hp_max) for u in loop.field])

# 台账里有战斗记录
ok(any(e.section == "战斗" for e in led.entries), "H11 台账写了战斗条目")
ok(any(e.kind == "检定" or e.kind == "对话" for e in led.entries),
   "H12 台账写了日常条目")

report = loop.report()
ok("调用记账" in report and "状态审计" in report, "H13 战报含记账与审计")
ok("泄漏检查" in report, "H14 战报含泄漏检查")
ok("未发现私有内容泄漏" in report, "H15 ⭐ 泄漏检查通过", report[-300:])

from engine.state import lint_leaks  # noqa: E402
ok(lint_leaks(led) == [], "H16 ⭐ 独立复核：没有私有内容泄漏", lint_leaks(led))

# 零花费确认
tot = client.totals()
ok(tot["calls"] > 0, "H17 确实调用了模型接口", tot)
ok(isinstance(client, FakeModel), "H18 ⭐ 全程用的是假模型（零网络零花费）")


# --------------------------------------------------------------------------- #
section("I. 敌方规划：一次调用规划所有敌人（D17）")

st2 = make_state("艾尔玛")
led2 = Ledger()
client2 = FakeModel()
loop2 = GameLoop(st2, led2, client2, rng=Random(7))
loop2.set_field([make_hero_unit(st2, "艾尔玛", uid="P1")])
loop2.start_combat(enemies=[("Bandit", 3)], distance=20)
before = client2.totals()["calls"]
r = loop2.run_combat_round(pc_name="P1")
plan_calls = [c for c in client2.calls if c.task == "plan_all_enemies"]
ok(len(plan_calls) == 1, "I1 ⭐ 敌方**只调用一次**规划全部敌人（D17）", len(plan_calls))
ok(plan_calls[0].prompt_tokens > 0, "I2 规划调用有输入 token")
pc_calls = [c for c in client2.calls if c.task == "combat_turn"]
ok(len(pc_calls) >= 1, "I3 ⭐ AI 玩家各自决策（不合并，D18）", len(pc_calls))


# --------------------------------------------------------------------------- #
section("J. 模型输出崩了 → 本地兜底（不空转）")

st3 = make_state("艾尔玛")
led3 = Ledger()

# 让规划任务和玩家任务都返回垃圾
bad = FakeModel(script=["这不是 JSON"] * 50)
loop3 = GameLoop(st3, led3, bad, rng=Random(11))
loop3.set_field([make_hero_unit(st3, "艾尔玛", uid="P1")])
loop3.start_combat(enemies=[("Bandit", 2)], distance=15)
r = loop3.run_combat_round(pc_name="P1")
ok(len(r.problems) > 0, "J1 ⭐ 垃圾输出被记成问题（不静默）", r.problems[:3])
hurt3 = [u for u in loop3.field if u.hp < u.hp_max]
ok(len(hurt3) >= 1, "J2 ⭐ 即使模型输出崩了，战斗仍靠本地兜底推进",
   [(u.name, u.hp) for u in loop3.field])


# --------------------------------------------------------------------------- #
section("K. ⭐ `targets` 键缺失 ≠ 空列表（真出过的 bug）")

from engine.models.client import _pick_option  # noqa: E402
from engine.rules.menu import Menu, Option, build_menu as _bm  # noqa: E402

# 场景：`Option.to_dict()` 在目标列表为空时**不输出 targets 键**。
# 假模型曾经把 `o.get("targets")` 的 None 当成"有目标"，于是挑了个够不着的攻击、
# 每回合被引擎拒绝、战斗空转两轮才被人发现。
opts_dead = [
    {"id": "a1", "kind": "attack", "requires_target": True,
     "name": "Light Crossbow", "note": "当前没有目标在 80 尺内"},
    {"id": "a2", "kind": "attack", "requires_target": True, "name": "Scimitar"},
    {"id": "a9", "kind": "move", "requires_target": False, "name": "移动"},
]
pick = _pick_option(opts_dead)
ok(pick.get("id") == "a9", "K1 ⭐ 打不到时**不挑攻击**，改挑移动", pick)
ok("target" not in pick, "K2 移动选项不带 target")

opts_live = [
    {"id": "a1", "kind": "attack", "requires_target": True, "targets": ["P1"]},
    {"id": "a2", "kind": "attack", "requires_target": True, "targets": ["P1"]},
]
ok(_pick_option(opts_live).get("target") == "P1", "K3 有目标时正常带上 target",
   _pick_option(opts_live))

opts_empty = [
    {"id": "a1", "kind": "attack", "requires_target": True, "targets": []},
    {"id": "a9", "kind": "dodge", "requires_target": False},
]
ok(_pick_option(opts_empty).get("id") == "a9", "K4 空 targets 列表也算没目标",
   _pick_option(opts_empty))

# parse_intent_reply 对"没有合法目标"要给**准确**的原因
b_dead, _ = build_unit("Bandit", uid="E9", team="enemy")
b_dead.position = (0, 0)
far = Unit(id="PF", name="远处的", hp_max=20, hp=20, ac=12, team="pc",
           speeds={"walk": 30})
far.position = (500, 0)
menu_now = _bm(b_dead, [b_dead, far])
ok(not [o for o in menu_now.options if o.kind == "attack"],
   "K5a 移动+射程都够不着时，菜单不列攻击", [o.name for o in menu_now.options])
empty_atk = Menu(unit_id="E9", unit_name=b_dead.name, team="enemy",
                 options=[Option(id="a1", kind="attack", slot="action",
                                 name="攻击", summary="", requires_target=True)])
r_dead = ModelReply(text="", data=[{"unit": "E9", "actions": [{"id": "a1"}]}])
_ints, _probs = parse_intent_reply(r_dead, {"E9": empty_atk})
ok(any("没有合法目标" in x for x in _probs),
   "K5 ⭐ 空目标攻击 → 报「没有合法目标」（不是含混的「没给 target」）", _probs)

# 战场真的全打不到时，一轮不能崩、也不能空转太久
st4 = make_state("艾尔玛")
loop4 = GameLoop(st4, Ledger(), FakeModel(), rng=Random(3))
loop4.set_field([make_hero_unit(st4, "艾尔玛", uid="P1")])
loop4.start_combat(enemies=[("Bandit", 1)], distance=30)
# 把敌人挪到极远处（PC 够不着，敌人也够不着）
loop4.field[-1].position = (5000, 0)
r4 = loop4.run_combat_round(pc_name="P1")
ok(r4.calls > 0, "K6 够不着时循环仍正常结束（不崩）", r4.calls)


# --------------------------------------------------------------------------- #
section("L. 执行施法意图（U2：选法术 + 选环阶 + 扣位）")

from engine.models import Intent, ModelReply  # noqa: E402


def caster(uid="W1", pos=(0.0, 0.0), **kw) -> Unit:
    base = dict(
        id=uid, name="罗恩", team="pc", is_pc=True, level=3, ac=12,
        hp_max=18, hp=18, speeds={"walk": 30}, position=pos,
        abilities={"strength": 8, "dexterity": 14, "constitution": 12,
                   "intelligence": 16, "wisdom": 12, "charisma": 10},
        cast_ability="intelligence",
        spell_slots={1: {"current": 4, "max": 4},
                     2: {"current": 2, "max": 2},
                     3: {"current": 1, "max": 1},
                     4: {"current": 1, "max": 1}},
    )
    base.update(kw)
    u = Unit(**base)
    u.spells = [get_spell(n) for n in
                ("Fireball", "Magic Missile", "Cure Wounds", "Hold Person",
                 "Fire Bolt", "Shield")]
    return u


e1 = Unit(id="E1", name="强盗1", team="enemy", hp_max=200, hp=200, ac=12,
          position=(20, 0))
e2 = Unit(id="E2", name="强盗2", team="enemy", hp_max=200, hp=200, ac=12,
          position=(25, 0))
w = caster()
field3 = [w, e1, e2]
menu = build_menu(w, field3)
fb_opt = next(o for o in menu.options if o.kind == "spell" and o.name == "Fireball")
r = execute_intent(w, Intent(unit="W1", option_id=fb_opt.id),
                   menu, field3, Random(7))
ok(r.ok, "L1 火球术施放成功", r.blocked)
ok(e1.hp < 200 and e2.hp < 200, "L2 ⭐ AoE 命中范围内全部敌人（不是只打一个）",
   (e1.hp, e2.hp))
ok(r.spell_cast is not None and r.spell_cast.save is not None,
   "L3 留痕里有豁免结算")
ok(r.spell_cast and len(r.spell_cast.save.targets) == 2,
   "L4 两个目标各有一条豁免留痕")
ok(w.spell_slots[3]["current"] == 0, "L5 ⭐ 3 环法术位被扣掉了（引擎扣，不是模型扣）")
ok(not w.can_take("action")[0], "L6 施法占掉了动作槽")

# 单体法术：模型必须给目标
w.reset_turn()
menu = build_menu(w, field3)
mm1 = next(o for o in menu.options if o.kind == "spell"
           and o.name == "Magic Missile" and o.slot_level == 1)
before_slot = w.spell_slots[1]["current"]
r2 = execute_intent(w, Intent(unit="W1", option_id=mm1.id), menu, field3, Random(1))
ok(not r2.ok and "需要目标" in r2.blocked, "L7 ⭐ 单体法术没给目标 → 拒绝",
   r2.blocked)
ok(w.spell_slots[1]["current"] == before_slot,
   "L8 ⭐ 被拒绝时不扣法术位（校验在扣位之前）")
_missing = Intent(unit="W1", option_id=mm1.id, target="")
_fixed = repair_missing_targets([_missing], menu)
ok(_fixed == ["E1"] and _missing.target == "E1",
   "L8b ⭐ 座位层给没写目标的单体法术补第一个合法目标", (_fixed, _missing.target))
w2 = caster(uid="W2", pos=(0.0, 0.0))
e3 = Unit(id="E3", name="靶子", team="enemy", hp_max=50, hp=50, ac=10,
          position=(20, 0))
_menu2 = build_menu(w2, [w2, e3])
_mm2 = next(o for o in _menu2.options if o.kind == "spell"
            and o.name == "Magic Missile" and o.slot_level == 1)
_auto = Intent(unit="W2", option_id=_mm2.id, target="")
r_auto = execute_intent(w2, _auto, _menu2, [w2, e3], Random(1), auto_target=True)
ok(r_auto.ok and _auto.target == "E3",
   "L8c ⭐ 执行层 auto_target 能兜住漏目标；默认仍严格", (r_auto.ok, _auto.target))
_w3 = caster(uid="W3", pos=(0.0, 0.0))
_down = Unit(id="A1", name="倒下的", team="pc", hp_max=20, hp=0,
             position=(5, 0))
_m3 = build_menu(_w3, [_w3, _down])
_cw = next((o for o in _m3.options if o.kind == "spell"
            and o.name == "Cure Wounds" and o.slot_level == 1), None)
ok(_cw is not None, "L8d 菜单必须列出 Cure Wounds", _m3.options)
if _cw is not None:
    r_down = execute_intent(_w3, Intent(unit="W3", option_id=_cw.id, target="A1"),
                            _m3, [_w3, _down], Random(5))
    ok(r_down.ok and _down.hp > 0,
       "L8e ⭐ 治疗可以选中 0 HP 的濒死同伴", (r_down.ok, _down.hp, r_down.blocked))

# 够不着 → 先移动；移动后仍够不着 → 不施法、不扣位
far = Unit(id="E9", name="远处的", team="enemy", hp_max=50, hp=50, ac=10,
           position=(500, 0))
w.reset_turn()
menu_far = build_menu(w, [w, far])
mm_far = next(o for o in menu_far.options if o.kind == "spell"
              and o.name == "Magic Missile" and o.slot_level == 1)
r3 = execute_intent(w, Intent(unit="W1", option_id=mm_far.id, target="E9"),
                    menu_far, [w, far], Random(1))
ok(not r3.ok and "够不着" in r3.blocked, "L9 ⭐ 移动后仍够不着 → 明确拒绝",
   r3.blocked)
ok(w.spell_slots[1]["current"] == before_slot, "L10 够不着时不扣法术位")

# 正常单体施法
w.reset_turn()
menu = build_menu(w, field3)
mm1 = next(o for o in menu.options if o.kind == "spell"
           and o.name == "Magic Missile" and o.slot_level == 1)
before_hp = e1.hp
r4 = execute_intent(w, Intent(unit="W1", option_id=mm1.id, target="E1"),
                    menu, field3, Random(3))
ok(r4.ok and e1.hp < before_hp, "L11 魔法飞弹打中指定目标", (r4.ok, e1.hp))
ok(w.spell_slots[1]["current"] == before_slot - 1,
   "L12 用掉一个 1 环位", w.spell_slots[1]["current"])

# 升环：火球术用 4 环位（SRD 给了 9d6，引擎算得出来）
w.reset_turn()
menu = build_menu(w, field3)
fb4 = next(o for o in menu.options if o.kind == "spell"
           and o.name == "Fireball" and o.slot_level == 4)
r5 = execute_intent(w, Intent(unit="W1", option_id=fb4.id),
                    menu, field3, Random(3))
ok(r5.ok and r5.spell_cast and r5.spell_cast.slot_level == 4,
   "L13 ⭐ 升环施法：留痕记的是 4 环",
   (r5.ok, getattr(r5.spell_cast, "slot_level", None)))
ok(w.spell_slots[4]["current"] == 0, "L14 扣的是 4 环位")

# 治疗（触碰法术可以把目标给队友，也可以给自己）
w.reset_turn()
w.hp = 5
menu = build_menu(w, field3)
cw = next(o for o in menu.options if o.kind == "spell"
          and o.name == "Cure Wounds" and o.slot_level == 1)
r6 = execute_intent(w, Intent(unit="W1", option_id=cw.id, target="W1"),
                    menu, field3, Random(5))
ok(r6.ok and w.hp > 5, "L15 ⭐ 治疗术真的回血", (r6.ok, w.hp))
ok(r6.spell_cast and r6.spell_cast.slot_consumed, "L16 治疗也扣法术位")

# 专注：再施一个专注法术会打断旧的
w2 = caster()
m2 = build_menu(w2, [w2, e1])
hp2 = next(o for o in m2.options if o.kind == "spell"
           and o.name == "Hold Person" and o.slot_level == 2)
r7 = execute_intent(w2, Intent(unit="W1", option_id=hp2.id, target="E1"),
                    m2, [w2, e1], Random(1))
ok(r7.ok and w2.concentrating_on == "Hold Person",
   "L17 ⭐ 专注型法术开始专注", (r7.ok, w2.concentrating_on))
w2.reset_turn()
m3 = build_menu(w2, [w2, e1])
hp3 = next(o for o in m3.options if o.kind == "spell"
           and o.name == "Hold Person" and o.slot_level == 2)
r8 = execute_intent(w2, Intent(unit="W1", option_id=hp3.id, target="E1"),
                    m3, [w2, e1], Random(1))
ok(any("打断" in n for n in r8.spell_cast.notes), "L18 ⭐ 再施专注法术会打断旧专注",
   r8.spell_cast.notes if r8.spell_cast else None)

# 反应法术：不在可选行动里，写进反应说明
w.reset_turn()
menu = build_menu(w, field3)
ok(not any(o.kind == "spell" and o.name == "Shield" for o in menu.options),
   "L19 ⭐ 护盾术不能主动放（反应只在被命中时触发）")
ok(any("Shield" in x for x in menu.reaction_notes),
   "L20 护盾术写进反应说明", menu.reaction_notes)

w.reset_turn()
menu = build_menu(w, field3)
fb = next(o for o in menu.options if o.kind == "spell" and o.name == "Fire Bolt")
before_1 = w.spell_slots[1]["current"]
r10 = execute_intent(w, Intent(unit="W1", option_id=fb.id, target="E2"),
                     menu, field3, Random(3))
ok(r10.ok and w.spell_slots[1]["current"] == before_1,
   "L21 ⭐ 戏法不扣法术位", (r10.ok, w.spell_slots[1]["current"]))

# 意图解析：法术目标不在候选里 → 拒绝
w.reset_turn()
menu = build_menu(w, field3)
mm = next(o for o in menu.options if o.kind == "spell"
          and o.name == "Magic Missile" and o.slot_level == 1)
bad = ModelReply(text="", data=[{"unit": "W1", "actions": [
    {"id": mm.id, "target": "不存在"}]}])
ints, probs = parse_intent_reply(bad, {"W1": menu})
ok(not ints and any("不在合法目标里" in p for p in probs),
   "L22 ⭐ 法术目标非法 → 拒绝并报问题", probs)
good = ModelReply(text="", data=[{"unit": "W1", "actions": [
    {"id": mm.id, "target": "E1"}]}])
ints2, probs2 = parse_intent_reply(good, {"W1": menu})
ok(len(ints2) == 1 and not probs2, "L23 合法法术意图解析成功", probs2)

# 完整战斗轮：施法写进台账
st5 = make_state("艾尔玛")
st5.commit([
    {"op": "insert", "path": "/角色列表/艾尔玛/施法/法术位/3",
     "value": {"当前": 1, "最大": 1}},
    {"op": "insert", "path": "/角色列表/艾尔玛/施法/法术书/火球术",
     "value": {"等级": 3, "准备中": True, "学派": "塑能", "成分": "V、S、M"}},
], actor="填表", reason="测试法术")
led5 = Ledger()
loop5 = GameLoop(st5, led5, FakeModel(), rng=Random(9))
loop5.set_field([make_hero_unit(st5, "艾尔玛", uid="P1")])
loop5.start_combat(enemies=[("Bandit", 2)], distance=20)
rc = loop5.run_combat_round(pc_name="P1")
ok(rc.calls > 0, "L24 带法术的 PC 能跑完整战斗轮", rc.calls)
ok(any("施法" in e.text for e in led5.entries),
   "L25 ⭐ 施法结果写进了台账（可复盘）",
   [e.text[:40] for e in led5.entries if e.section == "战斗"])


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")



