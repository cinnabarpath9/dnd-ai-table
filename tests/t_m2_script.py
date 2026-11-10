# coding=utf-8
"""M2 测试本回归：两章 13 场 / 4 战。

守这些事（每一条都对应 M2 覆盖矩阵（未收录）的一行）：
    · 结构：≥12 场、4 战、两章战役文件能读、线索 id 跨章不撞；
    · 整段假模型跑通两章，场次全部走完、地点/NPC 播种、无泄漏；
    · 场景效果：三种 kind / 两种 scope / 三种 target 都真的挂到人身上，
      换场清掉、轮次效果不跨场、**敌方目标的效果要挂到战斗里生成的敌人**；
    · 距离边界：35 尺（触及 5 + 移动 30）能接敌；70 尺菜单不列空目标；
    · 跨章欠账：上一章 open 的伏笔只进 DM，不进玩家包。
"""

from __future__ import annotations

import io
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import (  # noqa: E402
    AdventureRunner, load_adventure, load_campaign,
)
from engine.adventure.campaign import chapter_path, debt_text  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

UP = os.path.join(ROOT, "adventures", "M2测试本-上-钟下的账.json")
DOWN = os.path.join(ROOT, "adventures", "M2测试本-下-水寨的账.json")
THIRD = os.path.join(ROOT, "adventures", "M2测试本-续-上游的税关.json")
CAMP = os.path.join(ROOT, "adventures", "M2测试本.campaign.json")
NAMES = ("艾尔玛", "罗恩", "小翠", "布兰")

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def make_loop():
    st = StateTree.from_template(
        {n: new_character(n, level=4, job="战士",
                          abilities={"力量": 16, "敏捷": 14, "体质": 14},
                          hp_max=32, ac=16) for n in NAMES},
        current=NAMES[0])
    st.commit([{"op": "insert", "path": f"/角色列表/{n}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}} for n in NAMES],
              actor="填表", reason="装备")
    table = TableClient(dm=FakeModel(name="dm"), planner=FakeModel(name="pl"),
                        players={n: FakeModel(name=n) for n in NAMES})
    loop = GameLoop(st, Ledger(), table, rng=Random(20261107))
    loop.set_field([make_hero_unit(st, n) for n in NAMES])
    return st, loop


def mods_of(loop, name: str) -> list[dict]:
    u = next((x for x in loop.field if x.name == name), None)
    return list(u.mods) if u else []


def mod_named(loop, name: str, mod: str) -> dict | None:
    return next((m for m in mods_of(loop, name) if m.get("name") == mod), None)


up = load_adventure(UP)
down = load_adventure(DOWN)
third = load_adventure(THIRD)

print("\n=== A. 结构：M2 门槛与两章衔接 ===")
errs_up, warns_up = up.validate()
errs_dn, warns_dn = down.validate()
ok(errs_up == [] and errs_dn == [], "A1 两章剧本都合法", errs_up + errs_dn)
ok(not warns_up and not warns_dn, "A2 没有校验提醒", warns_up + warns_dn)
errs_3, warns_3 = third.validate()
ok(errs_3 == [] and warns_3 == [], "A1b 第三章（长跑压力本）也合法",
   errs_3 + warns_3)
total_scenes = len(up.scenes) + len(down.scenes) + len(third.scenes)
ok(total_scenes >= 20, "A3 ⭐ 总场次 ≥20（长跑门槛；13 场只够证明流程通）",
   total_scenes)
all_scenes = up.scenes + down.scenes + third.scenes
combats = [s.id for s in all_scenes if s.combat]
ok(len(combats) >= 4, "A4 ⭐ 至少 4 场战斗", combats)
ok(all(len([s for s in adv.scenes if s.combat]) >= 1
       for adv in (up, down, third)),
   "A5 每章至少 1 场战斗（不是全挤在一章）")
clues = [c for s in all_scenes for c in s.clues]
ids = [c.id for c in clues]
ok(len(ids) == len(set(ids)), "A6 ⭐ 线索 id 跨章不重复（共用一本世界树）", ids)
ok(all(c.skill in ("perception", "investigation") for c in clues),
   "A7 线索只用引擎支持的两种检定", sorted({c.skill for c in clues}))
camp = load_campaign(CAMP)
ok([c.id for c in camp.chapters] == ["ch1", "ch2", "ch3"],
   "A8 战役文件三章能读", [c.id for c in camp.chapters])

# 压力夹具自身不能悄悄退化：四个压力点都写断言
c2 = third.scene("c2")
c3 = third.scene("c3")
c4 = third.scene("c4")
ok(len(third.npcs) >= 8, "A9 全章 NPC 数 ≥8", len(third.npcs))
_c2_present = c2.brief.present
_in_c2 = [n["name"] for n in third.npcs if n["name"] in _c2_present]
ok(len(_in_c2) >= 8, "A9b ⭐ 压力点：c2 的在场名单里真的写了 8+ 个 NPC", _in_c2)
ok(len(c3.clues) >= 6, "A10 ⭐ 压力点：c3 一场 6+ 条线索", len(c3.clues))
ok(bool(c4.combat) and c4.combat.get("distance") == 70,
   "A11 ⭐ 压力点：c4 70 尺混编战斗", c4.combat)
ok(len(c4.combat.get("enemies") or []) >= 3,
   "A12 ⭐ 压力点：c4 三类敌人", c4.combat.get("enemies"))
ok(len(c2.brief.present) >= 60, "A13 压力点：c2 简报足够长（体积压力）",
   len(c2.brief.present))

print("\n=== B. 整段跑完（假模型，0 元） ===")
st, loop = make_loop()
r1 = AdventureRunner(loop, up)
r1.seed_map()
r1.run_all(combat_rounds=4)
ok(r1.scenes_done == [s.id for s in up.scenes], "B1 上半 7 场跑完", r1.scenes_done)
# 同一棵树接着跑下半：这是“一章一档 + 同一本世界树”的写法
r2 = AdventureRunner(loop, down)
r2.seed_map()
r2.run_all(combat_rounds=4)
down_ids = [s.id for s in down.scenes]
ok([x for x in r2.scenes_done if x in down_ids] == down_ids,
   "B2 下半 6 场跑完（scenes_done 跨章累计是预期）",
   [x for x in r2.scenes_done if x in down_ids])
r3 = AdventureRunner(loop, third)
r3.seed_map()
r3.run_all(combat_rounds=4)
third_ids = [s.id for s in third.scenes]
ok([x for x in r3.scenes_done if x in third_ids] == third_ids,
   "B2b 第三章 8 场跑完", [x for x in r3.scenes_done if x in third_ids])
ok(loop.world.npc("盲婆") is not None and loop.world.npc("账房先生·周七") is not None,
   "B3 两章 NPC 都播种进世界树")
ok(loop.world.npc("税吏潘") is not None, "B3b 第三章 NPC 也播种了")
ok(loop.world.location("水寨水道") is not None, "B4 下半地图地点已播种")
ok("未发现私有内容泄漏" in loop.report(), "B5 跑完泄漏检查通过")
ok(not [u for u in loop.field if u.team == "enemy"], "B6 收场后没有残留敌人",
   [u.name for u in loop.field])

print("\n=== C. 场景效果：kind / scope / target ===")
st2, loop2 = make_loop()
run2 = AdventureRunner(loop2, up)
run2.seed_map()
run2.run_scene(up.scene("a1"), combat_rounds=2)
sun = mod_named(loop2, "艾尔玛", "晨光刺眼")
ok(bool(sun) and sun.get("kind") == "skill" and sun.get("value") == -1,
   "C1 ⭐ 察觉 -1 挂上队伍", sun)
run2.run_scene(up.scene("a2"), combat_rounds=2)
ok(not mod_named(loop2, "艾尔玛", "晨光刺眼"), "C2 ⭐ 换场清掉上一场的场景修正")
tea = mod_named(loop2, "艾尔玛", "热茶下肚")
ok(bool(tea) and tea.get("kind") == "ac" and tea.get("value") == 1,
   "C3 ⭐ AC +1 挂上队伍", tea)

# 轮次效果：要在战斗里观察 —— 收场时 `_close_combat` 本来就会清掉它
loop2.set_scene_effects(up.scene("a3").effects, source="a3")
loop2.start_combat(enemies=[("Bandit", 1)], distance=35)
# 撑住血量，否则一轮就打完、轮次效果还没到期战斗就结束了（那不是"没清"）
for _u in loop2.field:
    if _u.team == "enemy":
        _u.hp = _u.hp_max = 300
wet = mod_named(loop2, "艾尔玛", "湿滑地面")
ok(bool(wet) and wet.get("scope") == "round",
   "C4 ⭐ 轮次效果（攻击 -1）在战斗里挂上", wet)
loop2.run_combat_round()
once = mod_named(loop2, "艾尔玛", "湿滑地面")
ok(bool(once), "C4b 打完一轮后轮次效果还在（rounds=2）", once)
for _ in range(2):
    loop2.run_combat_round()
ok(not mod_named(loop2, "艾尔玛", "湿滑地面"),
   "C4c ⭐ 到期后被清掉（不是永久挂着）")
run2.run_scene(up.scene("a4"), combat_rounds=2)
ok(not mod_named(loop2, "艾尔玛", "湿滑地面"),
   "C5 ⭐ 轮次效果不跨场（离开闸房后清掉）")

st3, loop3 = make_loop()
run3 = AdventureRunner(loop3, down)
run3.seed_map()
run3.run_scene(down.scene("b2"), combat_rounds=4)
enemy = next((u for u in loop3.field if u.team == "enemy"), None)
ok(enemy is not None, "C6 下半 b2 生成了敌人", [u.name for u in loop3.field])
if enemy is not None:
    flicker = next((m for m in enemy.mods if m.get("name") == "火光晃眼"), None)
    ok(bool(flicker) and flicker.get("value") == -1,
       "C7 ⭐ target=enemy 的场景效果真的挂到战斗里生成的敌人", enemy.mods)
else:
    ok(False, "C7 ⭐ target=enemy 的场景效果真的挂到战斗里生成的敌人")

print("\n=== D. 距离边界：35 尺能接敌；70 尺不列空目标 ===")
from engine.rules.menu import build_menu  # noqa: E402
st4, loop4 = make_loop()
run4 = AdventureRunner(loop4, up)
run4.seed_map()
run4.run_scene(up.scene("a3"), combat_rounds=4)
log = run4.logs[-1] if run4.logs else None
any_move = any("移动" in line or "接敌" in line
               for lg in run4.logs for line in lg.lines)
ok(any_move, "D1 ⭐ 35 尺这一场出现了接敌移动（边界值真的会走）")
distance_lines = [line for lg in run4.logs for line in lg.lines
                  if "距离" in line or "尺" in line]
ok(True, f"D2 战斗日志行数 {sum(len(lg.lines) for lg in run4.logs)}"
   + (f"（示例：{distance_lines[0][:40]}）" if distance_lines else ""))

# 70 尺：菜单里不该出现"有 requires_target 但没有 target"的攻击
st5, loop5 = make_loop()
run5 = AdventureRunner(loop5, down)
run5.seed_map()
loop5.start_combat(enemies=[("Bandit", 2)], distance=70)
menus = {u.id: build_menu(u, loop5.field) for u in loop5.field
         if u.team in ("pc", "ally")}
bad = [(u.id, o.id, o.name) for u, m in menus.items() for o in m.options
       if o.kind == "attack" and o.requires_target and not o.target_ids]
ok(not bad, "D3 ⭐ 70 尺时菜单不列『有目标要求却没合法目标』的攻击", bad)
reachable = [(u.id, o.name) for u, m in menus.items()
             for o in m.options if o.kind == "attack" and o.target_ids]
ok(not reachable, "D4 ⭐ 70 尺超出『触及+移动』，近战确实一个都够不着", reachable)

print("\n=== E. 跨章欠账：只进 DM ===")
# 在临时目录里复制三份剧本，跑完上半用**真实进度文件**产出欠账，
# 避免污染仓库里的 adventures/（进度文件是玩出来的，不该由测试写）。
import shutil  # noqa: E402
import tempfile  # noqa: E402
from engine.adventure.progress import CampaignProgress, progress_path_for  # noqa: E402

with tempfile.TemporaryDirectory(prefix="dnd-m2-camp-") as td:
    for src in (UP, DOWN, THIRD, CAMP):
        shutil.copy2(src, td)
    camp2 = load_campaign(os.path.join(td, os.path.basename(CAMP)))
    up2 = load_adventure(os.path.join(td, os.path.basename(UP)))
    down2 = load_adventure(os.path.join(td, os.path.basename(DOWN)))
    pp = progress_path_for(os.path.join(td, os.path.basename(UP)))
    prog = CampaignProgress(adventure=up2.title, path=pp)
    st6, loop6 = make_loop()
    rx = AdventureRunner(loop6, up2, progress=prog)
    rx.seed_map()
    rx.run_all(combat_rounds=2)
    ok(os.path.exists(pp), "E0 上半跑完写出了进度文件", pp)
    debt = debt_text(camp2, "ch2")
    ok("p3" in debt, "E1 ⭐ 下半开局欠账里有上一章还 open 的 p3", debt[:80])
    ok("p1" not in debt and "p2" not in debt,
       "E2 ⭐ 上一章已兑现的 p1/p2 不再进欠账", debt[:120])
    ok("只有 DM" in debt, "E2b 欠账块自带可见性声明", debt.splitlines()[:1])
    run6 = AdventureRunner(loop6, down2, campaign_debt=debt)
    hint = run6.dm_hint(down2.scenes[0])
    ok("p3" in hint, "E3 ⭐ 欠账进了 DM 的任务说明")
    asm = loop6._assembler(phase="explore")
    asm.set_block("说明", "隔离", why="测试")
    loop6._set_world(asm, "艾尔玛")
    world_pack = asm.build("艾尔玛", task="隔离").text()
    ok("p3" not in world_pack and "水寨方向放了" not in world_pack,
       "E4 ⭐ 欠账没进玩家包（只走 DM 说明）")

    # 同一个存档连着跑两章（一档打通）：欠账也必须注入 ——
    # 旧实现用 `_first`（=全新一局），同一棵树跑第二章时它已是 False，欠账会被静默丢掉。
    run7 = AdventureRunner(loop6, down2, campaign_debt=debt)
    hint2 = run7.dm_hint(down2.scenes[0])
    ok("p3" in hint2,
       "E5 ⭐ 同一存档续跑第二章，欠账照样注入（不依赖『全新一局』）")
    mid = run7.dm_hint(down2.scenes[1])
    ok("p3" not in mid, "E6 第二场起不再重复塞欠账")

# --------------------------------------------------------------------------- #
print("\n=== F. 两层欠账：ch2 留一条没还，ch3 开局要看得到、也还得掉 ===")

with tempfile.TemporaryDirectory(prefix="dnd-m2-camp3-") as td3:
    for src_path in (UP, DOWN, THIRD, CAMP):
        shutil.copy2(src_path, td3)
    camp3 = load_campaign(os.path.join(td3, os.path.basename(CAMP)))
    up3 = load_adventure(os.path.join(td3, os.path.basename(UP)))
    down3 = load_adventure(os.path.join(td3, os.path.basename(DOWN)))
    third3 = load_adventure(os.path.join(td3, os.path.basename(THIRD)))
    st8, loop8 = make_loop()
    p_up = progress_path_for(os.path.join(td3, os.path.basename(UP)))
    p_dn = progress_path_for(os.path.join(td3, os.path.basename(DOWN)))
    prog_up = CampaignProgress(adventure=up3.title, path=p_up)
    r_up = AdventureRunner(loop8, up3, progress=prog_up)
    r_up.seed_map()
    r_up.run_all(combat_rounds=2)
    prog_dn = CampaignProgress(adventure=down3.title, path=p_dn)
    r_dn = AdventureRunner(loop8, down3, progress=prog_dn,
                           campaign_debt=debt_text(camp3, "ch2"),
                           campaign=camp3, chapter_id="ch2")
    r_dn.seed_map()
    r_dn.run_all(combat_rounds=2)
    ok(not prog_dn.is_closed("p1"),
       "F1 ch2 的 p1 这一轮故意没还（留给第三章）",
       [b.note[:16] for b in prog_dn.beats_of("p1")])
    debt3 = debt_text(camp3, "ch3")
    ok("p1" in debt3 and "门口的两盏灯" in debt3,
       "F2 ⭐ ch3 开局欠账里有 ch2 没还的 p1", debt3[:90])
    r_third = AdventureRunner(loop8, third3, progress=None,
                              campaign_debt=debt3,
                              campaign=camp3, chapter_id="ch3")
    r_third.seed_map()
    r_third.run_all(combat_rounds=2)
    prog_dn2 = CampaignProgress.load(p_dn, adventure=down3.title)
    ok(prog_dn2.is_closed("p1"),
       "F3 ⭐ ch3 的 c8 把 ch2 的 p1 在 ch2 进度里关掉", prog_dn2.promises.get("p1"))
    ok(not any(d["id"] == "p1" for d in
               __import__("engine.adventure", fromlist=["debts"]).debts(camp3, "ch3")),
       "F4 ⭐ 兑现后重算 ch3 欠账，p1 不再列出")

    # F5：上一章进度文件坏了，跨章兑现不能从场景中段炸出去（外审 A3）
    with io.open(p_dn, "w", encoding="utf-8") as f:
        f.write("{ 坏 JSON")
    _c8 = next(s for s in third3.scenes if s.id == "c8")
    _runner = AdventureRunner(loop8, third3, campaign=camp3, chapter_id="ch3")
    _runner.logs = [type("LG", (), {"lines": [], "add": lambda self, x: self.lines.append(x)})()]
    try:
        _runner._pay_external(_c8)
        _boom = ""
    except Exception as exc:          # noqa: BLE001
        _boom = f"{type(exc).__name__}: {exc}"
    ok(_boom == "", "F5 ⭐ 上一章进度坏了也不炸穿", _boom)
    ok(any("跨章兑现失败" in line for line in _runner.logs[-1].lines),
       "F6 坏进度要在日志里说清（不静默）", _runner.logs[-1].lines)

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")