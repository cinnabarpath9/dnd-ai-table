# coding=utf-8
"""长跑（P5-B2）：把"一场 3 小时会话"压成可测的轮次，量上下文有没有失控。

为什么要有这一层：
    P5 之前的验收都是"一场戏"——3 场、6 轮战斗。但真实使用是**连续几十轮**，
    而台账是**只加不减**的：每轮 DM 旁白、每个 PC 的台词与裁定都进台账，
    下一轮又整本渲染进提示词。实测 30 轮从 941 tok 涨到 7,900 tok（约 8 倍）——
    到 60 轮就是一场把上下文吃掉一半的账。

这一层的三件事：
    1. **量**：每轮记"调用数 / 台账条数 / 记忆条数 / 最大提示词（字与估算 tok）"；
    2. **闸**：`budget_problems()` 给出"超预算 / 还在持续增长"的问题清单；
    3. **对比**：同一份剧本，折叠与不折叠台账，包体差多少。

⚠️ 它**不产生真模型调用**：`CaptureModel` 是 `FakeModel` 的子类，只记请求，
   仍然零网络、零花费。换成真模型时把 `loop.client` 换掉即可，这一层不用改。
"""

from __future__ import annotations

from dataclasses import dataclass

from .dice import Random
from .memory import MemoryStore
from .models import FakeModel
from .rules.loop import GameLoop, make_hero_unit
from .state import Ledger, StateTree, new_character
from .state.assembler import estimate_tokens
from .state.world import World, new_world

__all__ = [
    "CaptureModel", "build_table_loop", "RoundRow", "LongRun",
]

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}
DEFAULT_NAMES = ("艾尔玛", "罗恩", "小翠", "布兰")


class CaptureModel(FakeModel):
    """记下每次请求正文（假模型，零花费）。长跑测量用。"""

    def __init__(self, name: str = "长跑（假模型）") -> None:
        super().__init__(name=name)
        self.reqs: list = []

    def _complete(self, req):
        self.reqs.append(req)
        return super()._complete(req)


def build_table_loop(*, names=DEFAULT_NAMES, memory: MemoryStore | None = None,
                     seed: int = 20261105, location: str = "废弃哨塔",
                     date: str = "1492-09-17", period: str = "午后",
                     ledger_rounds: int = 6
                     ) -> tuple[StateTree, Ledger, GameLoop]:
    """造一张四人桌（4 个 PC、假模型、可选记忆库）。**零网络、零花费。**"""
    state = StateTree.from_template(
        {n: new_character(n, level=5, job="战士", abilities=AB,
                          hp_max=40, ac=16) for n in names},
        current=names[0], world=new_world(date=date, period=period))
    w = World(state).ensure()
    w.discover(location, description="塔门半开，里面很暗。")
    w.travel(location, actor="DM")
    ledger = Ledger()
    client = CaptureModel()
    loop = GameLoop(state, ledger, client, rng=Random(seed),
                    memory=memory, ledger_rounds=ledger_rounds)
    loop.set_field([make_hero_unit(state, n) for n in names])
    return state, ledger, loop


@dataclass
class RoundRow:
    """一轮的体检数据。"""
    round: int
    calls: int
    ledger_entries: int
    memory_entries: int
    max_prompt_chars: int
    max_prompt_tokens: int
    dm_ledger_chars: int
    folded: bool

    def line(self) -> str:
        return (f"{self.round:>3}  {self.calls:>2}  {self.ledger_entries:>5}  "
                f"{self.memory_entries:>5}  {self.max_prompt_chars:>7}  "
                f"{self.max_prompt_tokens:>6}  {self.dm_ledger_chars:>7}"
                f"  {'是' if self.folded else ''}")


class LongRun:
    """在一条已经造好的循环上跑 N 轮，逐轮记体检数据。"""

    def __init__(self, loop: GameLoop, *, situation: str = "你们继续在塔里探查。"):
        if not isinstance(getattr(loop.client, "reqs", None), list):
            raise TypeError(
                "LongRun 需要带 `reqs` 的客户端（用 CaptureModel 或子类）—— "
                "否则量不到提示词正文")
        self.loop = loop
        self.situation = situation
        self.rows: list[RoundRow] = []

    def step(self, *, situation: str | None = None) -> RoundRow:
        loop = self.loop
        client = loop.client
        before = len(client.reqs)
        base_round = loop.round
        loop.run_explore_round_all(
            situation=situation if situation is not None else
            f"{self.situation}（第 {base_round + 1} 轮）")
        prompts = [r.prompt for r in client.reqs[before:]]
        sizes = [len(p) for p in prompts] or [0]
        toks = [estimate_tokens(p) for p in prompts] or [0]
        dm_text = loop.ledger.render("dm", last_rounds=loop.ledger_rounds)
        row = RoundRow(
            round=loop.round,
            calls=len(prompts),
            ledger_entries=len(loop.ledger),
            memory_entries=0 if loop.memory is None else len(loop.memory),
            max_prompt_chars=max(sizes),
            max_prompt_tokens=max(toks),
            dm_ledger_chars=len(dm_text),
            folded="已折叠" in dm_text,
        )
        self.rows.append(row)
        return row

    def run(self, rounds: int) -> list[RoundRow]:
        for _ in range(max(0, int(rounds))):
            self.step()
        return list(self.rows)

    # ---------- 报告 ----------

    def summary(self) -> str:
        head = ("轮  调用  台账  记忆  最大提示词(字)  最大tok  DM台账字  折叠")
        return "\n".join([head] + [r.line() for r in self.rows])

    def plateau(self, *, after: int = 8) -> int:
        """预热之后的**波动幅度**（最大 tok - 最小 tok）。理想情况接近 0。"""
        rows = [r for r in self.rows if r.round > after]
        if not rows:
            return 0
        toks = [r.max_prompt_tokens for r in rows]
        return max(toks) - min(toks)

    def budget_problems(self, *, max_tokens: int = 3000,
                        max_growth: int = 200) -> list[str]:
        """超预算 / 没有收敛 → 问题清单（空 = 通过）。"""
        problems: list[str] = []
        if not self.rows:
            return ["一轮都没跑 —— 体检本身失效（不是通过）"]
        peak = max(r.max_prompt_tokens for r in self.rows)
        if peak > max_tokens:
            problems.append(f"单次提示词峰值 {peak} tok，超过预算 {max_tokens}"
                            f"（第 {max(self.rows, key=lambda r: r.max_prompt_tokens).round} 轮）")
        growth = self.plateau()
        if growth > max_growth:
            problems.append(f"预热后仍在增长：波动 {growth} tok > {max_growth} —— "
                            f"台账/记忆没有收敛")
        if not any(r.max_prompt_tokens for r in self.rows):
            problems.append("提示词全是 0 字 —— 测量口径失效")
        return problems