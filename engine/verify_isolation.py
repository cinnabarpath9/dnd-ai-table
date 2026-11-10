# coding=utf-8
"""独立防全知验证 —— **不看装配器的自述，只看真实发出的提示词**。

对应决策记录 §5.21（U11）。纯本地、零模型调用。

和 `lint_leaks()` 的区别：
    · `lint_leaks` 查**台账内部**（公开条目有没有抄私有条目）；
    · 本脚本查**装配出口**：把一次真实跑团里**每一次调用的提示词**抓下来，
      逐个检查"某个秘密出现在谁的包里"。

为什么要独立一份（狼人杀项目的教训）：
    `Assembler` 自己会报 `included/excluded`，但那是**它说它做了什么**。
    这里换成"旁证"：只要秘密真出现在不该出现的提示词里，就红。
    同时做**反向覆盖检查** —— 该看到的人没看到也算问题，
    否则"把所有人的包都清空"会假装通过。

跑法：
    python -m engine.verify_isolation        # 打印检查明细；有问题 exit=1
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random  # noqa: E402
from engine.memory import MemoryStore, parse_game_time  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import DM, Ledger, StateTree, new_character  # noqa: E402
from engine.state.visibility import PLANNER, lint_leaks  # noqa: E402
from engine.state.world import World, new_world  # noqa: E402

__all__ = [
    "PromptCapture", "SecretSpec", "SECRETS", "CapturingClient",
    "build_scenario", "collect_prompts", "viewer_of",
    "verify_prompts", "verify_block_order", "verify_ledger",
    "verify_lint_effectiveness", "run_all_checks", "main",
]

AB = {"力量": 16, "敏捷": 14, "体质": 15,
      "智力": 10, "感知": 12, "魅力": 10}


@dataclass(frozen=True)
class PromptCapture:
    """一次真实调用抓下来的提示词。"""
    viewer: str          # dm / 角色名
    role: str            # dm / player / planner
    task: str
    prompt: str


@dataclass(frozen=True)
class SecretSpec:
    """一个"只该给某些人看"的内容片段。

    `allowed` 为空集 = **谁都不该看到**（`engine` 档的暗骰明细）。
    """
    marker: str
    label: str
    allowed: frozenset


SECRETS: tuple[SecretSpec, ...] = (
    SecretSpec("门口有脚印", "公开线索", frozenset({"dm", "艾尔玛", "罗恩", PLANNER})),
    #: 玩家视角的世界块也要有"该看到的"证据：否则把世界块清空仍全绿。
    SecretSpec("旧哨塔", "玩家已发现的地点",
               frozenset({"dm", "艾尔玛", "罗恩", PLANNER})),
    SecretSpec("灰狼帮", "玩家已知的派系",
               frozenset({"dm", "艾尔玛", "罗恩", PLANNER})),
    SecretSpec("找回货物", "公开任务",
               frozenset({"dm", "艾尔玛", "罗恩", PLANNER})),
    SecretSpec("石板下有钥匙", "艾尔玛的私有对话", frozenset({"dm", "艾尔玛"})),
    SecretSpec("神秘学", "罗恩角色卡上的独占技能", frozenset({"罗恩"})),
    SecretSpec("驯兽", "艾尔玛角色卡上的独占技能", frozenset({"艾尔玛"})),
    SecretSpec("他在找失踪的妹妹", "角色卡私档（当前口径：谁都不发）",
               frozenset()),
    SecretSpec("队长是内应", "DM 密记", frozenset({"dm"})),
    SecretSpec("暗骰明细", "引擎档暗骰（连 DM 都不给）", frozenset()),
    SecretSpec("塔底祭坛", "地点秘密", frozenset({"dm"})),
    SecretSpec("头目是变形怪", "派系秘密", frozenset({"dm"})),
    SecretSpec("银月议会", "未听说的派系", frozenset({"dm"})),
    SecretSpec("王都", "未发现的地点", frozenset({"dm"})),
    SecretSpec("失踪者被献祭", "未公开任务的秘密", frozenset({"dm"})),
    #: P5-G：记忆块也是最容易漏过滤的一处 —— 它不在台账里，另外一条通路。
    SecretSpec("塔门上刻着艾尔玛", "玩家亲历过的公开记忆",
               frozenset({"dm", "艾尔玛", "罗恩"})),
    SecretSpec("欠条藏在磨盘底下", "DM 专属记忆（幕后账）", frozenset({"dm"})),
)


class CapturingClient(FakeModel):
    """记录每次请求，行为照旧（零花费）。"""

    def __init__(self) -> None:
        super().__init__(name="capture-isolation")
        self.reqs = []

    def _complete(self, req):
        self.reqs.append(req)
        return super()._complete(req)


def viewer_of(req) -> str:
    """把一次调用映射到"谁的视角"：DM / 敌方规划 = 各自独立视角，玩家 = 角色名。"""
    if req.role == "dm":
        return DM
    if req.role == "planner":
        return PLANNER
    return str(req.actor or req.role)


# --------------------------------------------------------------------------- #
# 场景：把所有"该藏起来"的东西都放进去
# --------------------------------------------------------------------------- #

def build_scenario() -> tuple[StateTree, Ledger, GameLoop, CapturingClient]:
    state = StateTree.from_template({
        "艾尔玛": new_character("艾尔玛", level=5, job="战士", abilities=AB,
                                hp_max=40, ac=16),
        "罗恩": new_character("罗恩", level=5, job="法师", abilities=AB,
                              hp_max=24, ac=12),
    }, current="艾尔玛", label="防全知验证",
        world=new_world(date="第 3 日", period="傍晚", weather="阴"))
    state.commit([
        {"op": "replace", "path": "/角色列表/罗恩/档案与个性",
         "value": "罗恩的私事：他在找失踪的妹妹。"},
        {"op": "replace", "path": "/角色列表/艾尔玛/档案与个性",
         "value": "艾尔玛的私事：她在找失踪的师父。"},
        {"op": "replace", "path": "/角色列表/罗恩/熟练配置/技能",
         "value": {"神秘学": 7}},
        {"op": "replace", "path": "/角色列表/艾尔玛/熟练配置/技能",
         "value": {"驯兽": 5}},
        {"op": "insert", "path": "/角色列表/艾尔玛/物品/长剑",
         "value": {"类别": "武器", "已装备": True}},
        {"op": "insert", "path": "/角色列表/罗恩/物品/匕首",
         "value": {"类别": "武器", "已装备": True}},
    ], actor="填表", reason="开局私档与装备")

    w = World(state).ensure()
    w.discover("旧哨塔", description="塔门半开，里面很暗。", region="荒原",
               secret="塔底祭坛。")
    w.add_faction("灰狼帮", attitude="敌对", description="盘踞荒原的强盗。",
                  known=True, secret="头目是变形怪。")
    w.add_faction("银月议会", attitude="中立", description="遥远的施法者组织。",
                  known=False, secret="他们在找徽记。")
    w.register_location("王都", description="远处的都城。", region="平原",
                        secret="国王病重。")
    w.open_quest("找回货物", description="商队被劫。", objective="找回三箱丝绸",
                 public=True, round=2)
    w.open_quest("调查失踪者", description="有人夜里失踪。",
                 objective="查明原因", public=False,
                 secret="失踪者被献祭。", round=3)

    ledger = Ledger()
    ledger.public("公告", "公开线索：门口有脚印。", section="公告", round=3)
    ledger.private("对话", "私语：石板下有钥匙。", "艾尔玛",
                   section="对话", round=3)
    ledger.secret("公告", "DM 密记：队长是内应。", section="公告", round=3)
    ledger.engine_only("检定", "暗骰明细：察觉 d20=3。", section="检定", round=3)

    client = CapturingClient()
    #: P5-G：记忆块走的是**另一条**出口（不是台账），所以也要放秘密进来验。
    memory = MemoryStore(label="防全知·记忆")
    memory.record("上次来哨塔：塔门上刻着艾尔玛的名字。",
                  start=parse_game_time("1492-09-16", "傍晚"),
                  people=("艾尔玛", "罗恩"), places=("旧哨塔",),
                  visible_to="public", key="verify-pub")
    memory.record("DM 记忆：磨坊主把欠条藏在磨盘底下，还没跟你们提过。",
                  start=parse_game_time("1492-09-16", "上午"),
                  people=("艾尔玛", "罗恩"), places=("旧哨塔",),
                  visible_to="dm", key="verify-dm")
    loop = GameLoop(state, ledger, client, rng=Random(20261103),
                    label="防全知验证", memory=memory)
    loop.set_field([
        make_hero_unit(state, "艾尔玛", uid="艾尔玛"),
        make_hero_unit(state, "罗恩", uid="罗恩"),
    ])
    return state, ledger, loop, client


def collect_prompts(loop: GameLoop, client: CapturingClient) -> list[PromptCapture]:
    """真跑一轮探索 + 一轮战斗，把所有调用抓下来。"""
    loop.run_explore_round(pc_name="艾尔玛", situation="你们站在旧哨塔前。")
    loop.run_explore_round(pc_name="罗恩", situation="罗恩去查墙根的刻痕。")
    loop.start_combat(enemies=[("Bandit", 2)], distance=20)
    loop.run_combat_round(pc_name="艾尔玛")
    out = []
    for req in client.reqs:
        out.append(PromptCapture(viewer=viewer_of(req), role=req.role,
                                 task=req.task, prompt=req.prompt))
    return out


# --------------------------------------------------------------------------- #
# 检查
# --------------------------------------------------------------------------- #

def verify_prompts(captures: list[PromptCapture],
                   secrets: tuple[SecretSpec, ...] = SECRETS) -> list[str]:
    """返回问题清单（空 = 通过）。**双向检查：不该看到 + 该看到。**"""
    problems: list[str] = []
    if not captures:
        return ["没有抓到任何提示词 —— 验证本身失效（不是通过）"]

    for cap in captures:
        if not cap.prompt:
            problems.append(f"{cap.viewer} 的 {cap.task} 提示词是空的")

    for spec in secrets:
        seen: dict[str, bool] = {}
        for cap in captures:
            hit = spec.marker in cap.prompt
            seen[cap.viewer] = seen.get(cap.viewer, False) or hit
            if hit and cap.viewer not in spec.allowed:
                problems.append(
                    f"泄漏：{spec.label}「{spec.marker}」出现在 {cap.viewer} 的"
                    f"{cap.task} 包里（只允许 {sorted(spec.allowed) or '无人'}）")
        # 反向覆盖：该看到的人必须真看到（否则"清空所有人的包"也会假装通过）
        for who in sorted(spec.allowed):
            got = seen.get(who)
            if got is None:
                problems.append(f"覆盖缺口：{who} 这一次没有任何捕获包，"
                                f"无法确认它能看到 {spec.label}")
            elif not got:
                problems.append(f"覆盖缺口：{who} 该看到 {spec.label}"
                                f"「{spec.marker}」却没看到")
    return problems


def verify_block_order(captures: list[PromptCapture]) -> list[str]:
    """独立核对"静态在前、动态在后"：看真实文本里的先后位置。"""
    problems: list[str] = []
    for cap in captures:
        markers = []
        if cap.viewer == DM:
            markers.append(("说明", "地下城主"))
        elif cap.viewer == PLANNER:
            # 敌方规划是"战斗子集"：只该有战术提示 + 公共战场事实
            markers.append(("说明", "你在为一场 D&D 战斗规划"))
        else:
            markers.append(("说明", "你是一名 D&D 冒险者"))
        markers += [("规则", "规则摘录"), ("台账", "门口有脚印"),
                    ("可选行动", "可用槽位")]
        present = [(name, cap.prompt.index(m)) for name, m in markers
                   if m in cap.prompt]
        positions = [p for _n, p in present]
        if positions != sorted(positions):
            problems.append(f"{cap.viewer} 的 {cap.task} 包块顺序不对：{present}")
        if len(present) < 2:
            problems.append(f"{cap.viewer} 的 {cap.task} 包只有 {len(present)} 个"
                            f"可核对块 —— 验证覆盖不足")
    return problems


def verify_ledger(ledger: Ledger) -> list[str]:
    """台账内部泄漏检查（`lint_leaks` 必须干净）。"""
    return [f"台账内部泄漏：{x}" for x in lint_leaks(ledger)]


def verify_lint_effectiveness() -> list[str]:
    """**守卫本身要能被证明有效**：故意造一次泄漏，lint 必须抓到。"""
    bad = Ledger()
    bad.private("对话", "这是只有甲知道的秘密内容，长度足够触发检查。", "甲")
    bad.public("公告", "有人把「这是只有甲知道的秘密内容」说出去了。")
    if not lint_leaks(bad):
        return ["lint_leaks 对一次故意的公开泄漏无反应 —— 守卫失效"]
    return []


def run_all_checks() -> tuple[list[str], list[PromptCapture]]:
    _state, ledger, loop, client = build_scenario()
    captures = collect_prompts(loop, client)
    problems: list[str] = []
    problems += verify_ledger(ledger)
    problems += verify_prompts(captures)
    problems += verify_block_order(captures)
    problems += verify_lint_effectiveness()
    return problems, captures


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    print("=" * 84)
    print("独立防全知验证（只看真实提示词，零模型调用）")
    print("=" * 84)
    problems, captures = run_all_checks()

    by_viewer: dict[str, int] = {}
    for c in captures:
        by_viewer[c.viewer] = by_viewer.get(c.viewer, 0) + 1
    print(f"\n抓到 {len(captures)} 次调用："
          + "、".join(f"{k} {v} 次" for k, v in sorted(by_viewer.items())))
    print(f"检查的秘密/私有片段：{len(SECRETS)} 个")
    for spec in SECRETS:
        who = "、".join(sorted(spec.allowed)) if spec.allowed else "无人（引擎档）"
        print(f"  · {spec.label}「{spec.marker}」→ 允许：{who}")

    print("\n结果")
    print("-" * 84)
    if problems:
        for p in problems:
            print("  ❌ " + p)
        print(f"\n共 {len(problems)} 个问题")
    else:
        print("  ✅ 没有泄漏、没有覆盖缺口、块顺序正确、lint 守卫有效")
    print("=" * 84)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())