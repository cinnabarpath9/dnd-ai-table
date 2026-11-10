# coding=utf-8
"""固定模板（中文 Markdown）→ 草稿。

模板长这样（`tools/forge/template.md` 是可直接改的样本）：

    # 剧本：雾渡的第三声钟
    - 人数: 4
    - 时长: 2 小时

    ## 伏笔
    - p1 钟绳上的黑蜡 | 谁碰过第三根钟绳就留痕

    ## 场景 s1 渡口复勘·天光
    - 地点: 雾渡口
    - 时间: 1492-09-17 清晨
    - 局面: 天光刚亮，渡口空着，钟绳上挂着一截黑蜡，缆绳被割断。
    - 在场: 布兰在渡口边查看钟绳，艾尔玛蹲在缆桩旁翻看缆绳断口
    - 跨度: 约半刻钟，天光到日头出来
    - 离场: 茶棚的老周还没出现，但灶上温着茶
    - 线索: 察觉 12 | 钟绳上有黑蜡，蜡里混着河沙 | 老船工说这是盲婆做的记号
    - 推进: p1 | 玩家发现蜡里混着河沙
    - 战斗: Bandit x2 @ 30 尺 / 4 轮
    - 兑现: p1

为什么是「固定模板」而不是自由解析：模板就是那份 JSON 契约的人话版本。
用户照着填，工具只做翻译和校验，**不需要模型、不花钱、可回归**；
自由文本（小说/战役本）才需要模型，那些走 `from_novel` 或后续的 LLM 层。
"""
from __future__ import annotations

import re
from typing import Any

SKILL_CN = {
    "察觉": "perception", "感知": "perception", "perception": "perception",
    "调查": "investigation", "智力": "investigation",
    "investigation": "investigation",
}

_HEAD = re.compile(r"^#\s*剧本[:：]\s*(?P<title>.+?)\s*$")
_SCENE = re.compile(r"^##\s*场景\s+(?P<id>\S+)\s*(?P<title>.*?)\s*$")
_PROMISE = re.compile(r"^[-*]\s*(?P<id>\S+)\s+(?P<title>[^|]+?)\s*"
                      r"(?:\|\s*(?P<summary>.*))?$")
_KV = re.compile(r"^[-*]\s*(?P<key>[^:：]+)[:：]\s*(?P<value>.*)$")


def _split_scenes(text: str) -> tuple[str, dict, list[tuple[str, str, list[str]]]]:
    title = ""
    meta: dict = {}
    scenes: list[tuple[str, str, list[str]]] = []
    current: list[str] | None = None
    buffer: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _HEAD.match(line)
        if m:
            title = m.group("title")
            continue
        m = _SCENE.match(line)
        if m:
            if current is not None:
                scenes.append((current[0], current[1], buffer))
            current = [m.group("id"), m.group("title")]
            buffer = []
            continue
        if line.startswith("## "):
            if current is not None:
                scenes.append((current[0], current[1], buffer))
                current, buffer = None, []
            continue
        if current is None:
            # 头部 KV 一律收（**不再要求"标题之前"**）：自带模板把
            # 人数/时长/来源写在 `# 剧本：…` 之后，旧条件会把它们静默丢掉。
            m = _KV.match(line)
            if m:
                key = m.group("key").strip()
                if key and key not in meta:
                    meta[key] = m.group("value").strip()
            continue
        buffer.append(line)
    if current is not None:
        scenes.append((current[0], current[1], buffer))
    return title, meta, scenes


def _parse_scene(sid: str, title: str, lines: list[str]) -> dict:
    scene: dict[str, Any] = {"id": sid, "title": title, "brief": {}, "world": {},
                             "clues": [], "promises": [], "beat_notes": {}}
    for raw in lines:
        m = _KV.match(raw.strip())
        if not m:
            continue
        key, value = m.group("key").strip(), m.group("value").strip()
        if key in ("地点", "location"):
            scene["world"]["location"] = value
        elif key in ("时间", "date", "时段"):
            parts = value.split()
            scene["world"]["date"] = parts[0] if parts else ""
            if len(parts) > 1:
                scene["world"]["period"] = " ".join(parts[1:])
        elif key in ("局面", "situation"):
            scene["situation"] = value
        elif key in ("在场", "present"):
            scene["brief"]["present"] = value
        elif key in ("跨度", "span"):
            scene["brief"]["span"] = value
        elif key in ("离场", "offstage"):
            scene["brief"]["offstage"] = value
        elif key in ("线索", "clue"):
            pieces = [p.strip() for p in value.split("|")]
            skill_dc = pieces[0] if pieces else ""
            skill, _, dc = skill_dc.partition(" ")
            text = pieces[1] if len(pieces) > 1 else ""
            source = pieces[2] if len(pieces) > 2 else ""
            scene["clues"].append({
                "id": f"c_{sid}_{len(scene['clues']) + 1}",
                "text": text,
                "skill": SKILL_CN.get(skill.strip(), "perception"),
                "dc": int(dc.strip()) if dc.strip().isdigit() else 12,
                "source": source,
            })
        elif key in ("推进", "beat"):
            pid, _, note = value.partition("|")
            pid, note = pid.strip(), note.strip()
            if pid:
                scene["promises"].append(pid)
                scene["beat_notes"][pid] = note or f"场景《{title}》"
        elif key in ("兑现", "pays"):
            scene["pays"] = [x.strip() for x in value.split("|") if x.strip()]
        elif key in ("战斗", "combat"):
            scene["combat"] = _parse_combat(value)
    return scene


def _parse_combat(value: str) -> dict | None:
    """`Bandit x2 @ 30 尺 / 4 轮` → {enemies, distance, rounds}。"""
    m = re.match(r"(?P<who>.+?)\s*@\s*(?P<dist>\d+)\s*尺?\s*(?:/\s*(?P<rounds>\d+)"
                 r"\s*轮?)?$", value)
    if not m:
        return None
    enemies: list[list] = []
    for chunk in m.group("who").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        em = re.match(r"(?P<name>.+?)\s*[x×*]\s*(?P<n>\d+)$", chunk)
        if em:
            enemies.append([em.group("name").strip(), int(em.group("n"))])
        else:
            enemies.append([chunk, 1])
    if not enemies:
        return None
    return {"enemies": enemies, "distance": int(m.group("dist")),
            "rounds": int(m.group("rounds") or 4)}


def parse_markdown(text: str) -> dict:
    """模板文本 → 草稿 dict（不补默认值，交给 `contract.to_adventure`）。"""
    title, meta, scenes = _split_scenes(text)
    draft: dict = {"title": title, "meta": {}, "scenes": [], "promises": [],
                   "threads": [], "npcs": []}
    for key, value in meta.items():
        if key in ("人数", "players"):
            draft["meta"]["players"] = value
        elif key in ("时长", "hours"):
            draft["meta"]["hours"] = value
        elif key in ("来源", "source"):
            draft["meta"]["source"] = value
        else:
            draft["meta"][key] = value
    for sid, stitle, lines in scenes:
        draft["scenes"].append(_parse_scene(sid, stitle, lines))
    # 伏笔：场景解析不到的全局 `## 伏笔` 段
    block = re.search(r"^##\s*伏笔.*?$", text, flags=re.M)
    if block:
        tail = text[block.end():]
        for raw in tail.splitlines():
            if raw.startswith("## "):
                break
            m = _PROMISE.match(raw.strip())
            if m:
                draft["promises"].append({
                    "id": m.group("id"), "title": m.group("title").strip(),
                    "summary": (m.group("summary") or "").strip(),
                })
    return draft