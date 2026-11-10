# coding=utf-8
"""可见性过滤 —— 防全知的核心。**零模型调用。**

对应决策记录 §5.3：dnd2024 那个产品用"额外调用一次模型检查旁白有没有泄密"来做防全知
（它自己的设置页写着"会增加调用费用和等待时间"）。**本项目不那样做** ——
照狼人杀项目的做法，用**上下文装配器 + 可见性三档**，纯代码、零额外调用。

三档（`visible_to`），与狼人杀一致：

    public       全场可见（也会进所有人的包、也会给真人前端）
    dm           只有 DM 可见（敌方密谋、暗骰、未公开的真相）
    pc:<名字>    只有这个角色可见（自己的私有经历、秘密检定结果）

`Entry.visible()` 是**唯一判据** —— 别处不许自己写 if 判断谁能看。

⚠️ 硬纪律（狼人杀踩过的 P0）：
    **私有的理由/内容绝不许出现在 public 条目里。**
    那边真的漏过一次：复用决策时把狼的私有理由拼进了公开投票条目，
    泄漏了狼的内部计划、污染了别人的提示词。这里用 `lint_leaks()` 守。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

__all__ = [
    "Visibility", "Entry", "Ledger", "SECTION_ORDER", "lint_leaks",
    "PUBLIC", "DM", "ENGINE", "PLANNER", "pc",
]

PUBLIC = "public"
DM = "dm"
#: 只有引擎/复盘看得到（暗骰明细、内部对账）。**DM 也看不到。**
ENGINE = "engine"
#: 敌方规划模型（战术参谋）的视角：**只吃 public**（战场事实/公共战报）。
#: 它不需要 DM 的幕后（世界秘密、NPC 内心、线索清单、DM 台账）—— 见 2026-11-05 内部审查。
PLANNER = "planner"


def pc(name: str) -> str:
    """私有可见性的构造函数：`pc("艾尔玛")` → `"pc:艾尔玛"`。"""
    return f"pc:{name}"


#: 渲染分组的固定顺序（狼人杀的做法：section 内保持原顺序，section 之间固定顺序）
SECTION_ORDER = (
    "公告",       # 全场播报：进入战斗、轮次变化、时间流逝
    "战斗",       # 先攻序、攻击结果、伤害、倒地
    "对话",       # NPC 与队友说出口的话
    "检定",       # 公开的检定结果
    "你的私有",   # 只有这个人看得到
)


class Visibility:
    """一组合法的可见性标签。

    ⚠️ **DM 看得到一切** —— 这是 2026-10-14 纠正过的口径。

    真实跑团里 DM（地下城主）就是知道所有角色的秘密、私有检定结果和背景故事，
    不然它没法正确推进流程（"玩家到底知不知道这件事"是 DM 必须掌握的）。
    初版把 `pc:X` 做成**对 DM 也排他**，那是错的。

    但"**DM 知道什么**"和"**DM 该说什么**"是两件事：
      · 私有事实（罗恩发现了石板）→ **必须给 DM**，否则它没法裁定玩家知不知道；
      · 私有推理/意图（"我怀疑他是变形怪"）→ 可以给，但 **DM 不许复述进旁白**；
      · 所以"不外泄"由 **提示词约束 + `lint_leaks()` 兜底**，**不是靠"不给它看"**。

    真正对 DM 也排他的只有一种：**引擎内部的暗骰明细**（`engine` 档），
    那是给复盘和对账用的，不是给 DM 叙事用的。
    """
    __slots__ = ("tags",)

    def __init__(self, *tags: str):
        if not tags:
            raise ValueError("visible_to 不能为空 —— 想清楚这条谁能看到")
        self.tags = frozenset(str(t) for t in tags)
        for t in self.tags:
            if t not in (PUBLIC, DM, ENGINE) and not t.startswith("pc:"):
                raise ValueError(
                    f"不认识的可见性 {t!r}"
                    f"（只允许 public / dm / engine / pc:<名字>）")

    @classmethod
    def of(cls, *tags: str) -> "Visibility":
        return cls(*tags)

    def allows(self, viewer: str) -> bool:
        """`viewer` 能不能看到。viewer 取 `dm` / `engine` / 角色名。

        **DM 看得到一切（除了 engine 档）。** 见类文档里那条纠正。
        """
        if PUBLIC in self.tags:
            return True
        if viewer == PLANNER:
            # 敌方规划只该看公共战场事实；dm / pc / engine 一律不给。
            return False
        if viewer == DM:
            # DM 是主持人：public / dm / pc:* 全看得到；只有 engine 档不给它
            return ENGINE not in self.tags or DM in self.tags
        if viewer == ENGINE:
            return ENGINE in self.tags or DM in self.tags
        # 角色：public 已在上面的分支处理过；剩下的看是不是自己的 pc:
        return pc(viewer) in self.tags

    def is_public(self) -> bool:
        return PUBLIC in self.tags

    def __repr__(self) -> str:
        return f"Visibility({', '.join(sorted(self.tags))})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Visibility) and other.tags == self.tags


# --------------------------------------------------------------------------- #
# 条目
# --------------------------------------------------------------------------- #

@dataclass
class Entry:
    """台账里的一条。

    `text` 是**给模型看的话**；`data` 是结构化的留痕（结算数值、骰子明细…）。
    两者分开：渲染给模型时用 text，复盘/对账时用 data。
    """
    seq: int
    kind: str                       # 战斗/对话/检定/公告/…
    text: str
    visible_to: Visibility
    section: str = "公告"
    actor: str = ""                 # 谁产生的（角色名 / "引擎" / "DM"）
    data: dict = field(default_factory=dict)
    round: int = 0                  # 战斗轮次 / 场景轮次
    at: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if self.section not in SECTION_ORDER:
            raise ValueError(f"不认识的 section {self.section!r}"
                             f"（可选：{', '.join(SECTION_ORDER)}）")

    def visible(self, viewer: str) -> bool:
        """**唯一判据。**"""
        return self.visible_to.allows(viewer)

    def line(self, viewer: str = "") -> str:
        who = f"{self.actor}：" if self.actor else ""
        return f"[第{self.round}轮] {who}{self.text}" if self.round else f"{who}{self.text}"


# --------------------------------------------------------------------------- #
# 台账
# --------------------------------------------------------------------------- #

class Ledger:
    """事件台账 + 可见性过滤。"""

    def __init__(self) -> None:
        self._entries: list[Entry] = []
        self._seq = 0

    # ---------- 写 ----------

    def add(self, kind: str, text: str, visible_to: Visibility | str | Iterable[str],
            *, section: str = "公告", actor: str = "", round: int = 0,
            data: dict | None = None) -> Entry:
        self._seq += 1
        vis = _as_visibility(visible_to)
        e = Entry(seq=self._seq, kind=kind, text=text, visible_to=vis,
                  section=section, actor=actor, data=dict(data or {}), round=round)
        self._entries.append(e)
        return e

    def public(self, kind: str, text: str, **kw) -> Entry:
        return self.add(kind, text, PUBLIC, **kw)

    def secret(self, kind: str, text: str, **kw) -> Entry:
        """只有 DM 知道。"""
        return self.add(kind, text, DM, **kw)

    def private(self, kind: str, text: str, who: str, **kw) -> Entry:
        """只有 `who` 知道（**DM 也看得到** —— 它是主持人）。"""
        return self.add(kind, text, pc(who), **kw)

    def engine_only(self, kind: str, text: str, **kw) -> Entry:
        """只有引擎/复盘看得到（暗骰明细、内部对账）。**连 DM 都不给。**"""
        return self.add(kind, text, ENGINE, **kw)

    # ---------- 读 ----------

    @property
    def entries(self) -> list[Entry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def visible_to(self, viewer: str) -> list[Entry]:
        """`viewer` 能看到的全部条目（按原顺序）。"""
        return [e for e in self._entries if e.visible(viewer)]

    def render(self, viewer: str, *, sections: Iterable[str] | None = None,
               kinds: Iterable[str] | None = None,
               last_rounds: int | None = None,
               exclude_seqs: Iterable[int] = ()) -> str:
        """渲染给某个视角看的台账。

        `last_rounds=N` 只保留最近 N 轮（战斗日志长了要截断，见决策记录 §4）。
        截断时**写明折叠了多少条** —— 不然模型会以为"前面什么都没发生"，
        而实际上更早的事在「记忆」块里（P5-G1）。
        """
        want_sec = set(sections) if sections else set(SECTION_ORDER)
        want_kind = set(kinds) if kinds else None
        skip = set(exclude_seqs)

        rows = [e for e in self.visible_to(viewer)
                if e.section in want_sec
                and (want_kind is None or e.kind in want_kind)
                and e.seq not in skip]
        omitted = 0
        if last_rounds is not None and rows:
            maxr = max(e.round for e in rows)
            kept = [e for e in rows if e.round >= maxr - last_rounds + 1]
            omitted = len(rows) - len(kept)
            rows = kept

        out: list[str] = []
        if omitted:
            out.append(f"（更早的 {omitted} 条记录已折叠，只保留最近 "
                       f"{last_rounds} 轮）")
        for sec in SECTION_ORDER:
            group = [e for e in rows if e.section == sec]
            if not group:
                continue
            out.append(f"—— {sec} ——")
            out.extend(e.line(viewer) for e in group)
        return "\n".join(out) if out else "（暂无可见记录）"

    def render_grouped(self, viewer: str, **kw) -> str:
        return self.render(viewer, **kw)

    # ---------- 复盘用：连私有一起看（**只给人看，不许进模型包**） ----------

    # ---------- 序列化（存档用） ----------

    def to_dict(self) -> dict:
        """导出整本台账。**可见性标签原样保留** —— 读回来不能把私有条目变公开。"""
        return {
            "seq": self._seq,
            "entries": [{
                "seq": e.seq,
                "kind": e.kind,
                "text": e.text,
                "visible_to": sorted(e.visible_to.tags),
                "section": e.section,
                "actor": e.actor,
                "data": dict(e.data),
                "round": e.round,
                "at": e.at,
            } for e in self._entries],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Ledger":
        if not isinstance(d, dict):
            raise ValueError(f"台账存档必须是对象，拿到的是 {type(d).__name__}")
        led = cls()
        for row in (d.get("entries") or []):
            if not isinstance(row, dict) or "kind" not in row or "text" not in row:
                raise ValueError(f"台账条目格式不对：{row!r}")
            try:
                tags = list(row.get("visible_to") or [])
                if not tags:
                    raise ValueError("缺少 visible_to")
                e = led.add(str(row["kind"]), str(row["text"]), tags,
                            section=str(row.get("section") or "公告"),
                            actor=str(row.get("actor") or ""),
                            round=int(row.get("round") or 0),
                            data=dict(row.get("data") or {}))
                if row.get("seq") is not None:
                    e.seq = int(row["seq"])
                if row.get("at"):
                    e.at = str(row["at"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"台账条目格式不对：{row!r}") from exc
            led._seq = max(led._seq, e.seq)
        if isinstance(d.get("seq"), int):
            led._seq = max(led._seq, int(d["seq"]))
        return led

    def render_omniscient(self) -> str:
        out = []
        for e in self._entries:
            tags = ",".join(sorted(e.visible_to.tags))
            out.append(f"[第{e.round}轮][{e.section}][{tags}] {e.line()}")
        return "\n".join(out)


def _as_visibility(v: Visibility | str | Iterable[str]) -> Visibility:
    if isinstance(v, Visibility):
        return v
    if isinstance(v, str):
        return Visibility(v)
    return Visibility(*v)


# --------------------------------------------------------------------------- #
# 泄漏检查（狼人杀那个 P0 的回归守）
# --------------------------------------------------------------------------- #

def lint_leaks(ledger: Ledger, *, min_len: int = 12) -> list[str]:
    """找出"私有内容跑进公开条目"的痕迹。

    判据：某条 public 条目的文本，与任何一条非 public 条目的文本有
    **长度 ≥ `min_len` 的公共子串** —— 那基本就是泄漏。

    狼人杀用的就是这个思路（最长公共子串 ≥ 20 字判泄漏），
    **不写死句子**，所以它不会因为改文案而假绿。
    """
    problems: list[str] = []
    pubs = [e for e in ledger.entries if e.visible_to.is_public()]
    secrets = [e for e in ledger.entries if not e.visible_to.is_public()]
    if not pubs or not secrets:
        return problems

    for p in pubs:
        pt = _norm(p.text)
        if len(pt) < min_len:
            continue
        for s in secrets:
            st = _norm(s.text)
            if len(st) < min_len:
                continue
            lcs = _lcs_len(pt, st)
            if lcs >= min_len:
                problems.append(
                    f"公开条目 #{p.seq} 与私有条目 #{s.seq}（{sorted(s.visible_to.tags)}）"
                    f"有 {lcs} 字公共子串 —— 疑似泄漏私有内容")

    # ⚠️ 只查 public↔私有 会漏掉两个方向：
    #   ① DM 密记被抄进某个玩家的私有条目（该玩家会看到 DM 的秘密）；
    #   ② 甲的私有被抄进乙的私有（乙看到甲的秘密）。
    def _pc_of(entry) -> str | None:
        return next((t[3:] for t in entry.visible_to.tags
                     if t.startswith("pc:")), None)

    dm_only = [e for e in secrets if e.visible_to.tags == frozenset({DM})]
    pc_entries = [e for e in secrets if _pc_of(e)]
    for s in dm_only:
        st = _norm(s.text)
        if len(st) < min_len:
            continue
        for p in pc_entries:
            pt = _norm(p.text)
            if len(pt) < min_len:
                continue
            lcs = _lcs_len(pt, st)
            if lcs >= min_len:
                problems.append(
                    f"DM 密记 #{s.seq} 出现在 pc:{_pc_of(p)} 的私有条目 #{p.seq}"
                    f"（{lcs} 字公共子串）—— 该玩家会看到 DM 的秘密")
    # pc→pc 暂不查：两个 PC 可能各自得到同一条中性结果（例如线索检定失败句
    # "你仔细看过、听过，没发现什么异常。"），按子串判会误报。DM→pc 才是
    # 真正伤桌面的方向，已在上面的 dm_only 分支覆盖。
    return problems


def _norm(s: str) -> str:
    """归一：去掉空白与常见标点，只留实义字符。"""
    return re.sub(r"[\s，。、；：！？「」『』（）()\[\]【】…—\-]+", "", s or "")


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（滚动数组，O(min(n,m)) 空间）。

    故意用"公共子串"而不是"公共子序列" —— 泄漏是**连续一段话被抄过去**，
    子序列会把"零零散散重复了几个字"也算上，误报太多。
    """
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    prev = [0] * (len(a) + 1)
    best = 0
    for i in range(1, len(b) + 1):
        cur = [0] * (len(a) + 1)
        for j in range(1, len(a) + 1):
            if b[i - 1] == a[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best
