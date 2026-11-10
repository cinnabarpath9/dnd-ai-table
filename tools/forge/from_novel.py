# coding=utf-8
"""小说/自由文本 → **粗稿**（启发式，不调用模型）。

原则：这一层只做"把长文切成场次 + 把开头一段当局面"，**不编内容**。
切出来的粗稿一定缺简报/线索/伏笔 —— 那些留给作者或上层模型补，
`doctor()` 会逐条报出来。

为什么不做"一键生成完整剧本"：一次抽取就编出简报/线索/伏笔，等于替作者
把世界写死；而小说里的因果往往跨几十段，硬抽出来的东西看着完整、跑起来不自洽。
所以我们只交"半成品 + 缺什么"，剩下的让作者（或接模型的 Agent）逐场补。
"""
from __future__ import annotations

import re

CHAPTER = re.compile(
    r"^\s*(?:第[零一二三四五六七八九十百千万\d]+[章节回卷篇]|"
    r"Chapter\s+\d+|CHAPTER\s+\d+)\b.*$", re.M)
PARA = re.compile(r"\n\s*\n")
SENTENCE_END = re.compile(r"(?<=[。！？!?])")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def _scene_situation(chunk: str, limit: int = 260) -> str:
    """取这一章的开头当局面：**原文摘句**，不改写。"""
    body = " ".join(line.strip() for line in chunk.splitlines() if line.strip())
    out = ""
    for sentence in SENTENCE_END.split(body):
        if not sentence.strip():
            continue
        out += sentence
        if len(out) >= 80:
            break
    out = out.strip()
    return out[:limit] if len(out) > limit else out


def draft_from_text(text: str, *, title: str = "", source: str = "",
                    max_scenes: int = 24) -> dict:
    """小说文本 → 草稿（场次骨架）。`source` 记在 meta 里，便于复盘「从哪来」。"""
    raw = _clean(text)
    marks = list(CHAPTER.finditer(raw))
    chunks: list[tuple[str, str]] = []
    if marks:
        for i, m in enumerate(marks[:max_scenes]):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            head = m.group(0).strip()
            chunks.append((head, raw[m.end():end]))
    else:
        for i, para in enumerate(PARA.split(raw)[:max_scenes]):
            chunks.append((f"第 {i + 1} 段", para))

    scenes: list[dict] = []
    for i, (head, body) in enumerate(chunks, start=1):
        situation = _scene_situation(body)
        if len(re.sub(r"\s", "", situation)) < 8:
            continue
        scenes.append({
            "id": f"s{i}",
            "title": head[:40] or f"第 {i} 场",
            "situation": situation,
            "brief": {"present": "", "span": "", "offstage": ""},
            "world": {},
            "clues": [],
            "promises": [],
        })
    return {
        "title": title or "（未命名：从文本转入）",
        "meta": {"source": source or "text"},
        "scenes": scenes,
        "promises": [],
        "threads": [],
        "npcs": [],
    }


def read_text_file(path: str) -> str:
    """读 txt/md：先按 UTF-8，失败再按 GB18030（老中文小说常见）。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"读不出文本编码（试过 utf-8 / gb18030）：{path}")