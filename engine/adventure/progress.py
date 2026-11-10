# coding=utf-8
"""跨局记忆：伏笔提过几次、兑现了没有、下一局还欠着什么。

为什么单独一个文件（不写回 `adventure.json`）：

    · `adventure.json` 是**作者**写的结构（哪几场、每场简报、欠什么）；
    · 这个文件是**玩出来**的历史（哪场提过、哪场兑现、跑了哪些场）；
    · 混在一起 = 改剧本会冲掉历史，或者复盘时历史污染剧本。

所以：**剧本只读，进度单独落盘**，默认放在剧本旁边：
`adventures\\哨塔的拖痕.json` → `adventures\\哨塔的拖痕.progress.json`。

三条口径：

1. **一提一笔**：一场里引用到的伏笔记一笔（同一场重复跑不重复记 ——
   "这一场提过它"是事实，不是尝试次数）；
2. **兑现要明确**：场景里写 `pays`，或者用 `close()` 手动关；
   跑完一场不会"猜"哪条伏笔算收了；
3. **坏档不静默**：格式/版本不对直接报错，不把新档当旧档读、不丢历史。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .model import Adventure, Promise, Scene

__all__ = [
    "PROGRESS_FORMAT", "PROGRESS_VERSION", "ProgressError", "Beat",
    "PromiseProgress", "CampaignProgress", "progress_path_for",
]

PROGRESS_FORMAT = "dnd-campaign-progress"
PROGRESS_VERSION = 1


class ProgressError(ValueError):
    """进度文件的格式/版本不对。"""

    def __init__(self, msg: str):
        super().__init__(msg)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def progress_path_for(adventure_path: os.PathLike | str) -> str:
    """默认进度文件：和剧本同目录、同名前缀。"""
    base, _ext = os.path.splitext(os.fspath(adventure_path))
    return base + ".progress.json"


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #

@dataclass
class Beat:
    """一笔「这一场提过它」。"""
    scene: str
    note: str = ""
    at: str = ""

    def line(self) -> str:
        when = f"（{self.at}）" if self.at else ""
        return f"{self.scene}：{self.note or '提过'}{when}"


@dataclass
class PromiseProgress:
    beats: list[Beat] = field(default_factory=list)
    closed: bool = False
    closed_note: str = ""
    closed_at: str = ""

    def last_scene(self) -> str:
        return self.beats[-1].scene if self.beats else ""


class CampaignProgress:
    """一局/一战役的跨局记忆。**可以没有文件**（纯内存，测试用）。"""

    def __init__(self, *, adventure: str = "", path: str | None = None):
        self.adventure = str(adventure)
        self.path = os.fspath(path) if path else None
        self.scenes_done: list[str] = []
        self.promises: dict[str, PromiseProgress] = {}
        #: 这一章的开场长休做过没有（Q35 / 外审 B4）。进度文件里持久化，
        #: 同一章断档重开不会白送第二次满血。
        self.rest_done: bool = False

    # ---------- 记录 ----------

    def record_scene(self, scene: Scene) -> list[str]:
        """跑完一场记一笔。返回**这次真的新增**的伏笔 id（重复跑不再记）。

        每笔带**内容**：剧本写了 `beat_notes[pid]` 就用它（"这一场推进成了什么样"）；
        没写才退化成「场景《标题》」—— 复盘时至少看得出是哪一场。
        """
        new: list[str] = []
        notes = getattr(scene, "beat_notes", None) or {}
        default_note = f"场景《{scene.title}》"
        for pid in scene.promises:
            pp = self.promises.setdefault(pid, PromiseProgress())
            if any(b.scene == scene.id for b in pp.beats):
                continue
            pp.beats.append(Beat(scene=scene.id,
                                 note=str(notes.get(pid) or default_note),
                                 at=_now()))
            new.append(pid)
        for pid in getattr(scene, "pays", ()):
            if self.close(pid, note=f"在场景《{scene.title}》兑现", save=False):
                if pid not in new:
                    new.append(pid)
        changed = bool(new)
        if scene.id not in self.scenes_done:
            self.scenes_done.append(scene.id)
            changed = True
        if changed:
            self.save()
        return new

    def close(self, promise_id: str, *, note: str = "", save: bool = True) -> bool:
        """把一条伏笔标成已兑现。已经关过返回 False。"""
        pp = self.promises.setdefault(promise_id, PromiseProgress())
        if pp.closed:
            return False
        pp.closed = True
        pp.closed_note = note or "手动标记已兑现"
        pp.closed_at = _now()
        if save:
            self.save()
        return True

    # ---------- 查询 ----------

    def beats_of(self, promise_id: str) -> list[Beat]:
        pp = self.promises.get(promise_id)
        return list(pp.beats) if pp else []

    def is_closed(self, promise_id: str) -> bool:
        pp = self.promises.get(promise_id)
        return bool(pp and pp.closed)

    def unresolved(self, adv: Adventure) -> list[Promise]:
        """**还欠着的**：剧本里是 open，且进度里没关。

        `paid` / `dropped` 的剧本状态优先 —— 作者说了收掉的，进度里不重复算。
        """
        out = []
        for p in adv.promises:
            if p.status != "open":
                continue
            if self.is_closed(p.id):
                continue
            out.append(p)
        return out

    def debt_text(self, adv: Adventure, *, limit: int = 6) -> str:
        """下一局给 DM 看的欠账块。没有欠账返回空串。"""
        owed = self.unresolved(adv)
        if not owed:
            return ""
        lines = ["【上一局欠着的伏笔 · 只有 DM 看得到，别直接念给玩家】"]
        for p in owed[:limit]:
            beats = self.beats_of(p.id)
            if beats:
                last = beats[-1]
                tail = (f"（提过 {len(beats)} 次，最后一次在「{last.scene}」"
                        + (f"：{last.note}" if last.note else "") + "）")
            else:
                tail = "（上一局没提到）"
            lines.append(f"· {p.id} {p.title}{tail}"
                         + (f" —— {p.summary}" if p.summary else ""))
        if len(owed) > limit:
            lines.append(f"· 还有 {len(owed) - limit} 条没列（先别一次全倒给 DM）")
        return "\n".join(lines)

    def summary(self, adv: Adventure | None = None) -> str:
        closed = sum(1 for pp in self.promises.values() if pp.closed)
        bits = [f"已跑 {len(self.scenes_done)} 场"]
        if adv is not None:
            owed = len(self.unresolved(adv))
            bits.append(f"伏笔 {len(adv.promises)} 条：已回收 {closed}｜欠着 {owed}")
            for p in self.unresolved(adv):
                beats = self.beats_of(p.id)
                bits.append(f"  · {p.id} {p.title}"
                            f"（{'提过 %d 次' % len(beats) if beats else '没提过'}）")
        else:
            bits.append(f"伏笔记录 {len(self.promises)} 条（已回收 {closed}）")
        if self.path:
            bits.append(f"记忆文件：{self.path}")
        return "\n".join(bits)

    # ---------- 存档 ----------

    def to_dict(self) -> dict:
        return {
            "format": PROGRESS_FORMAT,
            "version": PROGRESS_VERSION,
            "adventure": self.adventure,
            "scenes_done": list(self.scenes_done),
            "rest_done": bool(self.rest_done),
            "promises": {
                pid: {
                    "beats": [{"scene": b.scene, "note": b.note, "at": b.at}
                              for b in pp.beats],
                    "closed": bool(pp.closed),
                    "closed_note": pp.closed_note,
                    "closed_at": pp.closed_at,
                }
                for pid, pp in self.promises.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict, *, path: str | None = None) -> "CampaignProgress":
        if not isinstance(d, dict):
            raise ProgressError(f"进度文件根必须是对象，拿到 {type(d).__name__}")
        if d.get("format") != PROGRESS_FORMAT:
            raise ProgressError(f"不是本项目的进度文件（format={d.get('format')!r}，"
                                f"需要 {PROGRESS_FORMAT!r}）")
        if d.get("version") != PROGRESS_VERSION:
            raise ProgressError(f"进度文件版本 {d.get('version')!r} 与引擎 "
                                f"{PROGRESS_VERSION} 不匹配")
        p = cls(adventure=str(d.get("adventure") or ""), path=path)
        p.scenes_done = [str(x) for x in (d.get("scenes_done") or [])]
        p.rest_done = bool(d.get("rest_done"))
        raw = d.get("promises") or {}
        if not isinstance(raw, dict):
            raise ProgressError("promises 必须是对象")
        for pid, v in raw.items():
            if not isinstance(v, dict):
                raise ProgressError(f"伏笔 {pid!r} 的记录必须是对象")
            beats = []
            for i, b in enumerate(v.get("beats") or []):
                if not isinstance(b, dict) or not b.get("scene"):
                    raise ProgressError(f"伏笔 {pid!r} 的第 {i} 笔记录格式不对")
                beats.append(Beat(scene=str(b["scene"]), note=str(b.get("note") or ""),
                                  at=str(b.get("at") or "")))
            p.promises[str(pid)] = PromiseProgress(
                beats=beats, closed=bool(v.get("closed")),
                closed_note=str(v.get("closed_note") or ""),
                closed_at=str(v.get("closed_at") or ""))
        return p

    def save(self, path: str | None = None) -> str:
        """原子写盘（同目录临时文件 + `os.replace`）。没配路径就只留在内存。"""
        target = os.fspath(path) if path else self.path
        if not target:
            return ""
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        parent = os.path.dirname(os.path.abspath(target))
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".dnd-progress-", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, target)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise ProgressError(f"写进度文件失败：{target!r}（{exc}）") from exc
        self.path = target
        return target

    @classmethod
    def load(cls, path: os.PathLike | str, *, missing_ok: bool = True,
             adventure: str = "") -> "CampaignProgress":
        """读进度。文件不存在 = 新的一局（`missing_ok=True` 时返回空进度）。"""
        path = os.fspath(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            if missing_ok and isinstance(exc, FileNotFoundError):
                return cls(adventure=adventure, path=path)
            raise ProgressError(f"读不到进度文件 {path!r}：{exc}") from exc
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise ProgressError(f"进度文件不是合法 JSON：{path!r}（{exc}）") from exc
        p = cls.from_dict(doc, path=path)
        if not p.adventure:
            p.adventure = adventure
        return p