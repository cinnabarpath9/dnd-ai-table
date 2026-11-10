# coding=utf-8
"""把模型输出里的坏数值读成 int —— 唯一口径。

手改存档/账号数据里可能出现字符串、空白、None；这些地方**不能炸**，
也不能各模块各写一份（外审 C：`item_effects._int_or` 与 `loop._num` 是同一件事）。
"""

from __future__ import annotations

from typing import Any


def int_or(value: Any, default: int = 0) -> int:
    """能读成 int 就读，读不了用 `default`。**不抛，也不把 bool 当数字猜。**"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default