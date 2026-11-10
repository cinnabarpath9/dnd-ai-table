# coding=utf-8
"""记忆条目（AM）与记忆库 —— 「按内容召回」的数据底座（P5-G）。

一条 AM = **一件事**，不是一句话。它必须能回答：

    什么时候发生的（游戏内时间跨度）／谁在场／在哪里／属于哪条线索／谁有权知道

这五样正是"按内容挑记忆"要用的键。**没有时间也就没有"多久以前"**，
所以时间缺失是允许的（`unknown` 档），但摘要、可见性不能缺失。

⚠️ 四条口径（与项目其它部分的纪律一致）：
    1. **只记已发生的事**：AM 由引擎在"一场跑完/一次结算"后登记，
       不由模型凭想象生成 —— 那会造出"没发生过但很合理"的记忆。
    2. **可见性走 `Visibility`**（public / dm / engine / pc:X），
       与台账同一套判据；`engine` 档连 DM 都不给。
    3. **幂等键**：同一场重复跑不该记两条（`key=scene.id`）。
       与 P5-D 的"一提一笔"同一个道理。
    4. **空泛摘要不收**：`推进关系` 这种当摘要 = 没写，直接报错。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field

from ..state.visibility import (
    DM, ENGINE, PUBLIC, Visibility, pc,
)
from .clock import GameTime, parse_date, parse_clock

__all__ = [
    "MEMORY_FORMAT", "MEMORY_VERSION", "MemoryError", "MemoryEntry", "MemoryStore",
    "GENERIC_SUMMARIES",
]

MEMORY_FORMAT = "dnd-campaign-memory"
MEMORY_VERSION = 1

#: 拿来当摘要就等于没写的词（与剧本层的「空泛词」同一张表的口径）。
GENERIC_SUMMARIES = (
    "推进关系", "制造冲突", "埋下伏笔", "增加紧张", "丰富剧情",
    "推动剧情", "制造悬念", "加深感情", "展现性格", "引出后续",
    "一些事情", "发生了事情", "不详", "待定",
)

_EMPTY_OK = {"", "无", "没有", "略"}


class MemoryError(ValueError):
    """记忆条目/记忆库不成立。**逐条说原因，不静默丢弃。**"""


def _text(v, what: str) -> str:
    s = "" if v is None else str(v).strip()
    if not s:
        raise MemoryError(f"{what}不能为空")
    return s


def _tuple(v, what: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        v = [v]
    out = []
    for x in v:
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
    if len(out) != len(set(out)):      # pragma: no cover - 上面已去重
        raise MemoryError(f"{what}有重复项")
    return tuple(out)


def _as_visibility(v) -> Visibility:
    if isinstance(v, Visibility):
        return v
    if isinstance(v, str):
        return Visibility(v)
    return Visibility(*v)


# --------------------------------------------------------------------------- #
# 一条记忆
# --------------------------------------------------------------------------- #

@dataclass
class MemoryEntry:
    """一件已经发生过的事。"""
    id: str
    summary: str
    start: GameTime | None = None
    end: GameTime | None = None
    people: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    #: 线索标签：剧本的 thread id / promise id，或 `clue:xxx` 这类显式线索。
    #: **这是"按内容挑"最强的信号**，比"同一个地名"强得多。
    tags: tuple[str, ...] = ()
    visible_to: Visibility = field(default_factory=lambda: Visibility(PUBLIC))
    #: 来自哪（场景 id / manual / 战斗轮）。排查用，不参与召回打分。
    source: str = ""
    #: 幂等键（场景 id）。同键不重复登记。
    key: str = ""
    #: 是否"幕后"（不在场的人做的事）。DM 知道，玩家不知道；
    #: 登记时用 `visible_to="dm"` 表达，这个字段只是给人读的备注。
    offstage: bool = False

    def __post_init__(self) -> None:
        if not str(self.id or "").strip():
            raise MemoryError("记忆条目缺 id")
        self.summary = _text(self.summary, "摘要")
        self._check_summary()
        self.people = _tuple(self.people, "在场人物")
        self.places = _tuple(self.places, "地点")
        self.tags = _tuple(self.tags, "线索标签")
        self.visible_to = _as_visibility(self.visible_to)
        self.source = "" if self.source is None else str(self.source)
        self.key = "" if self.key is None else str(self.key)

    # ---------- 校验 ----------

    def _check_summary(self) -> None:
        plain = self.summary.strip()
        if plain in _EMPTY_OK:
            raise MemoryError(f"摘要写了「{plain}」等于没写 —— 要写清发生了什么")
        if plain in GENERIC_SUMMARIES:
            raise MemoryError(
                f"摘要是空泛表述（{plain!r}）—— 写清「谁、在哪、做了什么」")
        # 只剩空泛词/标点也算没写（与剧本层同一条判据）
        stripped = plain
        for w in GENERIC_SUMMARIES:
            stripped = stripped.replace(w, "")
        if not stripped.strip(" \t\r\n，。、；：！？,,.;:!?—-…"):
            raise MemoryError(f"摘要只有空泛词/标点（{plain!r}）—— 等于没写")

    # ---------- 时间 ----------

    @property
    def has_time(self) -> bool:
        return bool(self.start and self.start.known)

    # ---------- 可见性 ----------

    def visible(self, viewer: str) -> bool:
        """**唯一判据**（与台账同一条：DM 除了 engine 档以外都能看）。"""
        return self.visible_to.allows(viewer)

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "summary": self.summary,
            "start": _time_to_dict(self.start),
            "end": _time_to_dict(self.end),
            "people": list(self.people),
            "places": list(self.places),
            "tags": list(self.tags),
            "visible_to": sorted(self.visible_to.tags),
            "source": self.source,
            "key": self.key,
            "offstage": bool(self.offstage),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        if not isinstance(d, dict):
            raise MemoryError(f"记忆条目必须是对象，拿到 {type(d).__name__}")
        try:
            return cls(
                id=str(d.get("id") or ""),
                summary=str(d.get("summary") or ""),
                start=_time_from_dict(d.get("start")),
                end=_time_from_dict(d.get("end")),
                people=tuple(d.get("people") or ()),
                places=tuple(d.get("places") or ()),
                tags=tuple(d.get("tags") or ()),
                visible_to=_as_visibility(list(d.get("visible_to") or ())),
                source=str(d.get("source") or ""),
                key=str(d.get("key") or ""),
                offstage=bool(d.get("offstage")),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, MemoryError):
                raise
            raise MemoryError(f"记忆条目格式不对：{d!r}") from exc


def _time_to_dict(t: GameTime | None) -> dict | None:
    if t is None:
        return None
    return {"date": t.date.isoformat() if t.date else "",
            "minutes": t.minutes}


def _time_from_dict(d) -> GameTime | None:
    if not d:
        return None
    if not isinstance(d, dict):
        raise MemoryError(f"时间字段必须是对象或空，拿到 {type(d).__name__}")
    date_text = str(d.get("date") or "")
    minutes = d.get("minutes")
    return GameTime(
        date=parse_date(date_text),
        minutes=None if minutes is None else int(minutes),
    )


# --------------------------------------------------------------------------- #
# 记忆库
# --------------------------------------------------------------------------- #

class MemoryStore:
    """一局/一战役的记忆库。**纯内存也能用**（不配 path 就不落盘）。"""

    def __init__(self, *, label: str = "", path: str | os.PathLike | None = None):
        self.label = str(label)
        self.path = os.fspath(path) if path else None
        self._entries: list[MemoryEntry] = []
        self._next = 1

    # ---------- 写 ----------

    def record(self, summary: str,
               *, start: GameTime | None = None, end: GameTime | None = None,
               people=(), places=(), tags=(),
               visible_to=PUBLIC, source: str = "", key: str = "",
               offstage: bool = False, save: bool = True) -> MemoryEntry:
        """登记一件事。**同一个 `key` 不重复登记**（重复跑同一场不该多一条）。"""
        key = "" if key is None else str(key).strip()
        if key:
            old = self.by_key(key)
            if old is not None:
                return old
        entry = MemoryEntry(
            id=f"AM{self._next:04d}",
            summary=summary,
            start=start,
            end=end or start,
            people=tuple(people or ()),
            places=tuple(places or ()),
            tags=tuple(tags or ()),
            visible_to=visible_to,
            source=source,
            key=key,
            offstage=bool(offstage),
        )
        self._entries.append(entry)
        self._next += 1
        if save:
            self.save()
        return entry

    # ---------- 读 ----------

    @property
    def entries(self) -> list[MemoryEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(list(self._entries))

    def get(self, memory_id: str) -> MemoryEntry | None:
        for e in self._entries:
            if e.id == memory_id:
                return e
        return None

    def by_key(self, key: str) -> MemoryEntry | None:
        key = str(key or "").strip()
        if not key:
            return None
        for e in self._entries:
            if e.key == key:
                return e
        return None

    def visible_to(self, viewer: str) -> list[MemoryEntry]:
        """`viewer` 能看到的全部记忆（按登记顺序）。"""
        return [e for e in self._entries if e.visible(viewer)]

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        return {
            "format": MEMORY_FORMAT,
            "version": MEMORY_VERSION,
            "label": self.label,
            "next": self._next,
            "entries": [e.to_dict() for e in self._entries],
        }

    @classmethod
    def from_dict(cls, d: dict, *, path: str | os.PathLike | None = None
                  ) -> "MemoryStore":
        if not isinstance(d, dict):
            raise MemoryError(f"记忆库存档必须是对象，拿到 {type(d).__name__}")
        if d.get("format") != MEMORY_FORMAT:
            raise MemoryError(
                f"不是本项目的记忆库（format={d.get('format')!r}，"
                f"需要 {MEMORY_FORMAT!r}）")
        if d.get("version") != MEMORY_VERSION:
            raise MemoryError(
                f"记忆库版本 {d.get('version')!r} 与引擎 {MEMORY_VERSION} 不匹配")
        store = cls(label=str(d.get("label") or ""), path=path)
        rows = d.get("entries") or []
        if not isinstance(rows, list):
            raise MemoryError("记忆库 entries 必须是数组")
        for row in rows:
            store._entries.append(MemoryEntry.from_dict(row))
        used = {e.id for e in store._entries}
        if len(used) != len(store._entries):
            raise MemoryError("记忆库里有重复的 id")
        keys = [e.key for e in store._entries if e.key]
        if len(keys) != len(set(keys)):
            raise MemoryError("记忆库里有重复的幂等键")
        top = d.get("next")
        if isinstance(top, int) and top >= 1:
            store._next = max(int(top), store._next)
        for e in store._entries:
            tail = e.id[2:] if e.id.startswith("AM") else ""
            if tail.isdigit():
                store._next = max(store._next, int(tail) + 1)
        return store

    # ---------- 落盘 ----------

    def save(self, path: str | os.PathLike | None = None) -> str | None:
        """原子写盘（同目录临时文件 + `os.replace`）。没配路径就只留在内存。"""
        target = os.fspath(path) if path else self.path
        if not target:
            return None
        target = os.fspath(target)
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=1)
        fd, tmp = tempfile.mkstemp(prefix=".memory-", suffix=".tmp",
                                   dir=os.path.dirname(os.path.abspath(target)))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return target

    @classmethod
    def load(cls, path: str | os.PathLike, *, missing_ok: bool = True) -> "MemoryStore":
        p = os.fspath(path)
        if not os.path.exists(p):
            if missing_ok:
                return cls(path=p)
            raise MemoryError(f"记忆库文件不存在：{p}")
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except json.JSONDecodeError as exc:
            raise MemoryError(f"记忆库不是合法 JSON（{exc}）：{p}") from exc
        return cls.from_dict(d, path=p)

    # ---------- 自查 ----------

    def summary(self, viewer: str | None = None) -> str:
        rows = self.visible_to(viewer) if viewer else self.entries
        head = f"记忆库「{self.label or '未命名'}」共 {len(self)} 条"
        if viewer:
            head += f"，{viewer} 能看到 {len(rows)} 条"
        out = [head]
        for e in rows:
            when = e.start.iso() if e.has_time else "时间不明"
            tags = f" tags={list(e.tags)}" if e.tags else ""
            out.append(f"  {e.id}｜{when}｜{e.summary}{tags}"
                       f"［{','.join(sorted(e.visible_to.tags))}］")
        return "\n".join(out)