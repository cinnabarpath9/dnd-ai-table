# coding=utf-8
"""模型层的接口、假模型与座位。

**唯一的模型出口。** 真模型（P4）与假模型（回归/demo）走同一份接口，
所以"同一套循环，假模型跑一遍、真模型跑一遍"是同一段代码。

    client.py   ModelRequest / ModelReply / ModelClient / FakeModel
                + 意图解析与校验（parse_intent_reply）
    seats.py    牌桌座位：TableClient（按座位分发）+ HumanSeat（真人玩家）
"""

from .client import (
    FakeModel, Intent, ModelClient, ModelError, ModelReply, ModelRequest,
    RealModel, RecordedCall, parse_intent_reply,
)
from .seats import HumanSeat, TableClient

__all__ = [
    "ModelClient", "ModelRequest", "ModelReply", "ModelError",
    "FakeModel", "RealModel", "RecordedCall", "Intent", "parse_intent_reply",
    "TableClient", "HumanSeat",
]