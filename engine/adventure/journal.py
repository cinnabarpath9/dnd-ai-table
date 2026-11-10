# coding=utf-8
"""任务日志 / 结算回执（P0）：**只读投影，不是第二套真相**。

数据来源只有三处：

    · 剧本 `adventure.json`：这一场叫什么、目标是什么、收哪条伏笔；
    · `loop.scenes_done`：已经跑完哪几场（存档里有）；
    · 世界树的线索：哪条痕迹**已经被发现**。

所以这里没有任务表、没有奖励表、没有"结算写回"。页面看到的每一行都能在
现有事实里找到出处；没有登记的金币/经验就不编。

三条口径：

1. **不剧透未来**：只列"当前这一场"和"已完成的场"，未跑的场不出现；
2. **不泄露秘密**：只展示已发现的线索；没发现的线索连数量都不显示；
3. **回执只记事实**：完成、发现的线索、收掉的伏笔；没有数值奖励就不写。
"""

from __future__ import annotations

from typing import Any

from .model import Adventure, Scene

__all__ = ["build_journal", "JOURNAL_NOTE"]


#: 页面/文档共用的口径说明（不是数据）
JOURNAL_NOTE = ("回执只记已发生的事实：完成、发现的线索、收掉的伏笔；"
                "本作没有登记金币/经验奖励，不虚报。")


def _short(text: str, n: int = 90) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n].rstrip("，。、；：") + "…"


def _act_title(adv: Adventure, scene_id: str) -> str:
    for act in adv.acts:
        if scene_id in act.scenes:
            return act.title or act.id
    return ""


def _found_clues(loop, scene_id: str) -> list[dict]:
    """这一场里**已经被发现**的线索。没发现的绝不出现。"""
    out: list[dict] = []
    world = getattr(loop, "world", None)
    if world is None:
        return out
    for cid in world.clues():
        node = world.clue(cid) or {}
        if str(node.get("场景") or "") != scene_id:
            continue
        if not node.get("已发现"):
            continue
        out.append({"id": str(cid), "text": str(node.get("描述") or "")})
    return out


def _closed_promises(adv: Adventure, scene: Scene) -> list[dict]:
    """这一场 `pays` 的伏笔；剧本里找不到就跳过（不猜）。"""
    out: list[dict] = []
    for pid in scene.pays:
        p = adv.promise(pid)
        if p is None:
            continue
        out.append({"id": p.id, "title": p.title})
    return out


def _receipt(adv: Adventure, loop, scene: Scene, index: int) -> dict:
    return {
        "index": int(index),
        "scene": scene.id,
        "title": scene.title,
        "act": _act_title(adv, scene.id),
        "clues": _found_clues(loop, scene.id),
        "closed": _closed_promises(adv, scene),
    }


def build_journal(adv: Adventure | None, loop, *, receipt_limit: int = 8) -> dict:
    """当前目标 + 已完成场的结算回执。**纯读，不写任何状态。**"""
    if adv is None:
        return {"title": "", "completed": 0, "current": None,
                "receipts": [], "all_done": False, "note": JOURNAL_NOTE}

    done_ids = {str(x) for x in (getattr(loop, "scenes_done", ()) or ())}
    done_scenes = [s for s in adv.scenes if s.id in done_ids]
    current = next((s for s in adv.scenes if s.id not in done_ids), None)

    receipts = [_receipt(adv, loop, s, i + 1)
                for i, s in enumerate(adv.scenes) if s.id in done_ids]
    if receipt_limit is not None and receipt_limit >= 0:
        receipts = receipts[-int(receipt_limit):]
    receipts.reverse()                       # 最近的先看

    current_out: dict | None = None
    if current is not None:
        current_out = {
            "scene": current.id,
            "title": current.title,
            "act": _act_title(adv, current.id),
            "objective": _short(current.situation),
            "clues": _found_clues(loop, current.id),
        }

    return {
        "title": adv.title,
        "completed": len(done_scenes),
        "current": current_out,
        "receipts": receipts,
        "all_done": current is None,
        "note": JOURNAL_NOTE,
    }