# coding=utf-8
"""记忆层（P5-G）：游戏内时钟 / 记忆条目 / 按内容召回 / 时间分组渲染。

零模型调用、零网络。对外只暴露这几样：

    GameTime, parse_game_time            —— 游戏内时刻与算差
    MemoryStore, MemoryEntry             —— 记忆库与一条记忆（可落盘）
    Query, recall, render, recall_text   —— 按内容召回 + 拼提示词块
    match_text                           —— 复盘用的对账表（不进提示词）
"""

from .clock import (
    BUCKET_ROWS, GROUP_LABELS, GROUP_ORDER, PERIOD_HOURS, TIME_WORDS,
    ClockError, GameTime, bucket_of, describe, diff_minutes, display_of,
    known_periods, parse_clock, parse_date, parse_game_time, time_words,
)
from .entries import (
    MEMORY_FORMAT, MEMORY_VERSION, MemoryEntry, MemoryError, MemoryStore,
)
from .recall import (
    ALIASES, Query, RecallResult, Recalled, build_idf, canonical, match_text,
    recall, recall_text, render, score_entry,
)

__all__ = [
    "BUCKET_ROWS", "GROUP_LABELS", "GROUP_ORDER", "PERIOD_HOURS", "TIME_WORDS",
    "ClockError", "GameTime", "bucket_of", "describe", "diff_minutes",
    "display_of", "known_periods", "parse_clock", "parse_date", "parse_game_time",
    "time_words",
    "MEMORY_FORMAT", "MEMORY_VERSION", "MemoryEntry", "MemoryError", "MemoryStore",
    "Query", "RecallResult", "Recalled", "match_text", "recall", "recall_text",
    "render", "score_entry", "ALIASES", "canonical", "build_idf",
]