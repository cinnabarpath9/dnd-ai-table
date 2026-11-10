# coding=utf-8
"""不可信文本的收口：玩家台词/上传文本进提示词前的清洗。

借 NeuroBook 的分层思路：**台词是数据，不是指令**。玩家（或上游模型被污染后）
写进来的"忽略以上""你现在是…"不能改变 DM/座位的行为。
这里只做**小而可解释**的处理，不假装能识别所有注入：

- 去掉行首的角色标记（`system:` / `assistant:` / `###` / `<|...|>`）；
- 去掉常见的"改指令"句式所在行（中英各几条），其余行保留；
- 压掉控制字符与连续空行，限长。
"""
from __future__ import annotations

import re

__all__ = ["sanitize_player_text", "INSTRUCTION_MARKERS"]

#: 这些句式出现在台词里时，整行丢弃（只丢这一行，不连坐整段）。
INSTRUCTION_MARKERS = (
    "忽略以上", "忽略之前", "无视以上", "无视之前", "忘掉之前",
    "你现在是", "从现在起你是", "系统提示", "系统指令", "开发者模式",
    "ignore previous", "ignore all previous", "disregard the above",
    "you are now", "system prompt", "developer mode", "jailbreak",
)

_ROLE_LINE = re.compile(r"^\s*(?:system|assistant|user|developer)\s*[:：]", re.I)
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_ANGLE = re.compile(r"<\|[^>]*\|>")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_player_text(text: str, *, limit: int = 200) -> str:
    """清洗一段玩家台词：**保留内容**，只砍掉像"改指令"的行与标记。"""
    raw = str(text or "")
    raw = _ANGLE.sub("", raw)
    raw = _CONTROL.sub("", raw)
    kept: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if _ROLE_LINE.match(s) or _FENCE.match(s):
            continue
        low = s.lower()
        if any(m.lower() in low for m in INSTRUCTION_MARKERS):
            continue
        kept.append(s)
    out = " ".join(kept)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out[:max(0, int(limit))]