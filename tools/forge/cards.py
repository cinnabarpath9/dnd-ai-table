# coding=utf-8
"""酒馆角色卡 → 草稿。

支持的输入：

- 卡面 JSON（V1 平铺 / V2、V3 的 `data` 嵌套都认）；
- 卡面 PNG（SillyTavern 把 JSON 塞在 `chara` 的 tEXt 块里，base64）；
- 可选的世界书 JSON（`character_book` 内嵌，或单独导出的 `entries`）。

映射口径（**不编内容**）：

| 卡面字段 | 变成什么 |
|---|---|
| `name` | 剧本标题的后缀 + 一个 NPC |
| `scenario` | 开场场景的 `situation`（原文） |
| `first_mes` | 开场简报的「在场」（原文摘句） |
| `creator_notes` / `system_prompt` | 简报的「离场」候选（仍可能太泛，doctor 会报） |
| `character_book.entries` | 世界书的「背景线」（`threads`，原文摘要） |
| 其它（personality / mes_example …） | 不猜：留在 `meta.card` 里备查 |

**不做的**：不把卡面里的"性格描述"编成战斗数值，不替作者造线索/伏笔——
那些必须由作者补，`doctor()` 会逐条报缺什么。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import struct
import zlib
from typing import Any

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: 单个文本块解压后的上限（4 MB）：超过就当坏块跳过
MAX_TEXT_BLOCK = 4 * 1024 * 1024
#: 单张卡文件上限（CLI 也守，别把 2GB 图一次读进内存；外审 B10）
MAX_CARD_BYTES = 64 * 1024 * 1024
CARD_KEYS = ("spec", "spec_version", "data", "name", "description",
             "personality", "scenario", "first_mes", "mes_example",
             "creator_notes", "system_prompt", "post_history_instructions",
             "alternate_greetings", "character_book", "tags", "creator",
             "character_version", "extensions")


# --------------------------------------------------------------------------- #
# 读卡
# --------------------------------------------------------------------------- #
def _png_text_chunks(raw: bytes) -> dict[str, str]:
    """把 PNG 里的 tEXt / iTXt 块读成 {关键字: 文本}（不碰像素）。"""
    if not raw.startswith(PNG_MAGIC):
        raise ValueError("不是 PNG 文件")
    out: dict[str, str] = {}
    pos = len(PNG_MAGIC)
    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        kind = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"tEXt":
            key, _, text = body.partition(b"\x00")
            out[key.decode("latin-1")] = text.decode("utf-8", "replace")
        elif kind == b"iTXt":
            key, _, rest = body.partition(b"\x00")
            try:
                comp_flag, _method = rest[0], rest[1]
                rest = rest[2:]
                _lang, _, rest = rest.partition(b"\x00")
                _trans, _, text = rest.partition(b"\x00")
                if comp_flag:
                    # 解压上限：卡面的文本块本来就不该超过几 MB。
                    # 没有 max_length 时，8MB 上传可以解出 GB 级内存（外审 B1）。
                    text = zlib.decompress(text, bufsize=0,
                                           max_length=MAX_TEXT_BLOCK)
                out[key.decode("latin-1")] = text.decode("utf-8", "replace")
            except (IndexError, ValueError, zlib.error):
                # zlib.error 不是 ValueError 的子类（实测），单独列出来；
                # 坏块/压缩炸弹一律跳过，不让它把整个转换打崩。
                continue
        elif kind == b"IEND":
            break
    return out


def load_card_bytes(raw: bytes, *, name: str = "") -> dict:
    """从字节读卡（JSON 或 PNG），统一返回**平铺**的卡数据（V2/V3 取 `data`）。"""
    if raw.startswith(PNG_MAGIC):
        chunks = _png_text_chunks(raw)
        payload = chunks.get("chara") or chunks.get("ccv3")
        if not payload:
            raise ValueError("PNG 里没有 `chara` 数据块（不是酒馆卡？）")
        try:
            doc = json.loads(base64.b64decode(payload).decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"卡面数据解不开：{exc}") from exc
    else:
        try:
            doc = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"卡面不是合法 JSON（{name or '未命名'}）：{exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError("卡面根必须是对象")
    if isinstance(doc.get("data"), dict):
        merged = dict(doc["data"])
        merged.setdefault("spec", doc.get("spec"))
        merged.setdefault("spec_version", doc.get("spec_version"))
        return merged
    return doc


def load_card(path: str) -> dict:
    """读文件形式的卡面（PNG 或 JSON）。"""
    size = os.path.getsize(os.fspath(path))
    if size > MAX_CARD_BYTES:
        raise ValueError(f"卡文件太大（{size / 1024 / 1024:.1f}MB > "
                         f"{MAX_CARD_BYTES // (1024 * 1024)}MB）：{path}")
    with open(os.fspath(path), "rb") as f:
        raw = f.read()
    return load_card_bytes(raw, name=os.path.basename(os.fspath(path)))


def load_worldbook(path: str) -> list[dict]:
    """读单独导出的世界书：`{entries:[...]}` / `{entries:{...}}` / 直接数组。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        doc = json.load(f)
    entries: Any = doc
    if isinstance(doc, dict):
        entries = doc.get("entries", doc)
    if isinstance(entries, dict):
        entries = list(entries.values())
    if not isinstance(entries, list):
        raise ValueError("世界书格式不认识（要 entries 数组或对象）")
    return [e for e in entries if isinstance(e, dict)]


def worldbook_entries(card: dict) -> list[dict]:
    """卡内嵌世界书（`character_book.entries`）。"""
    book = card.get("character_book")
    if not isinstance(book, dict):
        return []
    entries = book.get("entries")
    if isinstance(entries, dict):
        entries = list(entries.values())
    return [e for e in entries or [] if isinstance(e, dict)]


# --------------------------------------------------------------------------- #
# 转草稿
# --------------------------------------------------------------------------- #
def _clip(text: Any, limit: int = 160) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _entry_title(entry: dict, fallback: str) -> str:
    for key in ("comment", "name", "title"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    keys = entry.get("keys") or entry.get("key") or []
    if isinstance(keys, str):
        keys = [keys]
    return "、".join(str(k) for k in keys if str(k).strip()) or fallback


def _norm(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip().lower()


def _keys_of(entry: dict) -> list[str]:
    keys = entry.get("keys") or entry.get("key") or []
    if isinstance(keys, str):
        keys = [keys]
    return [str(k).strip() for k in keys if str(k).strip()]


def merge_entries(entries: list[dict]) -> list[dict]:
    """把**同一对象的多个称呼**合并成一条背景线（别名合并）。

    规则（简单、可回归，不做模糊匹配）：
      - 两个条目的 `keys` 有交集 → 同一条；
      - 条目的 `comment`（标题）等于/包含在另一条的 keys 里 → 同一条；
      - 合起来：标题取最长的 comment，正文去重拼接，别名记在 summary 前缀。

    为什么要合并：卡面里同一个 NPC 常写成「老周 / 摆渡人 / 周伯」三条，
    不合并的话世界书会碎成一堆重复项，DM 每次都得自己判断"这是不是一个人"。
    """
    groups: list[dict] = []
    for entry in entries:
        # 外审 B6：`enabled:false` 的条目在**所有**入口都不该出现
        # （旧实现只在 draft_from_card 里滤，候选线索那条路漏了）。
        if str(entry.get("enabled", True)).lower() in ("false", "0"):
            continue
        keys = set(_norm(k) for k in _keys_of(entry))
        keys |= {_norm(entry.get("comment") or entry.get("name") or "")}
        keys.discard("")
        if not keys:
            continue
        target = None
        for group in groups:
            if group["keys"] & keys:
                target = group
                break
        if target is None:
            target = {"keys": set(), "entries": [], "aliases": set()}
            groups.append(target)
        target["keys"] |= keys
        target["entries"].append(entry)
        target["aliases"] |= keys

    out: list[dict] = []
    for group in groups:
        entries_in = group["entries"]
        title = max((str(e.get("comment") or e.get("name") or "").strip()
                     for e in entries_in), key=len, default="")
        if not title:
            title = "、".join(sorted(group["aliases"]))[:40]
        bodies: list[str] = []
        for entry in entries_in:
            body = _clip(entry.get("content"), 140)
            if body and body not in bodies:
                bodies.append(body)
        if not bodies:
            continue
        alias_text = "、".join(sorted(a for a in group["aliases"]
                                      if a != _norm(title)))
        summary = " ｜ ".join(bodies)
        if alias_text:
            summary = f"别名：{alias_text} ｜ {summary}"
        out.append({"title": title, "summary": summary,
                    "aliases": group["aliases"]})
    return out


#: 只有**可被查的痕迹**才算候选线索。地名/器物名（雾、钟、灯、绳）不算——
#: 否则"雾渡口"会因为一个「雾」字被建议成线索，噪声比信号多。
CLUE_HINTS = (
    ("investigation", ("刀痕", "字迹", "账本", "缺页", "刻痕", "记号",
                       "机关", "锁", "账", "名单")),
    ("perception", ("蜡", "脚印", "血迹", "痕迹", "气味", "声音", "影子")),
)


def suggest_clues(groups: list[dict]) -> list[dict]:
    """从合并后的背景线里挑**候选线索**（只建议，不写进草稿）。

    `dc` 是**建议值**，不是裁定：作者确认后才该进 `clues`。
    我们不自作主张把世界书全变成线索——那会让玩家"随便看就全知道"。
    """
    out: list[dict] = []
    for group in groups:
        text = group["summary"]
        if "｜" in text:
            text = text.split("｜", 1)[1].strip()
        hit_skill, hit_words = None, []
        for skill, words in CLUE_HINTS:
            found = [w for w in words if w in text]
            if found:
                hit_skill, hit_words = skill, found
                break
        if not hit_skill:
            continue
        sentence = re.split(r"[。！？]", text)[0].strip()
        if len(sentence) < 8:
            continue
        out.append({
            "text": sentence[:80],
            "skill": hit_skill,
            "suggested_dc": 14 if hit_skill == "investigation" else 12,
            "why": "命中 " + "、".join(hit_words[:3]),
            "from": group["title"],
        })
        if len(out) >= 6:
            break
    return out


def draft_from_card(card: dict, *, extra_entries: list[dict] | None = None,
                    title: str = "") -> dict:
    """卡面 → 草稿（一场开场 + 世界书背景线 + 一个 NPC）。"""
    name = str(card.get("name") or "").strip() or "（未命名角色）"
    scenario = _clip(card.get("scenario"), 400)
    first = _clip(card.get("first_mes"), 240)
    notes = _clip(card.get("creator_notes") or card.get("system_prompt"), 200)
    situation = scenario or first
    if not situation:
        situation = f"（卡面《{name}》没有 scenario / first_mes：开场要作者补）"

    entries = [e for e in (list(worldbook_entries(card))
                            + list(extra_entries or []))
               if str(e.get("enabled", True)).lower() not in ("false", "0")]
    threads = []
    for i, group in enumerate(merge_entries(entries), start=1):
        thread = {
            "id": f"t{i}",
            "title": group["title"],
            "kind": ("main" if _norm(name) in group["aliases"] or i == 1
                     else "side"),
            "summary": group["summary"],
        }
        threads.append(thread)
        if len(threads) >= 12:
            break

    return {
        "title": title or f"角色卡《{name}》导入",
        "meta": {
            "source": f"card:{name}",
            "players": "4",
            "hours": "待定",
            "card": {k: card.get(k) for k in
                     ("spec", "spec_version", "creator", "character_version",
                      "tags") if card.get(k)},
        },
        "npcs": [{"name": name, "role": _clip(card.get("personality"), 60)
                  or "卡面主角"}],
        "threads": threads,
        "promises": [],
        "scenes": [{
            "id": "s1",
            "title": f"{name}·开场",
            "situation": situation,
            "brief": {
                "present": first or "（待补：开场谁在场、在做什么）",
                "span": "开场这一刻",
                "offstage": notes or "（待补：这一场之外的人/事）",
            },
            "world": {},
            "clues": [],
            "promises": [],
        }],
    }