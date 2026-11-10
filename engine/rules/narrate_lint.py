# coding=utf-8
"""叙事文本的**离线体检**（U9 的零成本部分）。

先说清它能做什么、不能做什么：

    ✅ 能测：空/过短/过长、代码围栏、AI 自我暴露、骰面/内部 ID 泄漏、
       秘密原文泄漏、复读同一句；
    ❌ 不能测：写得好不好、有没有画面感、人物像不像 —— 那要真模型 + 人读，
       见 `docs/验收-叙事与真机.md` 的抽样表。

**只报问题，不改正文。** 改正文是"模型重写"或"人改稿"的事；这里悄悄润色
会让复盘对不上原文（和 P0-3 同一个口径）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Issue", "check_narration", "CODES"]

#: 问题代码 → 一句话说明（给日志/测试对拍用）。
CODES = {
    "empty": "空文本",
    "too_short": "太短（不像一段场景描述）",
    "too_long": "太长（超出单次叙事预算）",
    "markdown_fence": "带了代码围栏",
    "meta_leak": "AI/系统自我暴露",
    "dice_leak": "把骰面/掷骰细节写进了正文",
    "id_leak": "把内部编号（AM/选项/单位）写进了正文",
    "secret_leak": "把还没被发现的秘密原文写了出来",
    "repeat": "同一句重复出现",
}

_META = ("作为 ai", "作为ai", "语言模型", "系统提示", "提示词", "json",
         "token", "我不能继续")
_DICE = re.compile(r"(d20|d\d+|\bDC\s*\d+|掷出|骰出|骰面|natural\s*20)", re.I)
_ID = re.compile(r"(AM\d{4}|\[[A-Za-z]\d+\]|\bE\d+\b)")
_FENCE = re.compile(r"```")


@dataclass(frozen=True)
class Issue:
    code: str
    level: str          # P0 = 泄漏/自我暴露；P1 = 长度/复读/格式
    message: str

    def __str__(self) -> str:
        return f"[{self.level}/{self.code}] {self.message}"


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?]", text)
    return [p.strip() for p in parts if p.strip()]


def check_narration(text: str, *, forbidden=(), min_chars: int = 30,
                    max_chars: int = 1200) -> list[Issue]:
    """体检一段要给玩家看的叙事。返回问题清单（空 = 通过）。"""
    s = str(text or "").strip()
    issues: list[Issue] = []
    if not s:
        return [Issue("empty", "P0", "正文是空的")]
    plain = "".join(ch for ch in s if ch not in " \t\r\n")
    if len(plain) < min_chars:
        issues.append(Issue("too_short", "P1",
                            f"只有 {len(plain)} 个实字（至少 {min_chars}）"))
    if len(plain) > max_chars:
        issues.append(Issue("too_long", "P1",
                            f"{len(plain)} 个实字（上限 {max_chars}）"))
    if _FENCE.search(s):
        issues.append(Issue("markdown_fence", "P1", "出现了 ``` 围栏"))
    low = s.lower()
    for m in _META:
        if m in low:
            issues.append(Issue("meta_leak", "P0",
                                f"出现了 AI/系统口吻「{m}」"))
            break
    dm = _DICE.search(s)
    if dm:
        issues.append(Issue("dice_leak", "P0",
                            f"出现了骰面/DC 细节「{dm.group(0)}」—— 正文只该写结果"))
    im = _ID.search(s)
    if im:
        issues.append(Issue("id_leak", "P0",
                            f"出现了内部编号「{im.group(0)}」"))
    for phrase in forbidden:
        p = str(phrase or "").strip()
        if len(p) >= 4 and p in s:
            issues.append(Issue("secret_leak", "P0",
                                f"出现了尚未发现的秘密原文（{len(p)} 字）"))
            break
    seen: set[str] = set()
    for sent in _sentences(s):
        if len(sent) >= 8:
            if sent in seen:
                issues.append(Issue("repeat", "P1", f"同一句重复：「{sent[:20]}」"))
                break
            seen.add(sent)
    return issues