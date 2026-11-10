# coding=utf-8
"""U9 离线叙事体检：**能判的硬伤** + 抽样工具 + 接进 DM 回合。

口径（写清楚"测不了什么"）：

    ✅ 空/过短/过长、代码围栏、AI 自我暴露、骰面/内部编号泄漏、
       未发现秘密原文、复读；
    ❌ 写得好不好、画面感、人物像不像 —— 那要真模型 + 人读。
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
sys.path.insert(0, os.path.join(ROOT, "tools"))

from engine.dice import Random  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.clues import SceneClue  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.narrate_lint import check_narration  # noqa: E402
from engine.state import Ledger, StateTree, World, new_character  # noqa: E402
import narrative_sample  # noqa: E402

FAILS: list[str] = []
COUNT = 0
AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 14, "魅力": 10}
GOOD = ("塔门在风里吱呀作响，灰尘从门缝里往下跌。小翠蹲下去看地上的拖痕，"
        "罗恩把火把举高了一点，光顺着楼梯往上爬。")
SECRET = "地板上有一块松动的石板，下面压着一封信。"


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def codes(text: str, **kw) -> list[str]:
    return sorted({i.code for i in check_narration(text, **kw)})


def make(script: list[str] | None = None):
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士", abilities=dict(AB),
                             hp_max=24, ac=15)}, current="甲")
    u = make_hero_unit(st, "甲")
    loop = GameLoop(st, Ledger(), FakeModel(script=script), rng=Random(7))
    loop.set_field([u])
    w = World(st).ensure()
    w.discover("哨塔")
    w.travel("哨塔")
    return st, loop


# --------------------------------------------------------------------------- #
section("A. 体检规则：每条都能单独红")

ok(check_narration(GOOD) == [], "A1 正常叙事：零问题")
ok(codes("") == ["empty"], "A2 空文本 → empty")
ok("too_short" in codes("门开了。"), "A3 太短 → too_short")
ok("too_long" in codes(GOOD, max_chars=10), "A4 超上限 → too_long")
ok("markdown_fence" in codes(GOOD + "\n```json\n{}"), "A5 代码围栏 → markdown_fence")
ok("meta_leak" in codes("作为 AI，我不能继续。"), "A6 AI 自我暴露 → meta_leak")
ok("dice_leak" in codes("你掷出 d20=17，对抗 DC 14。"), "A7 骰面/DC → dice_leak")
ok("id_leak" in codes("你选了 [a2]，触发 AM0003。"), "A8 内部编号 → id_leak")
ok("secret_leak" in codes(SECRET, forbidden=[SECRET]),
   "A9 ⭐ 未发现秘密原文 → secret_leak")
ok(codes(SECRET, forbidden=[], min_chars=10) == [],
   "A10 同一段话，秘密已公开就不算泄漏")
ok("repeat" in codes("塔里很安静，只有风从破窗吹进来。" * 2),
   "A11 复读同一句 → repeat")
ok("repeat" not in codes("他推开门。他走了进去。"), "A12 不同句不算复读")

section("B. 离线抽样：手写样本对拍")

rows = narrative_sample.sample()
ok(len(rows) == 10, "B1 样本表有 10 条", len(rows))
bad = [r["id"] for r in rows if not r["ok"]]
ok(not bad, "B2 ⭐ 10 条样本的检出与期望全部一致", bad)
ok(rows[0]["got"] == [] and rows[0]["ok"],
   "B3 正常样本零误报（n01）", rows[0])

section("C. 接进 DM 回合：只记问题，不改正文")

st, loop = make(script=[SECRET])
loop.set_scene_context(scene_id="s1",
                       clues=[SceneClue(id="c1", text=SECRET, dc=10, secret=True)])
log = loop.run_explore_round(pc_name="甲")
hits = [p for p in log.problems if p.startswith("叙事体检")]
ok(any("secret_leak" in p for p in hits),
   "C1 ⭐ DM 抄了未发现秘密 → 台账里记 secret_leak", hits)
ok(any(SECRET in line for line in log.lines),
   "C2 正文仍原样进日志（不静默改写）")

st, loop = make(script=[GOOD])
log = loop.run_explore_round(pc_name="甲")
ok(not [p for p in log.problems if p.startswith("叙事体检")],
   "C3 正常叙事不误报", log.problems)

section("D. 工具入口")

ok(narrative_sample.DEFAULT.endswith(os.path.join("fixtures", "narratives.jsonl")),
   "D1 抽样工具默认读 fixture 表")
ok(narrative_sample.main([]) == 0, "D2 命令行抽样 exit=0")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")