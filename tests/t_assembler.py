# coding=utf-8
"""上下文装配器回归：每个视角只看到自己该看的。**这是防全知的验收测试。**"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.state import (  # noqa: E402
    DM, Ledger, StateTree, new_character,
)
from engine.state.assembler import (  # noqa: E402
    BLOCK_ORDER, Assembler, build, estimate_tokens,
)
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra: str = "") -> None:
    global COUNT
    COUNT += 1
    print(f"  {'PASS' if cond else 'FAIL'} {label}"
          f"{('  |  ' + str(extra)) if extra and not cond else ''}")
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


# --------------------------------------------------------------------------- #
section("A. token 估算（是估算，不是账单）")

ok(estimate_tokens("") == 0, "A1 空串 = 0")
ok(estimate_tokens("你好") == 2, "A2 两个汉字 ≈ 2 token", estimate_tokens("你好"))
n = estimate_tokens("abc def")
ok(2 <= n <= 6, "A3 两个英文词在合理区间", n)
ok(estimate_tokens("你好世界" * 100) > estimate_tokens("你好世界" * 10),
   "A4 越长越多")
ok(abs(estimate_tokens("这是一句中文") - 6) <= 2, "A5 中文按 1 字≈1 token",
   estimate_tokens("这是一句中文"))


# --------------------------------------------------------------------------- #
section("B. 装配器的基本行为")

st = StateTree.from_template({
    "艾尔玛": new_character("艾尔玛", level=3, job="战士",
                            abilities={"力量": 16, "敏捷": 14, "体质": 15,
                                       "智力": 10, "感知": 12, "魅力": 10},
                            hp_max=28, ac=16),
    "罗恩": new_character("罗恩", level=3, job="法师",
                          abilities={"力量": 8, "敏捷": 14, "体质": 12,
                                     "智力": 16, "感知": 12, "魅力": 10},
                          hp_max=18, ac=12),
}, current="艾尔玛")

led = Ledger()
led.public("公告", "你们走进了废弃的哨塔，塔里弥漫着霉味。", section="公告", round=1)
led.public("战斗", "哥布林袭击了艾尔玛，造成 7 点伤害。",
           section="战斗", actor="哥布林", round=1)
led.private("检定", "你注意到地板上有一块松动的石板。", "艾尔玛",
            section="你的私有", round=1)
led.secret("战斗", "哥布林头目其实是个变形怪，它想引你们深入。", section="战斗", round=1)

asm = Assembler(st, led)
asm.set_block("说明", "你是跑团中的一名冒险者。只输出你要做的行动。", why="系统提示")
asm.set_block("规则", "技能检定：d20 + 属性调整值 + 熟练加值 vs DC。", why="本场规则")
asm.set_block("角色卡", st.render("艾尔玛"), why="自己的状态")
asm.set_block("可选行动", "[a1] 攻击（长剑）：+5，1d8+3 挥砍，触及 5 尺\n[a2] 回避")

a = asm.build("艾尔玛", task="现在轮到你行动。")
ok(a.viewer == "艾尔玛", "B1 视角正确")
ok(a.has("说明") and a.has("规则") and a.has("角色卡") and a.has("可选行动"),
   "B2 常规块都在", a.included)
ok(a.has("台账"), "B3 台账按视角渲染出来了")
ok(a.has("任务"), "B4 任务块在")
ok(a.chars > 0 and a.tokens_est > 0, "B5 统计了字数与 token")


# --------------------------------------------------------------------------- #
section("C. ⭐ 防全知：每个视角看不到什么")

text_elma = a.text()
ok("石板" in text_elma, "C1 艾尔玛能看到自己的私有检定")
ok("变形怪" not in text_elma, "C2 ⭐ 艾尔玛看不到 DM 的秘密")
ok("哥布林袭击" in text_elma, "C3 艾尔玛能看到公开的战斗结果")

b = asm.build("罗恩", task="现在轮到你行动。")
text_ron = b.text()
ok("石板" not in text_ron, "C4 ⭐ 罗恩看不到艾尔玛的私有检定")
ok("变形怪" not in text_ron, "C5 ⭐ 罗恩也看不到 DM 的秘密")
ok("哥布林袭击" in text_ron, "C6 罗恩能看到公开信息")

d = asm.build(DM, task="描述接下来发生什么。")
text_dm = d.text()
ok("变形怪" in text_dm, "C7 DM 能看到自己的秘密")
ok("石板" in text_dm, "C8 ⭐ DM **看得到**角色的私有块（它是主持人，必须知道玩家知道什么）")


# --------------------------------------------------------------------------- #
section("D. 块级可见性（不只是台账）")

asm2 = Assembler(st)
asm2.set_block("说明", "公开的系统提示")
asm2.set_block("规则", "DM 专用规则：暗骰的用法。", visible_to=DM, why="只给 DM")
asm2.set_block("角色卡", st.render("艾尔玛"))
asm2.set_block("队友", "DM 视角的敌方密谋。", visible_to=DM)

ae = asm2.build("艾尔玛")
ok("公开的系统提示" in ae.text(), "D1 无 visible_to 的块人人可见")
ok("暗骰" not in ae.text(), "D2 ⭐ 标了 visible_to=dm 的规则块玩家拿不到")
ok(("规则", "可见性限制（只给 dm）") in ae.excluded, "D3 excluded 里记了原因",
   ae.excluded)
ok(("队友", "可见性限制（只给 dm）") in ae.excluded, "D4 同上")

adm = asm2.build(DM)
ok("暗骰" in adm.text(), "D5 DM 拿得到 dm 块")

asm3 = Assembler(st)
asm3.set_block("角色卡", st.render("罗恩"))
asm3.set_block("队友", "只有艾尔玛知道的：她的剑有诅咒。", visible_to="pc:艾尔玛")
ok("诅咒" in asm3.build("艾尔玛").text(), "D6 pc:艾尔玛 的块只有艾尔玛拿到")
ok("诅咒" not in asm3.build("罗恩").text(), "D7 ⭐ 罗恩拿不到")
ok("诅咒" in asm3.build(DM).text(),
   "D8 ⭐ DM **拿得到** pc: 块（主持人知道一切，只用来正确推进）")


# --------------------------------------------------------------------------- #
section("E. 块顺序（缓存友好的关键）")

asm4 = Assembler(st)
# 故意乱序加入
asm4.set_block("可选行动", "菜单内容")
asm4.set_block("说明", "系统说明")
asm4.set_block("角色卡", st.render("艾尔玛"))
asm4.set_block("规则", "规则内容")
a4 = asm4.build("艾尔玛", task="行动吧")
order = [b.name for b in a4.blocks]
ok(order == ["说明", "规则", "角色卡", "可选行动", "任务"],
   "E1 ⭐ 块顺序固定为 BLOCK_ORDER（与加入顺序无关）", order)
ok(order.index("说明") < order.index("角色卡") < order.index("可选行动"),
   "E2 静态在前、动态在后（保住前缀缓存）")

ok(BLOCK_ORDER[0] == "说明" and BLOCK_ORDER[-1] == "任务",
   "E3 BLOCK_ORDER 首尾是 说明/任务")

# 不认识的块名要报错（防止有人乱加块破坏顺序）
ok(raises(lambda: asm4.set_block("随便加的", "x"), ValueError),
   "E4 ⭐ 不认识的块名报错（要加块先改 BLOCK_ORDER）")

# 空块不占位
asm4.clear()
asm4.set_block("说明", "只有说明")
a5 = asm4.build("艾尔玛")
ok([b.name for b in a5.blocks] == ["说明"], "E5 空块不出现", [b.name for b in a5.blocks])
ok(("角色卡", "没提供") in a5.excluded, "E6 没提供的块进了 excluded")


# --------------------------------------------------------------------------- #
section("F. 台账渲染的透传参数")

led2 = Ledger()
for r in range(1, 13):
    led2.public("战斗", f"第 {r} 轮发生了某件写得比较长的事情，避免文本太短。",
                section="战斗", round=r)
asm5 = Assembler(st, led2)
asm5.set_block("角色卡", st.render("艾尔玛"))
a6 = asm5.build("艾尔玛", last_rounds=3)
ok("第 12 轮" in a6.text() and "第 10 轮" in a6.text(), "F1 last_rounds 透传生效")
ok("第 9 轮" not in a6.text(), "F2 更早的截断了")


# --------------------------------------------------------------------------- #
section("G. 一行装配的便捷入口")

a7 = build(st, led, "艾尔玛",
           system="你是冒险者。", rules="d20 + 调整值 vs DC",
           menu="[a1] 攻击", teammate_text="队友：罗恩 HP 18/18",
           task="轮到你行动")
ok(a7.has("说明") and a7.has("规则") and a7.has("角色卡")
   and a7.has("队友") and a7.has("可选行动") and a7.has("任务"),
   "G1 一行装配能装齐", a7.included)
ok("变形怪" not in a7.text(), "G2 ⭐ 一行装配也照样防全知")
ok("石板" in a7.text(), "G3 自己的私有还在")


# --------------------------------------------------------------------------- #
section("H. 上下文规模（对着决策记录的预算核）")

a8 = Assembler(st, led)
a8.set_block("说明", "你是一名冒险者。只输出行动。" * 8)         # 约 150 字
a8.set_block("规则", "技能检定 d20+调整值 vs DC；优势掷两颗取高。" * 30)  # 约 700 字
a8.set_block("角色卡", st.render("艾尔玛"))
a8.set_block("可选行动", "，".join(f"[a{i}] 某某动作" for i in range(1, 11)))
a9 = a8.build("艾尔玛", task="轮到你行动，选一个动作。")
print(f"    （实测：{a9.chars} 字 ≈ {a9.tokens_est} tok）")
ok(a9.tokens_est < 3000, "H1 日常轮包体在预算内（<3000 tok）", a9.tokens_est)

# 战斗包：角色卡换成"战斗必需块"（对应 §4 的两视图纪律）
a10 = Assembler(st, led)
a10.set_block("说明", "你是敌方规划的一部分。" * 5)
a10.set_block("规则", "每单位每回合 1 动作/1 附赠/1 反应/移动。" * 10)
a10.set_block("角色卡", "【E1】Bandit｜HP 11/11｜AC 12｜速度 30｜豁免 力+0 敏+1")
a10.set_block("可选行动", "，".join(f"[a{i}] 动作{i}" for i in range(1, 16)))
a11 = a10.build(DM, task="为所有敌人规划这一回合。")
print(f"    （战斗包实测：{a11.chars} 字 ≈ {a11.tokens_est} tok）")
ok(a11.tokens_est < 1500, "H2 战斗包体在预算内（<1500 tok）", a11.tokens_est)

ok(a9.tokens_est > a11.tokens_est, "H3 日常包比战斗包大（因为角色卡更全）")


# --------------------------------------------------------------------------- #
section("I. 自查")

asm6 = Assembler(st, led)
ok(raises(lambda: asm6.set_block("台账", "手动塞台账"), ValueError),
   "I1 台账由装配器现渲染，不许手动塞（这是防全知的落点）")
problems = asm6.audit("艾尔玛")
ok(any("角色卡" in p for p in problems), "I2 audit 抓出没提供角色卡", problems)
p2 = asm6.audit("不存在的人")
ok(any("没有角色" in p for p in p2), "I3 audit 抓出角色不存在", p2)
ok(asm6.audit(DM) == [], "I4 DM 视角不报问题（它本来就该看全部）")


# --------------------------------------------------------------------------- #
section("J. 硬预算：超了先裁记忆，绝不裁角色卡/菜单/任务")

long_mem = "很久以前发生过一件事，细节很多。" * 120


def mem(_viewer: str) -> str:
    return long_mem


asm7 = Assembler(st, led, memory_provider=mem)
asm7.set_block("说明", "你是冒险者。")
asm7.set_block("角色卡", st.render("艾尔玛"))
asm7.set_block("可选行动", "[a1] 攻击（长剑）\n[a2] 回避")
asm7.set_block("队友", "队友：罗恩 HP 18/18，法术位 3/4。" * 20)
asm7.set_block("世界", "当前地点：废弃哨塔，三层，夜。" * 20)
a12 = asm7.build("艾尔玛", task="轮到你行动。", max_tokens=900)
print(f"    （裁后：{a12.tokens_est} tok；dropped={a12.dropped}）")
ok(a12.dropped and a12.dropped[0][0] == "记忆",
   "J1 ⭐ 超预算先裁「记忆」（最先牺牲）", a12.dropped)
ok("记忆" not in a12.included, "J2 记忆确实没进包")
ok(a12.has("角色卡") and a12.has("可选行动") and a12.has("任务"),
   "J3 ⭐ 角色卡/菜单/任务一个不裁（裁了模型就瞎猜）", a12.included)
ok(a12.tokens_est <= 900, "J4 裁到预算内", a12.tokens_est)
ok(any("✂ 记忆" in line for line in a12.summary().splitlines()),
   "J5 summary 里能看到裁了什么")

asm8 = Assembler(st)
asm8.set_block("角色卡", "角色卡内容" * 900)      # 刻意撑爆，且不可裁
asm8.set_block("可选行动", "[a1] 攻击")
a13 = asm8.build("艾尔玛", task="行动", max_tokens=100)
ok(a13.over_budget > 0 and a13.has("角色卡"),
   "J6 ⭐ 裁不动时报 over_budget，不静默砍角色卡", a13.over_budget)
ok(a13.tokens_est > 100 and "角色卡" not in dict(a13.dropped).keys(),
   "J7 超预算时角色卡仍在 dropped 之外")

a14 = asm7.build("艾尔玛", task="行动")
ok(a14.dropped == [] and a14.over_budget == 0,
   "J8 不给 max_tokens → 行为与旧版一致（不裁）")

a15 = build(st, led, "艾尔玛", system="你是冒险者。",
            teammate_text="队友信息" * 200, max_tokens=200)
ok(any(n == "队友" for n, _ in a15.dropped),
   "J9 一行装配也吃 max_tokens", a15.dropped)

loop = GameLoop(st, led, TableClient(dm=FakeModel(), planner=FakeModel(),
                                     players={}), rng=Random(1))
ok(loop.prompt_budget is None, "J10 默认不设硬预算（行为与旧版一致）")

loop2 = GameLoop(st, led, TableClient(dm=FakeModel(), planner=FakeModel(),
                                      players={}), rng=Random(1),
                 prompt_budget=300)
asm9 = Assembler(st, led)
asm9.set_block("角色卡", st.render("艾尔玛"))
asm9.set_block("世界", "世界信息" * 300)
a16 = asm9.build("艾尔玛", task="行动", max_tokens=loop2.prompt_budget)
ok(loop2.prompt_budget == 300 and bool(a16.dropped)
   and a16.tokens_est <= 300,
   "J11 ⭐ GameLoop 的 prompt_budget 能传到装配器（超了照样裁）",
   (loop2.prompt_budget, a16.dropped, a16.tokens_est))


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")
