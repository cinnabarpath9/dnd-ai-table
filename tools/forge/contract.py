# coding=utf-8
"""草稿契约：**草稿（draft）→ adventure JSON**，以及「这本能跑吗」的检查。

设计口径：

- 草稿是**人写/模型抽**的东西，允许缺东西；缺了由 `to_adventure()` 补默认值，
  补不出来的（标题、局面、简报）留给 `doctor()` 报出来；
- 产物必须是 `engine.adventure.parse_adventure` 认的 JSON —— 只认这一份契约，
  不另起一套 schema（否则两边会漂）；
- 这里**不调用模型**。模型抽取是可选的上一层，产物仍然落成草稿。
"""
from __future__ import annotations

import json
import os
from typing import Any

from engine.adventure import AdventureError, parse_adventure

DRAFT_VERSION = 1
FORMAT = "dnd-campaign-adventure"

#: 一场探索大概几次调用、一轮战斗大概几次（按 M1/M2 实测均值取整，只用于估预算）
CALLS_PER_SCENE = 8
CALLS_PER_COMBAT_ROUND = 6


def _as_dict(raw: Any) -> dict:
    if isinstance(raw, str):
        return json.loads(raw)
    if not isinstance(raw, dict):
        raise TypeError(f"草稿必须是 dict 或 JSON 字符串，拿到 {type(raw).__name__}")
    return raw


def _slug(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def to_adventure(raw: Any) -> dict:
    """草稿 → 可以交给 `load_adventure` 的 JSON。

    只补**结构性**默认值（id、幕、空表），不替作者编内容：
    局面/简报这些缺了就是缺了，让 `doctor()` 报。
    """
    draft = _as_dict(raw)
    title = _slug(draft.get("title"), "")
    scenes_in = draft.get("scenes") or []
    if not isinstance(scenes_in, list):
        raise TypeError("scenes 必须是数组")

    scenes: list[dict] = []
    for i, scene in enumerate(scenes_in, start=1):
        if not isinstance(scene, dict):
            raise TypeError(f"scenes[{i - 1}] 必须是对象")
        sid = _slug(scene.get("id"), f"s{i}")
        brief = scene.get("brief") or {}
        world = scene.get("world") or {}
        # 外审 B7：草稿是人手写/模型抽的，类型给错要报**能看懂**的错，
        # 不能让 AttributeError 裸穿到 CLI。
        if not isinstance(brief, dict):
            raise ValueError(f"scenes[{i - 1}].brief 必须是对象（present/span/offstage），"
                             f"拿到 {type(brief).__name__}")
        if not isinstance(world, dict):
            raise ValueError(f"scenes[{i - 1}].world 必须是对象，拿到 {type(world).__name__}")
        for key in ("clues", "promises", "beat_notes"):
            raw = scene.get(key)
            if raw is not None and not isinstance(raw, (list, dict)):
                raise ValueError(f"scenes[{i - 1}].{key} 必须是数组/对象，"
                                 f"拿到 {type(raw).__name__}")
        combat = scene.get("combat")
        clues = []
        for j, clue in enumerate(scene.get("clues") or [], start=1):
            if not isinstance(clue, dict):
                continue
            clues.append({
                "id": _slug(clue.get("id"), f"c_{sid}_{j}"),
                "text": str(clue.get("text") or ""),
                "skill": str(clue.get("skill") or "perception"),
                "dc": clue.get("dc", 12),
                "secret": bool(clue.get("secret")),
                "place": str(clue.get("place") or world.get("location") or ""),
                "source": str(clue.get("source") or ""),
            })
        scenes.append({
            "id": sid,
            "title": _slug(scene.get("title"), f"第 {i} 场"),
            "situation": str(scene.get("situation") or ""),
            "brief": {
                "present": str(brief.get("present") or ""),
                "span": str(brief.get("span") or ""),
                "offstage": str(brief.get("offstage") or ""),
            },
            "threads": [str(x) for x in (scene.get("threads") or [])],
            "promises": [str(x) for x in (scene.get("promises") or [])],
            "pays": [str(x) for x in (scene.get("pays") or [])],
            "pays_external": [str(x) for x in (scene.get("pays_external") or [])],
            "clues": clues,
            "combat": combat if isinstance(combat, dict) else None,
            "effects": [dict(e) for e in (scene.get("effects") or [])
                        if isinstance(e, dict)],
            "beat_notes": {str(k): str(v)
                           for k, v in (scene.get("beat_notes") or {}).items()},
            "world": {str(k): v for k, v in world.items()},
        })

    promises = []
    for i, promise in enumerate(draft.get("promises") or [], start=1):
        if isinstance(promise, str):
            promises.append({"id": f"p{i}", "title": promise, "summary": ""})
        elif isinstance(promise, dict):
            promises.append({
                "id": _slug(promise.get("id"), f"p{i}"),
                "title": str(promise.get("title") or ""),
                "summary": str(promise.get("summary") or ""),
            })

    meta = draft.get("meta") if isinstance(draft.get("meta"), dict) else {}
    out = {
        "format": FORMAT,
        "version": 1,
        "title": title,
        "meta": dict(meta),
        "acts": draft.get("acts") or [{
            "id": "act1", "title": "第一幕",
            "scenes": [s["id"] for s in scenes],
        }],
        "scenes": scenes,
        "threads": draft.get("threads") or [],
        "promises": promises,
        "decisions": draft.get("decisions") or [],
        "agenda": draft.get("agenda") or [],
        "npcs": draft.get("npcs") or [],
        "map": draft.get("map") or {"points": [], "links": []},
    }
    return out


def doctor(raw: Any) -> dict:
    """这本地能不能跑：返回 {ok, errors, warnings, stats}。

    `errors` 来自引擎自己的校验（`parse_adventure` + `Adventure.validate`），
    这里**不另写一套规则**；`stats` 只是给作者看规模。
    """
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict = {}
    try:
        doc = to_adventure(raw)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "errors": [f"草稿结构不对：{exc}"],
                "warnings": [], "stats": {}}
    try:
        adv = parse_adventure(doc, source="<draft>")
    except AdventureError as exc:
        return {"ok": False, "errors": list(exc.problems),
                "warnings": [], "stats": {}}

    try:
        errs, warns = adv.validate()
    except AdventureError as exc:                     # 旧接口：只抛
        errs, warns = list(exc.problems), []
    errors.extend(errs)
    warnings.extend(warns)

    scenes = adv.scenes
    combats = [s for s in scenes if s.combat]
    rounds = sum(int((s.combat or {}).get("rounds") or 4) for s in combats)
    stats = {
        "scenes": len(scenes),
        "acts": len(adv.acts),
        "combats": len(combats),
        "combat_rounds": rounds,
        "clues": sum(len(s.clues) for s in scenes),
        "promises": len(adv.promises),
        "estimated_calls": (len(scenes) * CALLS_PER_SCENE
                            + rounds * CALLS_PER_COMBAT_ROUND),
    }
    for scene in scenes:
        if not scene.clues and not scene.combat and not scene.promises:
            warnings.append(f"{scene.id}《{scene.title}》没有任何线索/推进/战斗 —— "
                            f"玩家进去会没事可做")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "stats": stats}


def write_adventure(raw: Any, path: os.PathLike | str) -> str:
    """校验通过才落盘（避免半截剧本被误跑）。返回写入路径。"""
    report = doctor(raw)
    if not report["ok"]:
        raise AdventureError(report["errors"], source=os.fspath(path))
    doc = to_adventure(raw)
    target = os.path.abspath(os.fspath(path))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return target