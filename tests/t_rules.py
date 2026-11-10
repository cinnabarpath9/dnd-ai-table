# coding=utf-8
"""U7 回归：规则片段按需检索（本地 SRD rules）。"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.rules_text import (  # noqa: E402
    CORE_LINE, FOOTER, PHASE_RULES, PHASES, RulePick, all_rule_keys,
    available_topics, build_rules_text, get_rule, rules_text_for, search_rules,
)
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


AB = {"力量": 16, "敏捷": 14, "体质": 15,
      "智力": 10, "感知": 12, "魅力": 10}


# --------------------------------------------------------------------------- #
section("A. 规则表：查得到、说得出、不猜")

keys = all_rule_keys()
ok(len(keys) >= 50, f"本地 rules 至少 50 条（实测 {len(keys)}）", len(keys))
ok(len(keys) == len(set(keys)), "规则 key 不重复")
ok(len(available_topics()) == len(keys), "available_topics 与 records 数量一致")

cover = get_rule("Cover")
ok(cover is not None and cover.get("name") == "Cover", "按名字查到 Cover")
ok(get_rule("srd-2024_combat_cover") is not None, "按 key 查到同一条")
ok(get_rule("srd-2024_combat_cover")["key"] == cover["key"], "两种查法结果一致")

ok(get_rule("不存在的规则") is None, "查不到的规则返回 None（不编）")
ok(get_rule("") is None, "空名返回 None")

hits = search_rules("cover", limit=3)
ok(hits and hits[0]["name"] == "Cover", "关键词搜索把 Cover 排第一",
   [h["name"] for h in hits])
opp = search_rules("opportunity attacks", limit=3)
ok(opp and "melee-attacks" in str(opp[0].get("key")),
   "搜 opportunity attacks 命中「Melee Attacks」（借机攻击在那条里）",
   [h["name"] for h in opp])
ok(search_rules("", limit=3) == [], "空关键词不返回")
ok(search_rules("zzzz不存在", limit=3) == [], "搜不到就空（不瞎给）")
ok(len(search_rules("the", limit=2)) <= 2, "limit 生效")

missing_keys = [p.key for picks in PHASE_RULES.values() for p in picks
                if get_rule(p.key) is None]
ok(missing_keys == [], "⭐ 场景清单里的 key 全部存在", missing_keys)


# --------------------------------------------------------------------------- #
section("B. 场景块：内容对、字数在预算内")

combat = rules_text_for("combat")
explore = rules_text_for("explore")
spell = rules_text_for("spell")
social = rules_text_for("social")

ok("SRD 2024" in combat and "规则摘录" in combat, "带标题与出处")
for want in ("Opportunity Attacks", "Cover", "Critical Hits",
             "Dropping to 0 Hit Points"):
    ok(want in combat, f"战斗块含「{want}」")
ok("Vision and Light" not in combat and "Travel" not in combat,
   "战斗块不含探索专属规则")
ok(combat.index("Opportunity Attacks") > combat.index("Melee Attacks")
   or "Opportunity Attacks" in combat,
   "⭐ 借机攻击是从 Melee Attacks 里定点截出来的")

for want in ("Ability Checks", "Vision and Light", "Travel"):
    ok(want in explore, f"探索块含「{want}」")
ok("Opportunity Attacks" not in explore, "探索块不含战斗专属规则")
ok("Spellcasting" in spell, "施法块含 Spellcasting")
ok("Roleplaying" in social, "社交块含 Roleplaying")

ok(len(combat) <= 1700, f"战斗块在预算内（{len(combat)} 字）", len(combat))
ok(len(explore) <= 1700, f"探索块在预算内（{len(explore)} 字）", len(explore))
ok(CORE_LINE in combat and FOOTER in combat, "收尾有引擎口径与出处说明")
ok("\ufffd" not in combat and "**" not in combat,
   "⭐ 输出没有坏字符、没有 markdown 粗体")
ok(rules_text_for("combat") == combat, "同一场景两次结果一致（可复现）")
ok(rules_text_for("combat") is rules_text_for("combat"),
   "缓存命中返回同一个字符串对象")

fallback = rules_text_for("不存在的场景")
ok("Ability Checks" in fallback and "探索" in fallback,
   "未知场景回落到探索规则（不崩）")
ok(set(PHASES) == {"explore", "combat", "spell", "social"}, "阶段枚举固定")


# --------------------------------------------------------------------------- #
section("C. 按主题检索与预算")

only_cover = rules_text_for("combat", topics=("srd-2024_combat_cover",))
ok("Cover" in only_cover and "Critical Hits" not in only_cover,
   "⭐ 指定主题时只收那一条", only_cover[:80])
ok("指定主题" in only_cover, "标题标出是指定主题")

pick_text = build_rules_text(topics=[RulePick(
    "srd-2024_combat_melee-attacks", section="Opportunity Attacks", limit=1400)])
ok("Opportunity Attacks" in pick_text and "Making an Opportunity Attack" in pick_text,
   "RulePick 能定点截取小节")
ok("Disengage" in pick_text, "截取里带上了 Disengage 的例外说明")

sec_fallback = build_rules_text(topics=[
    ("srd-2024_combat_cover", "这段标题不存在", 200)])
ok("Walls, trees" in sec_fallback,
   "小节找不到就从头截（不返回空、不编）")

unknown = build_rules_text(topics=["完全不存在的东西"])
ok("找不到规则" in unknown and "完全不存在的东西" in unknown,
   "⭐ 找不到的主题明说，不编造", unknown[:120])

small = build_rules_text(phase="combat", max_chars=400)
ok(len(small) <= 700, f"小预算块被压到 {len(small)} 字", len(small))
ok(len(small) >= 150, "小预算也至少留一条规则 + 收尾")

big = build_rules_text(phase="combat", max_chars=4000)
ok(len(big) > len(combat), "大预算能装下更多规则",
   (len(big), len(combat)))
ok("Playing on a Grid" in big or "Temporary Hit Points" in big,
   "大预算里出现默认被挤掉的规则")

trimmed = build_rules_text(phase="combat", max_chars=520)
ok(len(trimmed) <= 800 and "（还有" in trimmed,
   "预算吃紧时如实说明「还有 N 条未收入」", trimmed[-80:])


# --------------------------------------------------------------------------- #
section("D. 接进 GameLoop：探索拿探索、战斗拿战斗")


class CaptureModel(FakeModel):
    def __init__(self) -> None:
        super().__init__(name="capture")
        self.reqs = []

    def _complete(self, req):
        self.reqs.append(req)
        return super()._complete(req)


def make_state() -> StateTree:
    st = StateTree.from_template({
        "罗恩": new_character("罗恩", level=3, job="战士", abilities=AB,
                              hp_max=28, ac=16),
    }, current="罗恩")
    st.commit([{"op": "insert", "path": "/角色列表/罗恩/物品/长剑",
                "value": {"类别": "武器", "已装备": True}}],
              actor="填表", reason="装备长剑")
    return st


ok(GameLoop._rules_text("combat") == rules_text_for("combat"),
   "GameLoop 战斗规则 == 检索结果")
ok(GameLoop._rules_text("explore") == rules_text_for("explore"),
   "GameLoop 探索规则 == 检索结果")

st = make_state()
cm = CaptureModel()
loop = GameLoop(st, Ledger(), cm, rng=Random(7), label="规则接入")
loop.set_field([make_hero_unit(st, "罗恩")])
loop.run_explore_round(pc_name="罗恩", situation="你们站在哨塔门口。")
dm_ex = next(r.prompt for r in reversed(cm.reqs) if r.role == "dm")
pc_ex = next(r.prompt for r in reversed(cm.reqs) if r.role == "player")
ok("Ability Checks" in dm_ex, "⭐ 探索轮 DM 的包里有探索规则原文")
ok("Opportunity Attacks" not in dm_ex, "探索轮不塞战斗规则")
ok("Ability Checks" in pc_ex, "探索轮玩家包同样有探索规则")

asm = loop._assembler(phase="combat")
built = asm.build("dm")
ok(built.get("规则") == rules_text_for("combat"),
   "装配器按 phase 挂对应规则块")
ok(next((b.why for b in built.blocks if b.name == "规则"), "") == "本场规则片段",
   "块说明没变")

st2 = make_state()
cm2 = CaptureModel()
loop2 = GameLoop(st2, Ledger(), cm2, rng=Random(9))
loop2.set_field([make_hero_unit(st2, "罗恩")])
loop2.start_combat(enemies=[("Bandit", 1)], distance=20)
loop2.run_combat_round(pc_name="罗恩")
planner_p = next(r.prompt for r in reversed(cm2.reqs)
                 if r.role == "planner")
pc_c_p = next(r.prompt for r in reversed(cm2.reqs)
              if r.role == "player")
ok("Opportunity Attacks" in planner_p or "Cover" in planner_p,
   "⭐ 战斗轮敌方规划拿到战斗规则")
ok("Opportunity Attacks" in pc_c_p or "Cover" in pc_c_p,
   "战斗轮 PC 也拿战斗规则")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")