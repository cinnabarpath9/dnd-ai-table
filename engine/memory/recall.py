# coding=utf-8
"""按内容召回过去的事件（P5-G 的 ①），**零模型调用、确定性可复现**。

格林规则里最容易被写错的一条是：

    **先按内容选，再按时间分组。** 不是"从最近的往前数几条"。

所以这里的分数是"这件事和现在有没有关系"，与时间**无关**：
一分钟前的无关小事进不了榜，半年前的那条欠条反而要进。

三层联系（照抄预设的 REAL / WEAK / FALLBACK，只是把"模型判断"换成"可算的证据"）：

    REAL（≥3 分）  共享线索标签（thread / promise / clue）＝ 因果链、伏笔、物证；
                   或"同一批人 + 同一个地点"（共同经历真的在影响当下）
    WEAK（2 分）   共享一个重要的人（这段关系有前账/张力）
    FALLBACK（1 分）只共享地点或只共享一个人名 —— 仅用来补位，且**标明是补位**

权重：线索 3 / 人物 2 / 地点 1；当前情境文本里出现的人名地名再 +1（最多 +2）。

⚠️ 与预设的一处**刻意不同**：预设按 `required_count = min(召回数, 总数)` 硬凑，
凑不满就用 FALLBACK 填。这里**不硬凑** —— 只有真有一分关系的才入选，
不够 `count` 就如实说"只有 N 条相关"。凑数会把无关记忆塞进提示词，
而模型最擅长的事就是把塞进来的东西编成因果。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from ..state.visibility import DM
from .clock import (
    GROUP_LABELS, GROUP_ORDER, GameTime, bucket_of, diff_minutes, display_of,
    time_words,
)
from .entries import MemoryEntry, MemoryStore

__all__ = [
    "Query", "Recalled", "RecallResult", "score_entry", "recall",
    "render", "match_text", "TIER_WEIGHT", "ALIASES", "canonical",
    "build_idf", "TEXT_CAP",
]

#: 证据权重（线索 > 人物 > 地点）。改权重 = 改"什么算有关"，所以写成常量。
TIER_WEIGHT = {"tag": 3.0, "person": 2.0, "place": 1.0}
#: 分档阈值：≥3 REAL，≥2 WEAK，>0 FALLBACK。
REAL_AT = 3.0
WEAK_AT = 2.0
#: 情境/摘要文本证据的**总分上限**：再相关的描写也不能单独把一条抬到 REAL。
TEXT_CAP = 2.0

#: 别名词典（`别名 → 正名`）。**只放"确定等价"的说法**，不猜近义。
#: 以后接向量时它仍然有用：入库前规范化、查询时实体对齐。
ALIASES: dict[str, str] = {
    "短刀": "匕首",
    "小刀": "匕首",
    "药水": "治疗药水",
    "红药": "治疗药水",
    "足迹": "脚印",
}

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub("", str(s or ""))


def canonical(term: str, aliases: dict[str, str] | None = None) -> str:
    """把一个说法折成正名（未知原样返回）。ID 形式的标签不折。"""
    s = str(term or "").strip()
    if not s or ":" in s:                      # clue:p2 这种 id 不参与别名
        return s
    table = ALIASES if aliases is None else aliases
    return table.get(s, s)


@lru_cache(maxsize=8)
def _canon_matcher(items: tuple[tuple[str, str], ...]):
    """把别名表编成"最长优先"的一次替换器。

    ⚠️ 不能逐个别名 `str.replace` 串联：`治疗药水` 里含别名 `药水`，
    串着替换会变成"治疗治疗药水"。这里把**正名也放进候选**（映射到自身），
    最长优先，一次扫描完成。
    """
    mapping: dict[str, str] = {}
    for alias, name in items:
        mapping[alias] = name
        mapping.setdefault(name, name)          # 正名 → 自身（防止被自己的子串污染）
    terms = sorted(mapping, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(t) for t in terms)) if terms else None
    return pattern, mapping


def _canon_text(text: str, aliases: dict[str, str] | None = None) -> str:
    """正文里的别名折成正名（最长优先、一次替换），再做子串匹配。"""
    s = _norm(text)
    table = ALIASES if aliases is None else aliases
    if not s or not table:
        return s
    pattern, mapping = _canon_matcher(
        tuple(sorted(table.items(), key=lambda kv: (-len(kv[0]), kv[0]))))
    return pattern.sub(lambda m: mapping[m.group(0)], s) if pattern else s


def build_idf(entries, aliases: dict[str, str] | None = None) -> dict[str, float]:
    """语料内 IDF：`log((N+1)/(df+1)) + 1`。**只统计结构化词元**（人/地/标签）。

    为什么不用词频：这里的"词元"是作者写的人名/地名/线索 id，没有分词歧义；
    正文摘要只做别名规范化后的子串匹配，不参与 df 统计（避免中文分词引入噪音）。
    """
    rows = list(entries)
    n = len(rows)
    df: Counter = Counter()
    for e in rows:
        terms = {canonical(t, aliases) for t in (*e.people, *e.places, *e.tags)}
        for t in terms:
            if t:
                df[t] += 1
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


# --------------------------------------------------------------------------- #
# 问题：现在在问什么
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Query:
    """一次召回要回答的"现在"。

    `text` 是当前情境（场景描写/本轮输入）；`people`/`places` 是**当前在场**的
    人和地点；`tags` 是当前正在推进的线索（剧本 thread/promise id）。
    """
    text: str = ""
    people: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    viewer: str = DM
    #: 当前游戏内时刻（用来算"多久以前"）。None = 算不出来 → unknown 档。
    now: GameTime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "people", _uniq(self.people))
        object.__setattr__(self, "places", _uniq(self.places))
        object.__setattr__(self, "tags", _uniq(self.tags))


def _uniq(items) -> tuple[str, ...]:
    out: list[str] = []
    for x in items or ():
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
    return tuple(out)


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #

def score_entry(entry: MemoryEntry, query: Query, *,
                idf: dict[str, float] | None = None,
                aliases: dict[str, str] | None = None) -> tuple[float, str]:
    """返回 `(分数, 证据说明)`。**完全不看时间。**

    `idf` 给了才按"词元稀有度"加权文本证据；不给 = 旧的每命中 +1（行为不变）。
    `aliases` 用来把"短刀/匕首"这类确定等价说法折成正名（结构化词元与正文都折）。
    """
    entry_tags = [canonical(t, aliases) for t in entry.tags]
    entry_people = [canonical(t, aliases) for t in entry.people]
    entry_places = [canonical(t, aliases) for t in entry.places]
    q_tags = [canonical(t, aliases) for t in query.tags]
    q_people = [canonical(t, aliases) for t in query.people]
    q_places = [canonical(t, aliases) for t in query.places]

    tags = [t for t in entry_tags if t in q_tags]
    people = [p for p in entry_people if p in q_people]
    places = [s for s in entry_places if s in q_places]

    score = (len(tags) * TIER_WEIGHT["tag"]
             + len(people) * TIER_WEIGHT["person"]
             + len(places) * TIER_WEIGHT["place"])

    # 文本证据：A) 情境里点到记忆的 人/地/标签；
    #           B) 记忆摘要里点到"现在在问的" 人/地/标签（别名已折成正名）。
    # 两层都按 IDF 加权（没给 idf 就是每命中 1 分），总分封顶 TEXT_CAP，
    # 免得一段描写单独把一条抬进 REAL。
    text = _canon_text(query.text, aliases)
    text_hits = 0.0
    hit_terms: list[str] = []
    if text:
        for token in (*entry_people, *entry_places, *entry_tags):
            t = _norm(token)
            if t and t in text and t not in hit_terms:
                hit_terms.append(t)
                text_hits += (idf.get(t, 1.0) if idf else 1.0)
    matched = set(tags) | set(people) | set(places)
    summary = _canon_text(entry.summary, aliases)
    if summary:
        for token in (*q_people, *q_places, *q_tags):
            t = _norm(token)
            if t and t in summary and t not in hit_terms and t not in matched:
                hit_terms.append(t)
                text_hits += 0.5 * (idf.get(t, 1.0) if idf else 1.0)
        # 别名跨说法：两边都折成正名后，同一个正名同时出现在情境与摘要里
        # （"短刀"↔"匕首" 两个方向都算）→ 弱证据。
        table = ALIASES if aliases is None else aliases
        for name in {canonical(k, table) for k in table}:
            if (name in text and name in summary
                    and name not in hit_terms and name not in matched):
                hit_terms.append(name)
                text_hits += 0.5 * (idf.get(name, 1.0) if idf else 1.0)
    score += min(TEXT_CAP, text_hits)

    bits: list[str] = []
    if tags:
        bits.append("共享线索 " + "、".join(tags))
    if people:
        bits.append("同场人物 " + "、".join(people))
    if places:
        bits.append("同一地点 " + "、".join(places))
    if hit_terms:
        bits.append(f"情境/摘要提到 {len(hit_terms)} 个相关词"
                    + ("（稀有词加权）" if idf else ""))
    return score, "；".join(bits) or "没有可算的重合"


def _tier(score: float) -> str:
    if score >= REAL_AT:
        return "REAL"
    if score >= WEAK_AT:
        return "WEAK"
    return "FALLBACK"


def _kind(entry: MemoryEntry, query: Query, tier: str) -> str:
    """把"为什么相关"归到预设的八个类型名上。**是启发式，不是推理。**"""
    if tier == "FALLBACK":
        return "补位召回"
    tags = [t for t in entry.tags if t in query.tags]
    people = [p for p in entry.people if p in query.people]
    places = [s for s in entry.places if s in query.places]
    if any(str(t).startswith("clue") for t in tags):
        return "物品线索"
    if tags:
        return "因果链"
    if len(people) >= 2:
        return "人物关系"
    if places and not people:
        return "地点延续"
    return "共同经历"


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Recalled:
    """召回中的一条：证据 + 选完之后才算出来的时间。"""
    entry: MemoryEntry
    score: float
    tier: str
    kind: str
    reason: str
    diff_min: int | None = None
    group: str = "unknown"
    display: str = "记不清时间"
    #: 登记顺序（只在分数打平时用来稳定排序，**不是**时间远近）
    order: int = 0


@dataclass
class RecallResult:
    query: Query
    items: list[Recalled] = field(default_factory=list)
    #: 这个视角一共有多少条可选（用来解释"为什么只召回到 2 条"）
    available: int = 0
    asked: int = 0

    def __len__(self) -> int:
        return len(self.items)

    def has(self, memory_id: str) -> bool:
        return any(r.entry.id == memory_id for r in self.items)

    def ids(self) -> list[str]:
        return [r.entry.id for r in self.items]

    def by_group(self) -> dict[str, list[Recalled]]:
        out: dict[str, list[Recalled]] = {}
        for r in self.items:
            out.setdefault(r.group, []).append(r)
        return out


# --------------------------------------------------------------------------- #
# 选
# --------------------------------------------------------------------------- #

def recall(store: MemoryStore, query: Query, *, count: int = 6,
           use_idf: bool = True, aliases: dict[str, str] | None = None
           ) -> RecallResult:
    """按内容挑最多 `count` 条。**排序键里没有时间。**

    `use_idf=True`（默认）：文本证据按语料内 IDF 加权；
    `use_idf=False`：每命中 +1（旧口径，方便对拍/回退）。
    """
    if count < 0:
        raise ValueError(f"count 不能是负数：{count}")
    pool = store.visible_to(query.viewer)
    idf = build_idf(pool, aliases) if use_idf else None
    scored: list[Recalled] = []
    for i, e in enumerate(pool):
        s, reason = score_entry(e, query, idf=idf, aliases=aliases)
        if s <= 0:
            continue
        tier = _tier(s)
        kind = _kind(e, query, tier)
        diff, group, show = _when(e, query.now)
        scored.append(Recalled(entry=e, score=s, tier=tier, kind=kind,
                               reason=reason, diff_min=diff, group=group,
                               display=show, order=i))
    # 分数降序；同分按登记顺序（稳定、可复现）。**任何情况下都不按时间排。**
    scored.sort(key=lambda r: (-r.score, r.order))
    return RecallResult(query=query, items=scored[:count],
                        available=len(pool), asked=count)


def _when(entry: MemoryEntry, now: GameTime | None
          ) -> tuple[int | None, str, str]:
    """选完之后才算时间：用**结束时间**（预设口径），没有就用开始时间。"""
    anchor = entry.end or entry.start
    if anchor is None or not anchor.known:
        return None, "unknown", display_of(None, "unknown")
    if now is None or not now.known:
        return None, "unknown", display_of(None, "unknown")
    diff = diff_minutes(now, anchor)
    # 选中的这条如果比"现在"还晚（记忆写错了/时钟倒流），明说不知道，不硬算
    group = bucket_of(diff, same_date=bool(
        now.date and anchor.date and now.date == anchor.date))
    return diff, group, display_of(diff, group)


# --------------------------------------------------------------------------- #
# 渲染（给提示词）
# --------------------------------------------------------------------------- #

def _clip(text: str, n: int = 70) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n].rstrip("，。、；：") + "…"


def render(result: RecallResult, *, viewer: str = "", header: bool = True,
           max_chars: int = 900) -> str:
    """把召回结果拼成提示词里的「记忆」块。

    三条排版口径：
      · **按时间档分组**，组内保持"分高在前"（不是按时间）；
      · 每条只给 `编号｜距今｜摘要`，**类型与理由不进提示词**（那是复盘用的）；
      · 结尾给"别念编号"的约束和本组可用的时间词 —— 否则模型会把 `AM0002`
        当成专有名词写进旁白。
    """
    if not result.items:
        return ""
    rows: list[str] = []
    if header:
        rows.append("【记忆 · 时间召回】"
                    f"（按内容挑，不按远近；共 {len(result.items)} 条）")

    groups = result.by_group()
    words_used: list[tuple[str, tuple[str, ...]]] = []
    for group in GROUP_ORDER:
        items = groups.get(group)
        if not items:
            continue
        rows.append(f"—— {GROUP_LABELS[group]} ——")
        for r in items:
            rows.append(f"{r.entry.id}｜{r.display}｜{_clip(r.entry.summary)}")
        words_used.append((group, time_words(group)))

    if words_used:
        flat: list[str] = []
        for _g, words in words_used:
            for w in words[:4]:
                if w not in flat:
                    flat.append(w)
        rows.append("【时间措辞】" + "、".join(flat))
    rows.append("【纪律】只用这些记忆保持前后一致；不要念出 AM 编号；"
                "不要因为「很久以前」就否认它发生过，"
                "也不要替玩家回忆他没经历过的事。")

    text = "\n".join(rows)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（记忆块超出预算，已截断）"
    return text


def recall_text(store: MemoryStore, query: Query, *, count: int = 6,
                max_chars: int = 900) -> str:
    """一步到位：召回 + 渲染。没有相关的就返回空串（装配器会标"没有可召回"）。"""
    return render(recall(store, query, count=count), max_chars=max_chars)


def match_text(result: RecallResult) -> str:
    """复盘用的对账表（预设 `<match>` 的形状）。**不进提示词。**"""
    rows = ["AM编号｜类型｜分数｜层级｜diff_min｜档位｜展示｜理由"]
    for r in result.items:
        diff = "NA" if r.diff_min is None else str(r.diff_min)
        rows.append(f"{r.entry.id}｜{r.kind}｜{r.score:g}｜{r.tier}｜{diff}｜"
                    f"{r.group}｜{r.display}｜{r.reason}")
    if len(rows) == 1:
        rows.append("（没有可召回的记忆）")
    return "\n".join(rows)