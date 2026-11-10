# coding=utf-8
"""战役层（P5-D2）：**跨剧本的欠账**。

一个剧本的进度文件只记它自己的伏笔；"上一章欠的下一章还"需要战役文件：

    adventures/某个战役.campaign.json
    {
      "title": "北境的拖痕",
      "chapters": [
        {"id": "ch1", "title": "哨塔的拖痕", "adventure": "哨塔的拖痕.json"},
        {"id": "ch2", "title": "塔下的买主", "adventure": "塔下的买主.json"}
      ]
    }

口径：

1. **战役文件只列章节，不复制剧本内容**（剧本仍是唯一真相）；
2. 每一章的进度仍是它自己的 `*.progress.json`（玩出来的历史）；
3. 第 N 章开跑时，把**第 1..N-1 章里还 open 且没关的伏笔**汇总成欠账块 ——
   只给 DM，最多 6 条，并带上"上次推进成了什么样"；
4. 章节 id/路径写坏、剧本读不出来 → 直接报错，不猜。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

from .model import Adventure, AdventureError, load_adventure
from .progress import CampaignProgress, progress_path_for

__all__ = [
    "CAMPAIGN_FORMAT", "CAMPAIGN_VERSION", "CampaignError", "Chapter",
    "Campaign", "parse_campaign", "load_campaign", "chapter_path",
    "previous_chapters", "debts", "debt_text", "main",
]

CAMPAIGN_FORMAT = "dnd-campaign-campaign"
CAMPAIGN_VERSION = 1


class CampaignError(ValueError):
    """战役文件/章节引用不成立。"""


@dataclass
class Chapter:
    id: str = ""
    title: str = ""
    adventure: str = ""
    #: 进这一章之前要不要休整："" = 不休，`"long"` = 长休（Q35）
    rest: str = ""


@dataclass
class Campaign:
    title: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    source: str = ""

    def chapter(self, cid: str) -> Chapter:
        for ch in self.chapters:
            if ch.id == cid:
                return ch
        raise CampaignError(f"战役里没有章节 {cid!r}"
                            f"（现有：{[c.id for c in self.chapters]}）")


def chapter_path(campaign: Campaign, chapter: Chapter) -> str:
    """章节剧本的绝对路径（相对战役文件所在目录）。"""
    base = os.path.dirname(os.path.abspath(campaign.source)) if campaign.source else ""
    return os.path.abspath(os.path.join(base, chapter.adventure))


def parse_campaign(raw: Any, *, source: str = "") -> Campaign:
    """只做结构解析；文件是否存在交给 `load_campaign`。"""
    if not isinstance(raw, dict):
        raise CampaignError(f"战役文件根必须是对象，拿到 {type(raw).__name__}")
    if raw.get("format") not in (None, CAMPAIGN_FORMAT):
        raise CampaignError(f"不是本项目的战役文件（format={raw.get('format')!r}）")
    if raw.get("version") not in (None, CAMPAIGN_VERSION):
        raise CampaignError(f"战役文件版本 {raw.get('version')!r} 与引擎 "
                            f"{CAMPAIGN_VERSION} 不匹配")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise CampaignError("战役缺少 title")
    raw_chapters = raw.get("chapters")
    if not isinstance(raw_chapters, list) or not raw_chapters:
        raise CampaignError("chapters 必须是非空数组")
    chapters: list[Chapter] = []
    seen: set[str] = set()
    for i, c in enumerate(raw_chapters):
        if not isinstance(c, dict):
            raise CampaignError(f"chapters[{i}] 必须是对象")
        cid = str(c.get("id") or "").strip()
        ctitle = str(c.get("title") or "").strip()
        adv = str(c.get("adventure") or "").strip()
        if not cid:
            raise CampaignError(f"chapters[{i}] 缺少 id")
        if cid in seen:
            raise CampaignError(f"章节 id 重复：{cid!r}")
        seen.add(cid)
        if not ctitle:
            raise CampaignError(f"章节 {cid!r} 缺少 title")
        if not adv:
            raise CampaignError(f"章节 {cid!r} 缺少 adventure（剧本文件名）")
        rest = str(c.get("rest") or "").strip().lower()
        if rest not in ("", "long"):
            raise CampaignError(f"章节 {cid!r} 的 rest 只能是 \"\" 或 \"long\"，"
                                f"拿到 {rest!r}")
        chapters.append(Chapter(id=cid, title=ctitle, adventure=adv,
                                rest=rest))
    return Campaign(title=title, chapters=chapters, source=source)


def load_campaign(path: os.PathLike | str) -> Campaign:
    """读战役文件，并**逐个把章节剧本读一遍**（读不出来就报，不留到开局）。"""
    path = os.path.abspath(os.fspath(path))
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as exc:
        raise CampaignError(f"读不到战役文件 {path!r}：{exc}") from exc
    except ValueError as exc:
        raise CampaignError(f"战役文件不是合法 JSON：{path!r}（{exc}）") from exc
    camp = parse_campaign(raw, source=path)
    loaded: dict[str, Adventure] = {}
    for ch in camp.chapters:
        target = chapter_path(camp, ch)
        if not os.path.exists(target):
            raise CampaignError(f"章节 {ch.id!r} 的剧本不存在：{target}")
        try:
            loaded[ch.id] = load_adventure(target)
        except AdventureError as exc:
            raise CampaignError(f"章节 {ch.id!r} 的剧本不合法："
                                f"{'；'.join(exc.problems)}") from exc
    _check_pays_external(camp, loaded)
    return camp


def _check_pays_external(camp: Campaign, loaded: dict[str, Adventure]) -> None:
    """`pays_external` 只能指向**前面章节**里真实存在的伏笔。

    写错了在开局前报（不是跑到一半才发现欠账关不掉）。
    """
    order = [c.id for c in camp.chapters]
    for ch in camp.chapters:
        idx = order.index(ch.id)
        earlier = {c.id: loaded[c.id] for c in camp.chapters[:idx]}
        for scene in loaded[ch.id].scenes:
            for ref in getattr(scene, "pays_external", ()) or ():
                cid, _, pid = str(ref).partition(":")
                if cid not in earlier:
                    raise CampaignError(
                        f"章 {ch.id!r} 的场景 {scene.id!r} 要兑现 {ref!r}，"
                        f"但 {cid!r} 不是它前面的章（之前有：{list(earlier)}）")
                known = {p.id for p in earlier[cid].promises}
                if pid not in known:
                    raise CampaignError(
                        f"章 {ch.id!r} 的场景 {scene.id!r} 要兑现 {ref!r}，"
                        f"但章 {cid!r} 里没有伏笔 {pid!r}（现有：{sorted(known)}）")


def previous_chapters(campaign: Campaign, chapter_id: str) -> list[Chapter]:
    """第 N 章之前的所有章节（**倒序**：最近的先看）。"""
    ids = [c.id for c in campaign.chapters]
    try:
        idx = ids.index(chapter_id)
    except ValueError as exc:
        raise CampaignError(f"战役里没有章节 {chapter_id!r}（现有：{ids}）") from exc
    return list(reversed(campaign.chapters[:idx]))


def debts(campaign: Campaign, chapter_id: str, *, limit: int | None = None
          ) -> list[dict]:
    """把前面章节里**还欠着的伏笔**汇总（跨剧本）。"""
    out: list[dict] = []
    for ch in previous_chapters(campaign, chapter_id):
        path = chapter_path(campaign, ch)
        adv: Adventure = load_adventure(path)
        prog = CampaignProgress.load(progress_path_for(path), missing_ok=True,
                                     adventure=adv.title)
        for p in prog.unresolved(adv):
            beats = prog.beats_of(p.id)
            last = beats[-1] if beats else None
            out.append({
                "chapter": ch.id, "chapter_title": ch.title,
                "id": p.id, "title": p.title, "summary": p.summary,
                "beats": len(beats),
                "last_scene": last.scene if last else "",
                "last_note": last.note if last else "",
            })
    if limit is not None:
        out = out[:max(0, int(limit))]
    return out


def debt_text(campaign: Campaign, chapter_id: str, *, limit: int = 6) -> str:
    """下一章开局的欠账块。**只给 DM。**没有欠账返回空串。"""
    rows = debts(campaign, chapter_id)
    if not rows:
        return ""
    lines = ["【上一章还欠着的伏笔 · 只有 DM 看得到，别直接念给玩家】"]
    for r in rows[:limit]:
        tail = f"（第 {r['chapter']} 章《{r['chapter_title']}》，提过 {r['beats']} 次"
        if r["last_scene"]:
            tail += f"，最后一次在「{r['last_scene']}」"
        if r["last_note"]:
            tail += f"：{r['last_note']}"
        tail += "）"
        lines.append(f"· {r['id']} {r['title']}{tail}"
                     + (f" —— {r['summary']}" if r["summary"] else ""))
    if len(rows) > limit:
        lines.append(f"· 还有 {len(rows) - limit} 条没列（先别一次全倒给 DM）")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="战役文件自检 / 跨章欠账")
    ap.add_argument("campaign", nargs="?",
                    default=os.path.join("adventures", "哨塔的拖痕.campaign.json"))
    ap.add_argument("--check", action="store_true", help="只做自检")
    ap.add_argument("--debts", metavar="CHAPTER", help="看某章开局会带哪些欠账")
    args = ap.parse_args(argv)
    try:
        camp = load_campaign(args.campaign)
    except CampaignError as exc:
        print(f"❌ {exc}")
        return 1
    print(f"✅ 战役合法：《{camp.title}》，{len(camp.chapters)} 章")
    for i, ch in enumerate(camp.chapters, 1):
        print(f"  {i}. {ch.id}《{ch.title}》→ {ch.adventure}")
    if args.debts:
        text = debt_text(camp, args.debts)
        print("\n" + (text or "（这一章开局没有跨章欠账）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())