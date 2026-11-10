# coding=utf-8
"""M1 入场剧本回归：《雾渡的第三声钟》（原创 6 场 / 2 战）。

守这些事：
    · 剧本本身合法（6 场、2 场战斗、10 条线索、3 个场景效果）；
    · 整段能跑完，世界地点/地图/NPC 都播种；
    · H2b 场景效果真的挂上、换场清掉；轮次效果不带到下一场；
    · H3 战利品（带词条）能在跑完后正常造/穿/结算。
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

from engine.adventure import AdventureRunner, load_adventure  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.web_actions import apply_action  # noqa: E402

M1 = os.path.join(ROOT, "adventures", "雾渡的第三声钟.json")
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


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def make_loop(level: int = 4):
    st = StateTree.from_template(
        {n: new_character(n, level=level, job="战士",
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


print("=== A. 剧本结构：M1 门槛与引擎边界 ===")
adv = load_adventure(M1)
errs, warns = adv.validate()
ok(errs == [], "A1 剧本合法", errs)
ok(len(adv.scenes) == 6, "A2 6 场", len(adv.scenes))
combat_scenes = [s.id for s in adv.scenes if s.combat]
ok(len(combat_scenes) == 2, "A3 ⭐ 2 场战斗", combat_scenes)
clues = [c for s in adv.scenes for c in s.clues]
ok(len(clues) >= 10, "A4 至少 10 条线索", len(clues))
ok(all(c.skill in ("perception", "investigation") for c in clues),
   "A5 ⭐ 线索只用引擎支持的两种检定", sorted({c.skill for c in clues}))
effects = [e for s in adv.scenes for e in s.effects]
ok(len(effects) == 3, "A6 3 个场景效果（H2b）", len(effects))
ok(adv.meta.get("时长", ""), "A7 写了时长", adv.meta.get("时长"))
ok(not warns, "A8 没有校验提醒", warns)

print("\n=== B. 整段跑完（假模型，0 元） ===")
st, loop = make_loop()
runner = AdventureRunner(loop, adv)
logs = runner.run_all(combat_rounds=4)
ok(runner.scenes_done == [s.id for s in adv.scenes],
   "B1 ⭐ 6 场全部跑完", runner.scenes_done)
ok(loop.world.current_location == "雾渡口", "B2 最终地点回到渡口",
   loop.world.current_location)
ok(any(lg.phase == "combat" for lg in logs), "B3 战斗真的打了")
ok("未发现私有内容泄漏" in loop.report(), "B4 跑完后泄漏检查通过")
ok(loop.world.npc("盲婆") is not None, "B5 NPC 播种进世界树")
ok(loop.world.location("钟楼顶") is not None, "B6 地图地点已播种")

print("\n=== C. H2b 效果：挂上、换场清、轮次不带出 ===")
st2, loop2 = make_loop()
runner2 = AdventureRunner(loop2, adv)
runner2.run_scene(adv.scene("s1"), combat_rounds=2)
runner2.run_scene(adv.scene("s2"), combat_rounds=2)
fog = [m for m in mods_of(loop2, "艾尔玛") if m.get("name") == "浓雾"]
ok(bool(fog) and fog[0].get("value") == -2
   and fog[0].get("target") == "perception",
   "C1 ⭐ 浓雾：队伍察觉 -2", mods_of(loop2, "艾尔玛"))
runner2.run_scene(adv.scene("s3"), combat_rounds=2)
ok(not [m for m in mods_of(loop2, "艾尔玛") if m.get("name") == "浓雾"],
   "C2 ⭐ 换到闸房后浓雾被清掉")
runner2.run_scene(adv.scene("s4"), combat_rounds=2)
ok(not [m for m in mods_of(loop2, "艾尔玛")
        if m.get("name") == "湿滑闸板"],
   "C3 ⭐ 离开闸房后，轮次效果不带到下一场")
runner2.run_scene(adv.scene("s5"), combat_rounds=2)
bell = [m for m in mods_of(loop2, "罗恩") if m.get("name") == "钟声震耳"]
ok(bool(bell) and bell[0].get("value") == -1, "C4 ⭐ 钟声震耳：攻击 -1", bell)
runner2.run_scene(adv.scene("s6"), combat_rounds=2)
ok(not mods_of(loop2, "罗恩"), "C5 收尾场所有临时修正清空", mods_of(loop2, "罗恩"))

print("\n=== D. H3 战利品：跑完能造/穿/结算 ===")
r = apply_action(st, loop, "艾尔玛", {
    "kind": "dm_create_item", "name": "渡口守夜灯", "category": "饰品",
    "词条": ["vigor_minor"],
    "描述": "铜壳小灯，灯芯是盲婆缠钟绳剩下的麻线。",
    "机制": "每场战斗开始时获得 3 点临时生命。"})
ok(r.get("ok") is True, "D1 造出战利品", r.get("message"))
apply_action(st, loop, "艾尔玛", {"kind": "equip_item", "name": "渡口守夜灯"})
u = next(x for x in loop.field if x.name == "艾尔玛")
ok(any(e.get("词条") == "vigor_minor" for e in u.item_effects),
   "D2 ⭐ 战利品词条进了单位", [e.get("名称") for e in u.item_effects])

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")