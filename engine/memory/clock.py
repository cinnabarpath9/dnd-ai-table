# coding=utf-8
"""游戏内时钟 + 时间分组 —— 「时间召回·格林情境维护」的规则落地（P5-G）。

来源：用户给的那份 SillyTavern 预设 `时间召回_格林情境维护.json`。它的核心是两步：

    ① **先按内容挑** 哪几条过去的事件与当前剧情有关（绝不按"离得近"挑）；
    ② **挑完之后** 才算这几条距今多久，再按固定边界分成 now/today/days/... 九档。

本文件只做 ② 里**能算准**的部分：日期/时段 → 分钟差 → 档位 → 展示串。
① 在 `recall.py`。**这里不调模型、不读文件、不依赖状态树。**

⚠️ 三条口径（写死，回归逐条钉住）：
    1. **档位边界是含边界的**：120 还是 now，121 就是 today；1440 还是 today，
       1441 就是 days —— 预设原文就是这么写的，不要"四舍五入"。
    2. **now / today 必须"同一天"**：跨了日就是 days 起步，
       哪怕只差 5 分钟（午夜前后）。日期差 ≠ 0 ⇒ same_date=false。
    3. **算不出来就明说"记不清时间"**：日期或时段缺失、时段不在表里、
       算出差是负数（记忆比现在还晚）—— 一律 unknown，**不猜**。

为什么用"游戏内时间"而不是墙上时间（`datetime.now()`）：
    跑团叙事里的"三天前"是**故事时间**。存档读档、隔天再跑，墙上时间全变了，
    而故事时间不变。用墙上时间会让同一份存档在不同日子召回出的措辞不同 ——
    那就不是"记忆"，是"日志"。
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

__all__ = [
    "ClockError", "GameTime", "PERIOD_HOURS", "GROUP_ORDER", "GROUP_LABELS",
    "TIME_WORDS", "BUCKET_ROWS", "parse_game_time", "parse_clock",
    "diff_minutes", "bucket_of", "display_of", "time_words", "describe",
    "known_periods",
]


class ClockError(ValueError):
    """时间参数不成立（不是"记不清"，是调用方给错了）。"""


# --------------------------------------------------------------------------- #
# 时段 → 一天中的第几分钟
# --------------------------------------------------------------------------- #

#: 时段名 → 小时。**表外的时段按 unknown 处理，不猜"大概是下午吧"。**
#: 这些值是叙事刻度，不是精确天文时间 —— 改表就等于改所有历史记忆的年龄，
#: 所以它是显式常量，不藏在函数里。
PERIOD_HOURS: dict[str, int] = {
    "凌晨": 3,
    "拂晓": 5,
    "黎明": 5,
    "清晨": 6,
    "早晨": 7,
    "早上": 7,
    "上午": 9,
    "正午": 12,
    "中午": 12,
    "午后": 14,
    "下午": 15,
    "傍晚": 17,
    "黄昏": 18,
    "入夜": 19,
    "夜晚": 20,
    "夜里": 21,
    "深夜": 23,
}

MINUTES_PER_DAY = 1440


def known_periods() -> list[str]:
    """表里有哪些时段（给报错信息用）。"""
    return sorted(PERIOD_HOURS)


# --------------------------------------------------------------------------- #
# 一个时刻
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GameTime:
    """游戏内的一个时刻。**任一部分缺失就是"不知道"**，不做部分推算。"""

    date: _dt.date | None = None
    minutes: int | None = None          # 0..1439，从午夜起

    def __post_init__(self) -> None:
        if self.minutes is not None and not (0 <= self.minutes < MINUTES_PER_DAY):
            raise ClockError(f"分钟数越界：{self.minutes}（应在 0~1439）")

    @property
    def known(self) -> bool:
        return self.date is not None and self.minutes is not None

    def abs_minutes(self) -> int | None:
        """自公元 1 年 1 月 1 日 00:00 起的分钟数（只做差，不做换算）。"""
        if not self.known:
            return None
        return self.date.toordinal() * MINUTES_PER_DAY + self.minutes

    def clock_text(self) -> str:
        if self.minutes is None:
            return ""
        return f"{self.minutes // 60:02d}:{self.minutes % 60:02d}"

    def iso(self) -> str:
        """存档用：`1492-09-17 14:00`；不知道就空串。"""
        if not self.known:
            return ""
        return f"{self.date.isoformat()} {self.clock_text()}"

    def __str__(self) -> str:
        return self.iso() or "（时间不明）"


def parse_date(text: str) -> _dt.date | None:
    """`1492-09-17` / `1492/9/17` 认；`第 3 日` 这种叙事日期**认不出就是 None**。"""
    s = str(text or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_clock(text: str) -> int | None:
    """`14:30` → 870；`午后` → 840；认不出 → None。"""
    s = str(text or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2})[:：](\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mi < 60:
            return h * 60 + mi
        return None
    h = PERIOD_HOURS.get(s)
    return None if h is None else h * 60


def parse_game_time(date_text: str, period_text: str) -> GameTime:
    """把世界树里的 `时间.日期` + `时间.时段` 变成一个时刻。

    认不出任何一半，就得到"不知道"的时刻 —— 调用方不用 try/except，
    `known` 会是 False，`describe()` 会说"记不清时间"。
    """
    d = parse_date(date_text)
    minutes = parse_clock(period_text)
    return GameTime(date=d, minutes=minutes)


# --------------------------------------------------------------------------- #
# 差多少
# --------------------------------------------------------------------------- #

def diff_minutes(now: GameTime, then: GameTime) -> int | None:
    """`now - then` 的分钟数。任一方不明 / 差为负 → None（不猜）。"""
    a, b = now.abs_minutes(), then.abs_minutes()
    if a is None or b is None:
        return None
    d = a - b
    return d if d >= 0 else None


def same_day(now: GameTime, then: GameTime) -> bool:
    if now.date is None or then.date is None:
        return False
    return now.date == then.date


# --------------------------------------------------------------------------- #
# 分档（预设原文的边界表，逐行照抄）
# --------------------------------------------------------------------------- #

#: 九档 + 未知。**顺序就是渲染顺序**（从最近到最远，unknown 最后）。
GROUP_ORDER: tuple[str, ...] = (
    "now", "today", "days", "weeks", "months", "seasons", "years", "old", "unknown",
)

#: 渲染时给人看的中文档名（与预设的时间约束词一一对应）。
GROUP_LABELS: dict[str, str] = {
    "now": "刚刚 / 此刻",
    "today": "今天早些时候",
    "days": "这几天",
    "weeks": "前阵子",
    "months": "上个月前后",
    "seasons": "几个月前",
    "years": "一年上下",
    "old": "很久以前",
    "unknown": "记不清时间",
}

#: 每一档推荐的时间约束词（照抄预设 `[时间约束词]` 段）。
#: 用途：让 DM 用"那天""前阵子"这类词自然带出时间，而不是写"AM0003 发生于…"。
TIME_WORDS: dict[str, tuple[str, ...]] = {
    "now": ("刚刚", "刚才", "眼下", "此刻", "这会儿"),
    "today": ("今天早些时候", "今天稍早", "白天的时候", "今天"),
    "days": ("这两天", "这几天", "近几天", "前些天"),
    "weeks": ("前阵子", "这阵子", "早些时候"),
    "months": ("半个月前", "上个月", "之前", "不久前", "前些日子"),
    "seasons": ("几个月前", "小半年前", "当时", "那阵子", "上次"),
    "years": ("一年前", "去年", "前几年", "这几年", "几年前", "那几年"),
    "old": ("很久以前", "过去", "那阵子", "早些时候", "当时", "上次"),
    "unknown": ("有一次", "曾经", "某次", "上次"),
}

#: 边界表：(下界分钟, 上界分钟, 是否要求同一天, 档位)。**含上下界。**
#: 与预设的梯形表同构，只是写成可执行的数据，方便测试逐行核对。
BUCKET_ROWS: tuple[tuple[int, int, bool | None, str], ...] = (
    (0, 120, True, "now"),
    (121, 1440, True, "today"),
    (0, 1440, False, "days"),
    (1441, 7200, None, "days"),
    (7201, 30240, None, "weeks"),
    (30241, 86400, None, "months"),
    (86401, 259200, None, "seasons"),
    (259201, 2628000, None, "years"),
    (2628001, None, None, "old"),
)


def bucket_of(diff: int | None, *, same_date: bool) -> str:
    """分钟差 → 档位名。`None` → `unknown`。"""
    if diff is None:
        return "unknown"
    if diff < 0:
        return "unknown"
    for lo, hi, need_same_day, group in BUCKET_ROWS:
        if diff < lo:
            continue
        if hi is not None and diff > hi:
            continue
        if need_same_day is True and not same_date:
            continue
        if need_same_day is False and same_date:
            continue
        return group
    return "unknown"


def display_of(diff: int | None, group: str) -> str:
    """档内展示串：`45m` / `3h20m` / `5d3h20m` / `记不清时间`。

    ⚠️ 与预设一致：**today 档写成 h+m，days 及以上写成 d+h+m，0 也要写出来**
    （`3h0m` 而不是 `3h`），否则读起来像模板没填。
    """
    if diff is None or group == "unknown":
        return "记不清时间"
    if group == "now":
        return f"{diff}m"
    if group == "today":
        return f"{diff // 60}h{diff % 60}m"
    d = diff // 1440
    h = (diff % MINUTES_PER_DAY) // 60
    m = diff % 60
    return f"{d}d{h}h{m}m"


def time_words(group: str) -> tuple[str, ...]:
    """这一档推荐的时间词；不认识的档位给 unknown 的那组。"""
    return TIME_WORDS.get(group, TIME_WORDS["unknown"])


def describe(now: GameTime, then: GameTime) -> tuple[int | None, str, str]:
    """一步到位：`(分钟差, 档位, 展示串)`。"""
    diff = diff_minutes(now, then)
    group = bucket_of(diff, same_date=same_day(now, then))
    return diff, group, display_of(diff, group)