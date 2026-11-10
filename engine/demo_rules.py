# coding=utf-8
"""规则检索 demo：DM 的规则片段从本地 SRD `rules` 按场景取。**零模型调用。**

跑法：
    python -m engine.demo_rules

证明四件事：
  1. 探索轮 / 战斗轮拿到的是不同的 SRD 规则原文摘录；
  2. 借机攻击这种"藏在别条规则里的小节"能定点截出来；
  3. 有字数预算：装不下会如实说，不硬塞、不编；
  4. `GameLoop` 真的用上了检索结果（探索给探索、战斗给战斗）。
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.rules.loop import GameLoop  # noqa: E402
from engine.rules.rules_text import (  # noqa: E402
    build_rules_text, get_rule, rules_text_for, search_rules,
)


def show(title: str, text: str, limit: int = 14) -> None:
    print(f"\n{title}")
    print("-" * 84)
    lines = text.splitlines()
    for line in lines[:limit]:
        print("  " + line)
    if len(lines) > limit:
        print(f"  …（还有 {len(lines) - limit} 行）")
    print(f"  ［{len(text)} 字］")


def main() -> int:
    print("=" * 84)
    print("规则检索 demo（本地 SRD rules，零模型调用）")
    print("=" * 84)

    combat = rules_text_for("combat")
    explore = rules_text_for("explore")
    show("【1】战斗规则组", combat)
    show("【2】探索规则组", explore, limit=8)

    print("\n【3】按主题定点截取：借机攻击（藏在 Melee Attacks 里）")
    print("-" * 84)
    opp = ("srd-2024_combat_melee-attacks", "Opportunity Attacks", 900)
    one = build_rules_text(topics=[opp])
    for line in one.splitlines():
        if "Melee Attacks" in line:
            print("  " + line[:300] + "…")
    print(f"  含「Making an Opportunity Attack」："
          f"{'Making an Opportunity Attack' in one}")

    print("\n【4】关键词检索")
    print("-" * 84)
    for q in ("cover", "opportunity attacks", "healing"):
        hits = search_rules(q, limit=2)
        print(f"  「{q}」→ " + "、".join(h.get("name", "?") for h in hits))

    print("\n【5】预算：装不下就如实说，不硬塞")
    print("-" * 84)
    small = build_rules_text(phase="combat", max_chars=500)
    notes = [l for l in small.splitlines() if l.startswith("（还有")]
    print(f"  500 字预算 → 实际 {len(small)} 字；"
          f"{notes[0] if notes else '（没有跳过项）'}")
    print(f"  全部 56 条规则里，战斗组默认收 "
          f"{len([l for l in combat.splitlines() if l.startswith('·')])} 条；"
          f"其余按预算或场景取舍。")

    print("\n【6】GameLoop 真的用上了")
    print("-" * 84)
    same_combat = GameLoop._rules_text("combat") == combat
    same_explore = GameLoop._rules_text("explore") == explore
    print(f"  战斗：GameLoop 与检索结果一致＝{same_combat}")
    print(f"  探索：GameLoop 与检索结果一致＝{same_explore}")

    checks = {
        "战斗组含借机攻击": "Opportunity Attacks" in combat,
        "战斗组含掩护": "Cover" in combat,
        "战斗组不含探索专属": "Vision and Light" not in combat,
        "探索组含属性检定": "Ability Checks" in explore,
        "探索组不含借机攻击": "Opportunity Attacks" not in explore,
        "定点截取成功": "Making an Opportunity Attack" in one,
        "检索 Cover 命中": bool(search_rules("cover", limit=1)),
        "预算内": len(combat) <= 1750 and len(explore) <= 1750,
        "GameLoop 一致": same_combat and same_explore,
        "原文可查": get_rule("Cover") is not None,
    }
    print("\n【7】检查")
    print("-" * 84)
    for name, good in checks.items():
        print(f"  {'✅' if good else '❌'} {name}")

    ok = all(checks.values())
    print("\n" + "=" * 84)
    print("✅ 规则检索：全部通过" if ok else "❌ 有检查项失败")
    print("=" * 84)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())