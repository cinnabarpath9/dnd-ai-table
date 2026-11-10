# coding=utf-8
"""P5-F 回归：NPC 五骰人格 + 后台状态机。

守五件事：

    A. **五骰**：五个 D12 都落在表内、可复现、结果不同；人格能往返；
    B. **状态机**：知情三态（已知/未知/误解）、强度夹在 0~100、秘密只在 DM；
    C. **世界树**：登记/更新都走 commit（有审计）、重复登记报错、存档往返；
    D. **防全知**：NPC 人格与状态**只出现在 DM 视角**；
    E. **剧本接线**：跑第一场时登记 NPC，重复跑不重复掷。
"""

from __future__ import annotations

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.adventure import (  # noqa: E402
    AdventureRunner, load_adventure, parse_adventure,
)
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.persona import (  # noqa: E402
    DESIRES, FEARS, FLAWS, MASKS, MOTIVATIONS, NpcState, persona_block,
    roll_persona,
)
from engine.state import DM, Ledger, StateTree, World, new_character  # noqa: E402

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
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")


def make_state(names=("甲", "乙")) -> StateTree:
    return StateTree.from_template(
        {n: new_character(n, level=3, job="战士", abilities=AB,
                          hp_max=24, ac=15) for n in names},
        current=names[0])


# --------------------------------------------------------------------------- #
section("A. 五骰：范围、可复现、往返")

p1 = roll_persona(Random(20261104), name="疤脸", role="强盗头目")
ok(p1.name == "疤脸" and p1.role == "强盗头目", "A1 名字与身份带上了")
ok(p1.motivation in MOTIVATIONS, "A2 动机来自表", p1.motivation)
ok(p1.mask in MASKS, "A3 面具来自表", p1.mask)
ok(any(p1.flaw == f[0] and p1.flaw_trigger == f[1] for f in FLAWS),
   "A4 破绽与触发点成对", (p1.flaw, p1.flaw_trigger))
ok(p1.desire in DESIRES and p1.fear in FEARS, "A5 欲望/恐惧来自表")
ok(p1.conflict and p1.desire in p1.conflict and p1.fear in p1.conflict,
   "A6 矛盾线把欲望与恐惧串起来", p1.conflict)

p1b = roll_persona(Random(20261104), name="疤脸", role="强盗头目")
ok(p1b.to_dict() == p1.to_dict(), "A7 ⭐ 同种子同人格（可复现）")
p2 = roll_persona(Random(7), name="疤脸")
ok(p2.to_dict() != p1.to_dict(), "A8 不同种子给出不同人格")

back = type(p1).from_dict(p1.to_dict())
ok(back.to_dict() == p1.to_dict(), "A9 人格往返一致")
ok(persona_block(p1).startswith("【NPC · 疤脸"),
   "A10 人格卡以 NPC 名开头", persona_block(p1)[:40])


# --------------------------------------------------------------------------- #
section("B. 状态机：知情三态、强度、秘密")

st = NpcState(emotion="警惕", intensity=70, goal_short="守住塔顶",
              boundary="不杀同乡")
st.know("冒险者已经进塔")
st.know("箱子是空的", "误解")
st.know("买主是谁", "未知")
ok(len(st.knowledge) == 3, "B1 三条知情记录")
ok(any(x["state"] == "误解" for x in st.knowledge), "B2 误解是独立状态")
st.know("冒险者已经进塔", "误解")
ok([x for x in st.knowledge if x["fact"] == "冒险者已经进塔"][0]["state"] == "误解",
   "B3 同一事实改状态是覆盖，不重复")
st.know("塔里只有三个强盗")
try:
    st.know("随便", "全知")
    ok(False, "B4 非法知情状态被拒")
except ValueError:
    ok(True, "B4 非法知情状态被拒")
st.secrets.append("箱子底下还有夹层")
text = st.render()
ok("情绪 警惕(70)" in text and "短期目标：守住塔顶" in text, "B5 渲染情绪/目标")
ok("已知：塔里只有三个强盗" in text and "误解：" in text
   and "箱子是空的" in text and "冒险者已经进塔" in text,
   "B6 已知与误解分开渲染", text)
ok("🔒秘密：箱子底下还有夹层" in text, "B7 秘密带锁标记")
d = st.to_dict()
ok(NpcState.from_dict(d).to_dict() == d, "B8 状态往返一致")
ok(NpcState.from_dict({"强度": 999}).intensity == 100, "B9 强度夹到 100")
ok(NpcState.from_dict({"强度": -5}).intensity == 0, "B10 强度夹到 0")


# --------------------------------------------------------------------------- #
section("C. 世界树：登记/更新/审计/存档")

state = make_state()
w = World(state)
w.ensure()
w.register_npc("疤脸", role="强盗头目", persona=p1.to_dict(),
               state=st.to_dict(), reason="测试登记")
ok(w.npc("疤脸") is not None and w.npcs() == ["疤脸"],
   "C1 登记后能查到", w.npcs())
ok(any("测试登记" in e.reason for e in state.audit), "C2 登记留了审计")
try:
    w.register_npc("疤脸")
    ok(False, "C3 重复登记被拒")
except Exception as exc:                       # noqa: BLE001
    ok("已经登记" in str(exc), "C3 重复登记被拒", str(exc))

st2 = NpcState(emotion="暴怒", intensity=90, goal_short="下楼迎敌")
st2.secrets.append("夹层里有买主的账本")
w.update_npc_state("疤脸", st2.to_dict(), reason="测试更新")
ok(w.npc("疤脸")["状态"]["情绪"] == "暴怒", "C4 状态更新写回")
ok(any("测试更新" in e.reason for e in state.audit), "C5 更新也有审计")

clone = StateTree.from_dict(state.to_dict(include_audit=True))
wc = World(clone)
ok(wc.npc("疤脸")["人格"]["核心动机"] == p1.motivation,
   "C6 存档往返后人格还在", wc.npc("疤脸")["人格"]["核心动机"])


# --------------------------------------------------------------------------- #
section("D. 防全知：NPC 只给 DM")

dm_text = w.render(DM)
player_text = w.render("甲")
ok("疤脸" in dm_text and "核心动机" in dm_text and "🔒秘密" in dm_text,
   "D1 ⭐ DM 视角有 NPC 人格/状态/秘密")
ok("疤脸" not in player_text and "核心动机" not in player_text,
   "D2 ⭐ 玩家视角完全没有 NPC 内部信息")


# --------------------------------------------------------------------------- #
section("E. 剧本接线：第一场掷五骰，重复跑不重复")

adv = load_adventure(SAMPLE)
ok(len(adv.npcs) == 3, "E1 示例剧本登记了 3 个 NPC", adv.npcs)
ok({n["name"] for n in adv.npcs} == {"强盗头目·疤脸", "老磨坊主", "买主（未露面）"},
   "E2 NPC 名字对上", [n["name"] for n in adv.npcs])

st3 = make_state(("艾尔玛", "罗恩"))
loop = GameLoop(st3, Ledger(), FakeModel(), rng=Random(20261104))
loop.set_field([make_hero_unit(st3, "艾尔玛"), make_hero_unit(st3, "罗恩")])
runner = AdventureRunner(loop, adv)
runner.run_scene(adv.scene("s1"))
names = loop.world.npcs()
ok(sorted(names) == sorted(n["name"] for n in adv.npcs),
   "E3 ⭐ 跑第一场时把 3 个 NPC 登记进世界", names)
p = loop.world.npc("强盗头目·疤脸")
ok(p and p["人格"].get("核心动机") and p["状态"].get("短期目标"),
   "E4 人格与议程初始目标都写进去了",
   p.get("状态", {}).get("短期目标") if p else None)
runner.run_scene(adv.scene("s2"))
ok(len(loop.world.npcs()) == 3, "E5 ⭐ 第二场不重复登记", loop.world.npcs())
ok(any("登记 NPC" in e.reason for e in st3.audit),
   "E6 登记有审计", [e.reason for e in st3.audit][-2:])

# 手工再调一次 register_npcs 也不应重复掷
before = loop.world.npc("老磨坊主")["人格"]
runner.register_npcs()
ok(loop.world.npc("老磨坊主")["人格"] == before, "E7 register_npcs 是幂等的")

# 剧本校验：重复 NPC 名要报错
raw = json.load(open(SAMPLE, encoding="utf-8"))
raw["npcs"].append({"name": "老磨坊主", "role": "重复登记"})
dup = parse_adventure(raw)
errs, _ = dup.validate()
ok(any("NPC 名字重复" in e for e in errs),
   "E8 重复 NPC 名在剧本校验时报错", errs)


print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")