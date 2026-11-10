# coding=utf-8
"""锻造台的统一转换入口：CLI 与网页都只调这里。

为什么要有它（外审 D）：以前 `cli.py` 与 `serve.py` 各写一套"按 kind 选适配器 + doctor"，
行为已经分叉过一次（网页不认 GB18030，CLI 认）。加新适配器也只该改一处。

约定：
- 只吃 **bytes**（网页上传的是字节、CLI 读进来也是字节），编码在 `decode_text()` 里统一试；
- 返回 `{draft, report, summary, suggestions}`；不落盘、不打印（落盘/打印是调用方的事）。
"""
from __future__ import annotations

from typing import Any

from .cards import (draft_from_card, load_card_bytes, merge_entries,
                    suggest_clues, worldbook_entries)
from .contract import doctor
from .from_markdown import parse_markdown
from .from_novel import draft_from_text

__all__ = ["decode_text", "convert"]

#: 老中文小说常见 GB18030；先 UTF-8（带 BOM 也认），再退。
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")


def decode_text(data: bytes) -> str:
    """把上传/读取的字节解成文本；三种编码都失败就明确抛，不瞎替换。"""
    for enc in ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError("读不出文本编码（试过 utf-8-sig / utf-8 / gb18030）")


def convert(kind: str, name: str, data: bytes, *,
            title: str = "", extra_entries: list[dict] | None = None) -> dict[str, Any]:
    """按 `kind` 转草稿并跑 `doctor`。不认识 kind 直接抛 ValueError。"""
    clues: list[dict] = []
    if kind == "card":
        card = load_card_bytes(data, name=name)
        draft = draft_from_card(card, extra_entries=extra_entries, title=title)
        entries = worldbook_entries(card) + list(extra_entries or [])
        clues = suggest_clues(merge_entries(entries))
        summary = (f"导入卡面 {card.get('name') or '（无名）'}："
                   f"背景线 {len(draft['threads'])} 条 / NPC {len(draft['npcs'])} 个")
    elif kind == "md":
        draft = parse_markdown(decode_text(data))
        summary = f"模板解析：{len(draft['scenes'])} 场"
    elif kind == "novel":
        draft = draft_from_text(decode_text(data), title=title or "", source=name)
        summary = f"切成 {len(draft['scenes'])} 场粗稿（简报/线索/伏笔待补）"
    else:
        raise ValueError(f"不认识的类型：{kind}")
    report = doctor(draft)
    return {"draft": draft, "report": report, "summary": summary,
            "suggestions": clues}