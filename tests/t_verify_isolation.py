# coding=utf-8
"""U11 回归：独立防全知验证脚本。"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.state import Ledger  # noqa: E402
from engine.state.visibility import PLANNER, lint_leaks  # noqa: E402
from engine.verify_isolation import (  # noqa: E402
    DM, SECRETS, CapturingClient, PromptCapture, build_scenario, collect_prompts,
    main, run_all_checks, verify_block_order, verify_ledger,
    verify_lint_effectiveness, verify_prompts, viewer_of,
)

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


def has_problem(problems: list[str], needle: str) -> bool:
    return any(needle in p for p in problems)


# --------------------------------------------------------------------------- #
section("A. 正例：场景本身必须干净")

problems, captures = run_all_checks()
ok(problems == [], "A1 ⭐ 完整场景零问题", problems[:3])
ok(len(captures) >= 6, f"抓到了 {len(captures)} 次调用", len(captures))
roles = {c.role for c in captures}
ok({"dm", "player", "planner"} <= roles, "A2 三类调用都抓到", sorted(roles))
viewers = {c.viewer for c in captures}
ok({DM, PLANNER, "艾尔玛", "罗恩"} <= viewers, "A3 四个视角都有", sorted(viewers))
tasks = {c.task for c in captures}
ok({"explore_action", "combat_turn", "plan_all_enemies"} <= tasks,
   "A4 探索/战斗/规划任务都在", sorted(tasks))

plan = next(c for c in captures if c.viewer == PLANNER)
ok("队长是内应" not in plan.prompt and "塔底祭坛" not in plan.prompt,
   "A4b ⭐ planner 包不含 DM 秘密/地点秘密")
ok("欠条藏在磨盘底下" not in plan.prompt, "A4c planner 也拿不到 DM 记忆")
ok("门口有脚印" in plan.prompt, "A4d planner 仍能看到公共战场线索")
ok(("Bandit" in plan.prompt) or ("强盗" in plan.prompt),
   "A4e planner 有自己的敌方单位卡")

p2, c2 = run_all_checks()
ok(p2 == problems and len(c2) == len(captures), "A5 两次跑结果一致（可复现）")

ok(viewer_of(type("R", (), {"role": "planner", "actor": "enemy_planner"})())
   == PLANNER, "A6 planner 是独立视角（只吃 public），不再归到 DM")
ok(viewer_of(type("R", (), {"role": "player", "actor": "艾尔玛"})()) == "艾尔玛",
   "A7 player 归到自己")


# --------------------------------------------------------------------------- #
section("B. 反例：故意造泄漏，验证器必须抓到")

bad = list(captures) + [PromptCapture(viewer="罗恩", role="player",
                                      task="fake_turn",
                                      prompt="DM 密记：队长是内应。")]
probs = verify_prompts(bad)
ok(has_problem(probs, "队长是内应") and has_problem(probs, "泄漏"),
   "B1 ⭐ DM 密记出现在罗恩包里 → 报泄漏", probs[:2])

bad2 = list(captures) + [PromptCapture(
    viewer="罗恩", role="player", task="fake_turn",
    prompt="暗骰明细：察觉 d20=3。")]
ok(has_problem(verify_prompts(bad2), "暗骰明细"),
   "B2 ⭐ 引擎档暗骰出现在任何玩家包里 → 报泄漏")

bad3 = list(captures) + [PromptCapture(
    viewer="罗恩", role="player", task="fake_turn",
    prompt="塔底祭坛。")]
ok(has_problem(verify_prompts(bad3), "塔底祭坛"),
   "B3 ⭐ 地点秘密出现在玩家包 → 报泄漏")

bad4 = [c for c in captures if c.viewer != "艾尔玛"] or [
    PromptCapture(viewer="罗恩", role="player", task="x", prompt="公开")]
probs4 = verify_prompts(bad4)
ok(has_problem(probs4, "覆盖缺口") and has_problem(probs4, "艾尔玛"),
   "B4 ⭐ 该看到的人没包 → 报覆盖缺口（不能靠清空通过）", probs4[:2])

ok(has_problem(verify_prompts([]), "失效"),
   "B5 空捕获 → 直接判验证失效")

ok(not has_problem(verify_prompts(bad), "引擎档暗骰"),
   "B6 只有 DM 密记那一条被报，不误伤别的")


# --------------------------------------------------------------------------- #
section("C. 真实提示词里的目标检查（不靠通用循环）")

state, ledger, loop, client = build_scenario()
caps = collect_prompts(loop, client)

by_view = {}
for c in caps:
    by_view.setdefault(c.viewer, []).append(c)

def joined(viewer: str) -> str:
    return "\n".join(c.prompt for c in by_view.get(viewer, []))

ok("石板下有钥匙" in joined(DM), "C1 DM 看得到艾尔玛的私有对话")
ok("石板下有钥匙" in joined("艾尔玛"), "C2 艾尔玛看得到自己的私有对话")
ok("石板下有钥匙" not in joined("罗恩"), "C3 ⭐ 罗恩看不到艾尔玛的私有对话")
ok("神秘学" in joined("罗恩"), "C4 罗恩的卡里有自己的独占技能")
ok("神秘学" not in joined("艾尔玛"), "C5 ⭐ 艾尔玛包里没有罗恩的技能")
ok("驯兽" in joined("艾尔玛") and "驯兽" not in joined("罗恩"),
   "C6 反方向也不串")
ok("暗骰明细" not in joined(DM) and "暗骰明细" not in joined("罗恩")
   and "暗骰明细" not in joined("艾尔玛"),
   "C7 ⭐ 引擎档暗骰不进任何提示词（连 DM 都没有）")

for secret in ("塔底祭坛", "头目是变形怪", "银月议会", "王都", "失踪者被献祭"):
    ok(secret not in joined("罗恩") and secret not in joined("艾尔玛"),
       f"C8 ⭐ 玩家包里没有世界秘密「{secret}」")
ok("塔底祭坛" in joined(DM) and "银月议会" in joined(DM),
   "C9 DM 的世界块里这些都有")

ok("他在找失踪的妹妹" not in "\n".join(c.prompt for c in caps),
   "C10 ⭐ 角色卡私档（档案与个性）当前不进任何提示词")


# --------------------------------------------------------------------------- #
section("D. 块顺序与 lint 守卫")

ok(verify_block_order(caps) == [], "D1 ⭐ 真实包的块顺序正确（说明→规则→台账→可选行动）")

reordered = [PromptCapture(viewer="罗恩", role="player", task="fake",
                           prompt="可用槽位 可选行动\n规则摘录\n门口有脚印\n"
                                  "你是一名 D&D 冒险者")]
ok(has_problem(verify_block_order(reordered), "块顺序不对"),
   "D2 ⭐ 顺序被打乱 → 报错", verify_block_order(reordered))

thin = [PromptCapture(viewer="罗恩", role="player", task="fake", prompt="一句话")]
ok(has_problem(verify_block_order(thin), "覆盖不足"),
   "D3 只有一句话的包 → 报验证覆盖不足")

ok(verify_ledger(ledger) == [], "D4 场景台账内部无泄漏")
poisoned = Ledger()
poisoned.private("对话", "这是只有甲知道的秘密内容，长度足够触发检查。", "甲")
poisoned.public("公告", "有人把「这是只有甲知道的秘密内容」说出去了。")
ok(verify_ledger(poisoned), "D5 ⭐ 被污染的台账会被 lint 抓到",
   verify_ledger(poisoned)[:1])
ok(verify_lint_effectiveness() == [], "D6 lint 守卫自证有效")

# 秘密清单本身要覆盖"该看到 / 不该看到"两侧
specs = {s.marker: s for s in SECRETS}
ok(specs["暗骰明细"].allowed == frozenset(), "D7 引擎档明确写「谁都不给」")
ok(specs["石板下有钥匙"].allowed == frozenset({"dm", "艾尔玛"}),
   "D8 私有对话明确写「本人 + DM」")


# --------------------------------------------------------------------------- #
section("E. CLI")

code = main()
ok(code == 0, "E1 `python -m engine.verify_isolation` 退出码 0", code)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")