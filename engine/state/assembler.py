# coding=utf-8
"""上下文装配器 —— 每个单位看到的上下文**只能**由这里产出。

对应决策记录 §5.12.1 的 M3，以及狼人杀项目里最值钱的那条设计：

    那个项目每个座位有**自己独立的一份上下文**（`wolf/context.py` 是唯一出口），
    谁该看到什么由台账 `visible_to` 一条条控制 ——
    不是"所有 AI 看同一份游戏日志"。

**这条纪律在本项目里是硬要求**，理由有三：

    1. **防全知**：玩家模型不该知道 DM 的秘密、也不该知道队友的私有检定；
    2. **省 token**：只发这个人这一刻需要的块（决策记录 §4：战斗里不需要整张角色卡）；
    3. **保住前缀缓存**：块的顺序固定、静态在前动态在后（决策记录 §2.3）。

所以：
    · `build()` 是唯一出口，块顺序即优先级，**只能在这里加块**；
    · 每个块自己声明"谁能看"，由装配器过滤 —— 调用方不许自己拼字符串；
    · 装配结果带 `included` / `excluded` 明细，**可以断言"这个人没拿到那块"**（可测）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..state.tree import StateTree
from ..state.visibility import DM, Ledger

__all__ = [
    "Block", "Assembled", "Assembler", "estimate_tokens",
    "BLOCK_ORDER", "TRIM_ORDER", "build",
]

#: 块的固定顺序（**顺序即优先级，也是缓存友好的关键**）
#: ⚠️ 静态内容在前、动态内容在后 —— 任何"每次都不一样"的东西塞前面都会废掉缓存。
BLOCK_ORDER = (
    "说明",       # 系统提示：你是谁、怎么输出（最稳定）
    "规则",       # 本场规则片段（半稳定）
    "世界",       # 地点/派系/任务/时间（按视角过滤；半稳定）
    "本桌目标",   # 剧本给玩家看的长期目标/流程关键词（半稳定）
    "角色卡",     # 这个单位自己的 stat_data 摘要（半稳定）
    "队友",       # 同队的公开信息（较稳定）
    "台账",       # 已发生的事（按视角过滤，动态）
    "记忆",       # 更早的往事：按内容召回，时间在选完后才分组（动态）
    "可选行动",   # 菜单（动态）
    "任务",       # 这一次要它做什么（最动态）
)

#: 超预算时**按这个顺序裁**（从最先牺牲的排到最后）。
#: 只裁"有它更好、没它也能跑"的块；角色卡/菜单/任务/台账绝不裁 ——
#: 裁掉它们等于让模型瞎猜自己有什么、能做什么，宁可报 `over_budget` 也不静默砍。
TRIM_ORDER = ("记忆", "世界", "队友", "规则")


@dataclass
class Block:
    """一个新上下文块。"""
    name: str                                  # 必须 ∈ BLOCK_ORDER
    text: str
    #: 谁能看：None = 所有人；"dm" = 只有 DM；"pc:名字" = 只有这个人
    visible_to: str | None = None
    #: 说明这个块为什么存在（排查用：某次调用为什么这么长）
    why: str = ""

    def allows(self, viewer: str) -> bool:
        """⚠️ **DM 看得到一切**（2026-10-14 纠正的口径）。

        真实跑团里 DM 就是知道所有角色的秘密与私有检定结果 ——
        不然它没法正确推进流程。所以 `visible_to="pc:某人"` 的块 **DM 也拿得到**。

        "不外泄"不靠"不给 DM 看"，而靠：
          · 提示词里明确写"不许复述角色的私有信息"；
          · `lint_leaks()` 在落台账时兜底。
        """
        if self.visible_to is None:
            return True
        if viewer == DM:
            return True
        if self.visible_to == DM:
            return False          # dm 档只有 DM 能看
        if self.visible_to.startswith("pc:"):
            return viewer == self.visible_to[3:]
        return viewer == self.visible_to


@dataclass
class Assembled:
    """装配结果。`included` / `excluded` 让"谁看到了什么"可断言、可复盘。"""
    viewer: str
    task: str = ""
    blocks: list[Block] = field(default_factory=list)
    included: list[str] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)   # (块名, 原因)
    chars: int = 0
    tokens_est: int = 0
    #: 硬预算下被裁掉的块：`(块名, 为什么裁)`。空 = 没超预算。
    dropped: list[tuple[str, str]] = field(default_factory=list)
    #: 裁完还超出的 token 数（>0 表示"该给的都给了，还是放不下"）。
    #: ⚠️ 这时**不静默砍角色卡/菜单**，调用方要么换小模型，要么接受超预算。
    over_budget: int = 0

    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text)

    def has(self, name: str) -> bool:
        return name in self.included

    def get(self, name: str) -> str:
        for b in self.blocks:
            if b.name == name:
                return b.text
        return ""

    def summary(self) -> str:
        rows = [f"视角 {self.viewer}｜任务 {self.task}｜"
                f"{self.chars} 字 ≈ {self.tokens_est} tok"]
        for b in self.blocks:
            rows.append(f"  ✓ {b.name}（{len(b.text)} 字）"
                        f"{('　— ' + b.why) if b.why else ''}")
        for n, why in self.excluded:
            rows.append(f"  ✗ {n} —— {why}")
        for n, why in self.dropped:
            rows.append(f"  ✂ {n} —— {why}")
        if self.over_budget:
            rows.append(f"  ⚠️ 裁完仍超预算 {self.over_budget} tok"
                        f"（角色卡/菜单/任务/台账不裁）")
        return "\n".join(rows)


# --------------------------------------------------------------------------- #
# token 估算（**是估算，不是账单**）
# --------------------------------------------------------------------------- #

_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_ASCII = re.compile(r"[A-Za-z0-9]+")
_PUNCT = re.compile(r"[^\w\s]")


def estimate_tokens(text: str) -> int:
    """粗估 token 数。

    口径（刻意保守，宁可高估）：
      · 中日韩字符：**1 字 ≈ 1 token**（多数中文 tokenizer 是 1~1.5 字/token）
      · 英文数字词：**1 词 ≈ 1.3 token**
      · 标点与空白：按 4 个算 1 token

    ⚠️ 这只是给"包有没有涨"用的尺子，**不是账单**。
    真实 token 数只能由上游 `usage` 字段给 —— 那个要在 P3 接入模型后才拿得到。
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    words = len(_ASCII.findall(text))
    rest = len(_PUNCT.findall(text)) + text.count(" ") + text.count("\n")
    return int(cjk + words * 1.3 + rest / 4 + 0.5)


# --------------------------------------------------------------------------- #
# 装配器
# --------------------------------------------------------------------------- #

class Assembler:
    """把状态、台账、菜单按视角装成一次调用的上下文。

    典型用法：
        asm = Assembler(state, ledger)
        asm.set_block("规则", rules_text, why="本场规则片段")
        asm.set_block("可选行动", menu_text)
        a = asm.build("艾尔玛", task="现在轮到你行动。")
        prompt = a.text()
    """

    def __init__(self, state: StateTree, ledger: Ledger | None = None,
                 memory_provider: Callable[[str], str] | None = None):
        self.state = state
        self.ledger = ledger or Ledger()
        #: 「记忆」块的唯一来源：`viewer -> 这次该看到的记忆文本`。
        #: 由记忆层按视角过滤后渲染；**不许调用方自己拼字符串**（同「台账」）。
        self.memory_provider = memory_provider
        self._blocks: dict[str, Block] = {}

    # ---------- 加块 ----------

    def set_block(self, name: str, text: str, *, visible_to: str | None = None,
                  why: str = "") -> None:
        if name not in BLOCK_ORDER:
            raise ValueError(
                f"不认识的块 {name!r}（只允许：{', '.join(BLOCK_ORDER)}）\n"
                f"块顺序即优先级、也是缓存友好的关键 —— 要加新块先改 BLOCK_ORDER。")
        if name == "台账":
            # ⚠️ 「台账」是**防全知的落点**：它必须由 build() 按视角现渲染。
            #    允许手动塞的话，一次"忘了过滤"的调用就会把 DM 的秘密或别人的私有
            #    直接塞进某个玩家的包里 —— 而且看起来一切正常。**宁可报错。**
            raise ValueError(
                "「台账」不许手动设置 —— 它由 build() 按视角过滤后现渲染。\n"
                "要往台账里加内容，用 Ledger.public() / .secret() / .private()。")
        if name == "记忆":
            # 同一个理由：记忆条目带 visible_to，玩家不该看的往事必须根本不出现在
            # 他的包里。允许手动塞就等于把"按视角过滤"这一步交给调用方自觉。
            raise ValueError(
                "「记忆」不许手动设置 —— 它由 build() 通过 memory_provider 按视角"
                "召回并渲染。\n要加回忆，用 MemoryStore.record()。")
        if not text:
            self._blocks.pop(name, None)
            return
        self._blocks[name] = Block(name=name, text=text, visible_to=visible_to, why=why)

    def add_block(self, name: str, text: str, **kw) -> None:
        """追加到已有块后面（如台账分几段拼）。"""
        old = self._blocks.get(name)
        if old and old.text:
            text = old.text + "\n\n" + text
        self.set_block(name, text, **kw)

    def clear(self, name: str | None = None) -> None:
        if name is None:
            self._blocks.clear()
        else:
            self._blocks.pop(name, None)

    # ---------- 装配 ----------

    def build(self, viewer: str, *, task: str = "",
              task_block: str | None = None, max_tokens: int | None = None,
              **ledger_kw) -> Assembled:
        """为 `viewer` 视角装配一次上下文。

        `viewer` 取 `"dm"` 或某个角色名。
        `task` 会作为「任务」块追加（不传则用 `task_block` 原样）。
        `max_tokens` 给了就是**硬预算**：超过时按 `TRIM_ORDER` 从低优先级块开始
        裁（记忆 → 世界 → 队友 → 规则），并记进 `dropped`；裁完仍超则记
        `over_budget`，**绝不静默砍角色卡/菜单/任务/台账**。
        `ledger_kw` 透传给 `Ledger.render()`（`last_rounds` / `kinds` …）。
        """
        out = Assembled(viewer=viewer, task=task)

        for name in BLOCK_ORDER:
            if name == "台账":
                # 台账**总是**由这里按视角现渲染 —— 这是防全知的落点
                text = self.ledger.render(viewer, **ledger_kw) if self.ledger else ""
                if text and text != "（暂无可见记录）":
                    out.blocks.append(Block("台账", text,
                                            why=f"按 {viewer} 视角过滤后的事件"))
                    out.included.append("台账")
                else:
                    out.excluded.append(("台账", "该视角暂无可见记录"))
                continue

            if name == "记忆":
                # 与台账同一个纪律：**按视角现渲染**，调用方塞不进来。
                if self.memory_provider is None:
                    out.excluded.append(("记忆", "没提供记忆库"))
                    continue
                text = self.memory_provider(viewer)
                if text:
                    out.blocks.append(Block("记忆", text, why=(
                        f"按 {viewer} 视角召回：先按内容挑、再按时间分组")))
                    out.included.append("记忆")
                else:
                    out.excluded.append(("记忆", "这个视角没有需要召回的往事"))
                continue

            if name == "任务":
                text = task_block if task_block is not None else (
                    task or "")
                if text:
                    out.blocks.append(Block("任务", text, why="本次要它做什么"))
                    out.included.append("任务")
                else:
                    out.excluded.append(("任务", "没给任务"))
                continue

            b = self._blocks.get(name)
            if b is None:
                out.excluded.append((name, "没提供"))
                continue
            if not b.allows(viewer):
                out.excluded.append((name, f"可见性限制（只给 {b.visible_to}）"))
                continue
            out.blocks.append(b)
            out.included.append(name)

        text = out.text()
        out.chars = len(text)
        out.tokens_est = estimate_tokens(text)
        if max_tokens is not None and max_tokens > 0:
            self._apply_budget(out, int(max_tokens))
        return out

    @staticmethod
    def _apply_budget(out: Assembled, max_tokens: int) -> None:
        """按 `TRIM_ORDER` 裁到预算内；裁不动的记 `over_budget`。"""
        while out.tokens_est > max_tokens:
            victim = None
            for name in TRIM_ORDER:
                victim = next((b for b in out.blocks if b.name == name), None)
                if victim is not None:
                    break
            if victim is None:
                break
            out.blocks.remove(victim)
            if victim.name in out.included:
                out.included.remove(victim.name)
            old = out.tokens_est
            out.chars = len(out.text())
            out.tokens_est = estimate_tokens(out.text())
            out.dropped.append((
                victim.name,
                f"超预算：{old} > {max_tokens} tok，按优先级裁掉"
                f"（省约 {old - out.tokens_est} tok）"))
        if out.tokens_est > max_tokens:
            out.over_budget = out.tokens_est - max_tokens

    # ---------- 自查 ----------

    def audit(self, viewer: str) -> list[str]:
        """检查这个视角拿到了什么、少了什么（不含任务）。"""
        problems: list[str] = []
        if viewer == DM:
            return problems
        if not self.state.has_character(viewer):
            problems.append(f"状态树里没有角色 {viewer!r}（现有：{self.state.characters()}）")
            return problems
        if "角色卡" not in self._blocks:
            problems.append("没提供「角色卡」块 —— 模型不知道自己的状态")
        return problems


# --------------------------------------------------------------------------- #
# 便捷函数
# --------------------------------------------------------------------------- #

def build(state: StateTree, ledger: Ledger, viewer: str, *,
          rules: str = "", menu: str = "", teammate_text: str = "",
          world: str = "", task: str = "", system: str = "",
          max_tokens: int | None = None, **ledger_kw) -> Assembled:
    """一行装配（给简单场景用；复杂场景请用 `Assembler`）。"""
    asm = Assembler(state, ledger)
    if system:
        asm.set_block("说明", system, why="系统提示")
    if rules:
        asm.set_block("规则", rules, why="本场规则片段")
    if world:
        asm.set_block("世界", world, why="世界状态（按视角过滤）")
    asm.set_block("角色卡", state.render(viewer) if state.has_character(viewer) else "",
                  why="这个单位自己的状态")
    if teammate_text:
        asm.set_block("队友", teammate_text, why="同队的公开信息")
    if menu:
        asm.set_block("可选行动", menu, why="合法动作菜单")
    return asm.build(viewer, task=task, max_tokens=max_tokens, **ledger_kw)
