# coding=utf-8
"""剧本层：把"一段冒险"拆成 骨架 / 台账 / 议程 / 单场简报。

分层借自 NeuroBook（`tools\\NeuroBook-文档`，见决策记录 §5.23）：

    L0 骨架   —— 这一局有哪几幕、哪几场（人定）
    L1 台账   —— 因果线 Thread / 伏笔 Promise / 决策 Decision
    L1.5 议程 —— NPC 下一步想做什么、什么时候再入场
    L2 事实   —— 位置 / HP / 时间 / 谁知道什么
                 **已经由 `engine/state/` 与 `Unit` 管，这里不重复存**
    L3 简报   —— 这一场 DM 必须知道的：在场与主动性 / 时间跨度 / 离场

**唯一真相是 `adventure.json`。** `render_skeleton()` 只是给人看的视图，
不是第二个真相 —— 不要手改渲染出来的 Markdown 再反推回 JSON
（NeuroBook 的 D16：「两边都存事实就会有两个真相」）。

三条校验口径（照抄它的「空泛词不能当 summary」）：

1. **必填**：每场都要有 situation 与三段简报；缺哪个就报哪个，带场次 id；
2. **具体**：`推进关系` / `制造冲突` / `埋下伏笔` 这类空话不算数；
3. **引用**：场景引用的 Thread / Promise 必须存在；幕里引用的场景必须存在。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ..rules.clues import (  # noqa: E402
    ClueError, SceneClue, check_scene_clue, parse_clue,
)

__all__ = [
    "AdventureError", "Brief", "Scene", "Thread", "Promise", "Decision",
    "Agenda", "Act", "Adventure", "parse_adventure", "load_adventure",
    "render_skeleton", "dm_brief_text", "clue_brief_text",
    "PROMISE_TAGS", "THREAD_KINDS", "GENERIC_PHRASES",
]

#: 伏笔的类别（借 NeuroBook 的四类 + 一个 D&D 常用的「悬念」）
PROMISE_TAGS = ("setup_payoff", "prophecy", "motif", "mirror", "open_question")
#: 因果线的类别
THREAD_KINDS = ("main", "side", "personal")
#: 空泛词：拿来当情况/简报/摘要 = 没写
GENERIC_PHRASES = (
    "推进关系", "制造冲突", "埋下伏笔", "增加紧张", "丰富剧情",
    "推动剧情", "制造悬念", "加深感情", "展现性格", "引出后续",
)


class AdventureError(ValueError):
    """剧本不合法。`problems` 是逐条原因，方便一次改完。"""

    def __init__(self, problems: list[str], *, source: str = ""):
        self.problems = list(problems or ["（没有具体原因）"])
        self.source = source
        where = f"{source}：" if source else ""
        super().__init__(where + "剧本有问题 —— " + "；".join(self.problems))


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #

@dataclass
class Brief:
    """单场简报：DM 必须知道、但不该原样念给玩家听的三件事。"""
    present: str = ""      # 在场与主动性：谁在、各自想干什么
    span: str = ""         # 时间跨度：距上一场多久
    offstage: str = ""     # 离场：不在场的人此刻在做什么、何时回来


@dataclass
class Scene:
    id: str = ""
    title: str = ""
    situation: str = ""
    brief: Brief = field(default_factory=Brief)
    threads: list[str] = field(default_factory=list)
    promises: list[str] = field(default_factory=list)
    #: 这一场**兑现**（收掉）的伏笔；只"提到"的写在 `promises` 里
    pays: list[str] = field(default_factory=list)
    #: 本场兑现的**前面章节**的伏笔，写法 `["ch1:p3"]`（章 id:伏笔 id）。
    #: 兑现发生在**那一章的进度文件**里 —— 欠账是从上一章进度算的，
    #: 只在本章记一笔不会让欠账消失（Q23）。
    pays_external: list[str] = field(default_factory=list)
    #: 给**假 DM** 的旁白（真模型会按 situation + 简报自己写）；可空
    narration: str = ""
    #: `{"enemies": [["Bandit", 3]], "distance": 30, "rounds": 4}`
    combat: dict | None = None
    #: 这一场**可被检定发现**的痕迹（P5-G2）。玩家要"观察/搜索"才可能拿到，
    #: 失败时连"这里有东西"都不说。写法见 `engine/rules/clues.py` 的 `SceneClue`。
    clues: list[SceneClue] = field(default_factory=list)
    #: 本场**临时修正**（H2b）：`[{"name": "高地", "kind": "attack",
    #: "value": 1, "target": "party", "scope": "scene"}]`。
    #: 换场景清掉；`scope="round"` 的按该单位回合扣。见 `rules/effects.py`。
    effects: list[dict] = field(default_factory=list)
    #: `{"p2": "玩家查到箱子被拖上塔顶"}` —— **这一场把哪条伏笔推进成了什么样**。
    #: 只给进度文件用（复盘看得到内容）；不写就退化成"场景《标题》"。
    beat_notes: dict[str, str] = field(default_factory=dict)
    #: `{"location": "...", "location_description": "...", "date": "...",
    #:   "period": "...", "weather": "..."}`
    world: dict = field(default_factory=dict)

    @property
    def world_location(self) -> str:
        return str(self.world.get("location") or "")


@dataclass
class Thread:
    """长期因果线：接下来会发生什么。"""
    id: str = ""
    title: str = ""
    kind: str = "main"
    summary: str = ""


@dataclass
class Promise:
    """伏笔 / 欠玩家的交代。**和 Thread 正交**：别为一条伏笔建一条因果线。"""
    id: str = ""
    title: str = ""
    tag: str = "open_question"
    status: str = "open"       # open / paid / dropped
    summary: str = ""


@dataclass
class Decision:
    """为什么这么安排。判据：不写理由，换个人接手会做出不同或更差的选择。"""
    id: str = ""
    what: str = ""
    why: str = ""
    risk: str = ""


@dataclass
class Agenda:
    """NPC 议程：只放「下一步想做什么」，位置/HP 归世界状态。"""
    who: str = ""
    next: str = ""
    reentry: str = ""
    note: str = ""


@dataclass
class Act:
    id: str = ""
    title: str = ""
    scenes: list[str] = field(default_factory=list)


@dataclass
class Adventure:
    title: str = ""
    scenes: list[Scene] = field(default_factory=list)
    acts: list[Act] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    promises: list[Promise] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    agenda: list[Agenda] = field(default_factory=list)
    #: 剧本里登记过的 NPC：`[{"name": "...", "role": "..."}]`
    #: 跑第一场时掷五骰、写进世界树（只给 DM 看）
    npcs: list[dict] = field(default_factory=list)
    #: 世界节点图（U13）：`{"places": [...], "passages": [...]}`
    #: 跑第一场前播种进世界树；点位只标"在不在、去没去过"，坐标不编造。
    map: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    source: str = ""
    #: 校验通过后的提醒（不是错误）
    warnings: list[str] = field(default_factory=list)

    # ---------- 查询 ----------

    def scene(self, sid: str) -> Scene:
        for s in self.scenes:
            if s.id == sid:
                return s
        raise AdventureError([f"剧本里没有场景 {sid!r}"
                               f"（现有：{[s.id for s in self.scenes]}）"])

    def thread(self, tid: str) -> Thread | None:
        return next((t for t in self.threads if t.id == tid), None)

    def promise(self, pid: str) -> Promise | None:
        return next((p for p in self.promises if p.id == pid), None)

    # ---------- 校验 ----------

    def validate(self) -> tuple[list[str], list[str]]:
        """返回 (错误, 提醒)。**有错误就不能跑。**"""
        errors: list[str] = []
        warnings: list[str] = []

        if not self.title.strip():
            errors.append("缺少 title")

        # ---- 场景 ----
        seen_scenes: set[str] = set()
        for i, sc in enumerate(self.scenes):
            where = f"scenes[{i}]（{sc.id or '无 id'}）"
            if not sc.id.strip():
                errors.append(f"{where}: 缺少 id")
            elif sc.id in seen_scenes:
                errors.append(f"{where}: 场景 id 重复")
            seen_scenes.add(sc.id)

            if not sc.title.strip():
                errors.append(f"{where}: 缺少 title")
            if not sc.situation.strip():
                errors.append(f"{where}: 缺少 situation")
            elif len(_plain(sc.situation)) < 8:
                errors.append(f"{where}: situation 太短（至少 8 个实字，"
                              f"别写成「进塔」）")

            for label, val, floor in (
                    ("在场与主动性", sc.brief.present, 4),
                    ("时间跨度", sc.brief.span, 2),
                    ("离场", sc.brief.offstage, 2)):
                if not val.strip():
                    errors.append(f"{where}: 简报缺少「{label}」")
                elif len(_plain(val)) < floor:
                    errors.append(f"{where}: 简报「{label}」太短"
                                  f"（{val!r}），写清楚谁、在哪、做什么")
                elif _is_generic(val):
                    errors.append(f"{where}: 简报「{label}」是空泛表述"
                                  f"（{val!r}）—— 要写具体的人和动作")

            if sc.combat is not None:
                errors.extend(_check_combat(sc.combat, where))
            if sc.world:
                errors.extend(_check_world(sc.world, where))
            for pid, note in (sc.beat_notes or {}).items():
                if pid not in sc.promises:
                    errors.append(f"{where}: beat_notes 写了 {pid!r}，"
                                  f"但它不在本场 promises 里")
                elif _is_generic(note):
                    errors.append(f"{where}: {pid!r} 的推进内容太泛（{note!r}）"
                                  f"—— 写清这一场把它推进成了什么样")

        # ---- 线索（P5-G2）：可被检定发现的痕迹 ----
        clue_ids: set[str] = set()
        for i, sc in enumerate(self.scenes):
            where = f"scenes[{i}]（{sc.id or '无 id'}）"
            for j, c in enumerate(sc.clues):
                cw = f"{where}.clues[{j}]（{c.id or '无 id'}）"
                errors.extend(check_scene_clue(c, cw))
                if c.id and c.id in clue_ids:
                    errors.append(f"{cw}: 线索 id 重复 —— 线索按 id 存进世界树，"
                                  f"必须全局唯一")
                clue_ids.add(c.id)
                if c.source and _is_generic(c.source):
                    errors.append(f"{cw}: source 是空泛表述（{c.source!r}）")
                if c.place and sc.world_location and c.place != sc.world_location:
                    warnings.append(
                        f"{cw}: place={c.place!r} 与场景地点 "
                        f"{sc.world_location!r} 不同 —— 玩家不在那里就撞不到")

        # ---- 场景临时修正（H2b）----
        from ..rules.effects import coerce_effect
        for i, sc in enumerate(self.scenes):
            where = f"scenes[{i}]（{sc.id or '无 id'}）"
            for j, eff in enumerate(sc.effects):
                try:
                    coerce_effect(eff, for_scene=True)
                except ValueError as exc:
                    errors.append(f"{where}.effects[{j}]: {exc}")

        # ---- 因果线 / 伏笔 / 决策 ----
        thread_ids: set[str] = set()
        for i, t in enumerate(self.threads):
            where = f"threads[{i}]（{t.id or '无 id'}）"
            if not t.id.strip():
                errors.append(f"{where}: 缺少 id")
            elif t.id in thread_ids:
                errors.append(f"{where}: Thread id 重复")
            thread_ids.add(t.id)
            if not t.title.strip():
                errors.append(f"{where}: 缺少 title")
            if t.kind not in THREAD_KINDS:
                errors.append(f"{where}: kind 必须是 {THREAD_KINDS} 之一，"
                              f"拿到 {t.kind!r}")
            if t.summary and _is_generic(t.summary):
                errors.append(f"{where}: summary 是空泛表述（{t.summary!r}）")

        promise_ids: set[str] = set()
        for i, p in enumerate(self.promises):
            where = f"promises[{i}]（{p.id or '无 id'}）"
            if not p.id.strip():
                errors.append(f"{where}: 缺少 id")
            elif p.id in promise_ids:
                errors.append(f"{where}: Promise id 重复")
            promise_ids.add(p.id)
            if not p.title.strip():
                errors.append(f"{where}: 缺少 title")
            if p.tag not in PROMISE_TAGS:
                errors.append(f"{where}: tag 必须是 {PROMISE_TAGS} 之一，"
                              f"拿到 {p.tag!r}")
            if p.status not in ("open", "paid", "dropped"):
                errors.append(f"{where}: status 必须是 open/paid/dropped，"
                              f"拿到 {p.status!r}")
            if p.summary and _is_generic(p.summary):
                errors.append(f"{where}: summary 是空泛表述（{p.summary!r}）")

        decision_ids: set[str] = set()
        for i, d in enumerate(self.decisions):
            where = f"decisions[{i}]（{d.id or '无 id'}）"
            if not d.id.strip():
                errors.append(f"{where}: 缺少 id")
            elif d.id in decision_ids:
                errors.append(f"{where}: Decision id 重复")
            decision_ids.add(d.id)
            if not d.what.strip():
                errors.append(f"{where}: 缺少 what")
            if not d.why.strip():
                errors.append(f"{where}: 缺少 why（不写理由，下个人会改回去）")

        # ---- 议程 ----
        for i, a in enumerate(self.agenda):
            where = f"agenda[{i}]（{a.who or '无 who'}）"
            if not a.who.strip():
                errors.append(f"{where}: 缺少 who")
            if not a.next.strip():
                errors.append(f"{where}: 缺少 next（这个人下一步想做什么）")
            elif _is_generic(a.next):
                errors.append(f"{where}: next 是空泛表述（{a.next!r}）")

        # ---- NPC 名单 ----
        npc_names: set[str] = set()
        for i, n in enumerate(self.npcs):
            name = str(n.get("name") or "").strip()
            where = f"npcs[{i}]（{name or '无 name'}）"
            if not name:
                errors.append(f"{where}: 缺少 name")
                continue
            if name in npc_names:
                errors.append(f"{where}: NPC 名字重复")
            npc_names.add(name)

        # ---- 世界节点图（U13） ----
        place_names: set[str] = set()
        for i, p in enumerate(self.map.get("places") or []):
            where = f"map.places[{i}]（{p.get('name') or '无 name'}）"
            name = str(p.get("name") or "").strip()
            if not name:
                errors.append(f"{where}: 缺少 name")
            elif name in place_names:
                errors.append(f"{where}: 地点名重复")
            place_names.add(name)

        #: 通道端点可以引用 map.places，也可以直接引用某场的地点
        known_places = place_names | {sc.world_location for sc in self.scenes
                                      if sc.world_location}
        seen_edges: set[frozenset] = set()
        for i, p in enumerate(self.map.get("passages") or []):
            a = str(p.get("from") or "").strip()
            b = str(p.get("to") or "").strip()
            where = f"map.passages[{i}]（{a or '?'} → {b or '?'}）"
            if not a or not b:
                errors.append(f"{where}: 两端都要有 from/to")
                continue
            if a == b:
                errors.append(f"{where}: 通道不能连到自己")
                continue
            for n in (a, b):
                if n not in known_places:
                    errors.append(f"{where}: 端点 {n!r} 没有登记"
                                  f"（写进 map.places，或用场景 world.location）")
            edge = frozenset((a, b))
            if edge in seen_edges:
                errors.append(f"{where}: 通道重复 —— 同一条路只写一次"
                              f"（通道是双向的）")
            seen_edges.add(edge)

        # ---- 引用 ----
        for i, sc in enumerate(self.scenes):
            where = f"scenes[{i}]（{sc.id or '无 id'}）"
            for tid in sc.threads:
                if tid not in thread_ids:
                    errors.append(f"{where}: 引用了不存在的 Thread {tid!r}"
                                  f"（现有：{sorted(thread_ids)}）")
            for pid in sc.promises:
                if pid not in promise_ids:
                    errors.append(f"{where}: 引用了不存在的 Promise {pid!r}"
                                  f"（现有：{sorted(promise_ids)}）")
            for pid in sc.pays:
                if pid not in promise_ids:
                    errors.append(f"{where}: 兑现了不存在的 Promise {pid!r}")
                elif pid not in sc.promises:
                    warnings.append(f"场景 {sc.id!r} 兑现 {pid!r}，但它没列在本场 "
                                    f"promises 里（那谁也不知道它在这一场被提到）")

        # ---- 幕 ----
        act_ids: set[str] = set()
        for i, act in enumerate(self.acts):
            where = f"acts[{i}]（{act.id or '无 id'}）"
            if not act.id.strip():
                errors.append(f"{where}: 缺少 id")
            elif act.id in act_ids:
                errors.append(f"{where}: Act id 重复")
            act_ids.add(act.id)
            if not act.title.strip():
                errors.append(f"{where}: 缺少 title")
            if not act.scenes:
                errors.append(f"{where}: 一幕至少要有一场")
            for sid in act.scenes:
                if sid not in seen_scenes:
                    errors.append(f"{where}: 引用了不存在的场景 {sid!r}")

        # ---- 提醒（不是错误） ----
        if self.acts:
            in_acts = {sid for act in self.acts for sid in act.scenes}
            for sc in self.scenes:
                if sc.id and sc.id not in in_acts:
                    warnings.append(f"场景 {sc.id!r} 没有归到任何一幕"
                                    f"（跑全剧本时会照常跑到，但骨架不完整）")
        used_promises = {pid for sc in self.scenes for pid in sc.promises}
        for p in self.promises:
            if p.status == "open" and p.id not in used_promises:
                warnings.append(f"Promise {p.id!r} 还是 open，但没有任何场景"
                                f"提到它 —— 是不是忘了埋/忘了收？")
        for d in self.decisions:
            if not d.risk.strip():
                warnings.append(f"Decision {d.id!r} 没写 risk（为什么可能错）")
        return errors, warnings


# --------------------------------------------------------------------------- #
# 校验小工具
# --------------------------------------------------------------------------- #

def _plain(text: str) -> str:
    return re.sub(r"[\s，,。.；;、！!？?“”\"'（）()【】\[\]]", "", str(text or ""))


def _is_generic(text: str) -> bool:
    """只剩空泛词 / 标点 = 没写。**不是一个词都不能用，是不能只有它。**"""
    t = _plain(text)
    if len(t) < 2:
        return True
    left = t
    for g in GENERIC_PHRASES:
        left = left.replace(g, "")
    # 把**所有**空泛词剥完再看剩下多少 —— 只剥一个会把"制造冲突，埋下伏笔"放过去。
    return len(left.strip()) < 2


def _check_combat(c: Any, where: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(c, dict):
        return [f"{where}: combat 必须是对象"]
    enemies = c.get("enemies")
    if not isinstance(enemies, list) or not enemies:
        errors.append(f"{where}: combat.enemies 必须是非空数组（如 [[\"Bandit\", 3]]）")
    else:
        for j, e in enumerate(enemies):
            if (not isinstance(e, (list, tuple)) or len(e) != 2
                    or not isinstance(e[0], str) or not str(e[0]).strip()):
                errors.append(f"{where}: combat.enemies[{j}] 必须是 [怪物名, 数量]")
                continue
            try:
                n = int(e[1])
            except (TypeError, ValueError):
                errors.append(f"{where}: combat.enemies[{j}] 的数量不是整数")
                continue
            if n <= 0:
                errors.append(f"{where}: combat.enemies[{j}] 的数量必须 > 0")
    try:
        distance = float(c.get("distance", 30))
        if distance <= 0:
            errors.append(f"{where}: combat.distance 必须 > 0")
    except (TypeError, ValueError):
        errors.append(f"{where}: combat.distance 不是数字")
    try:
        rounds = int(c.get("rounds", 4))
        if rounds < 0:
            errors.append(f"{where}: combat.rounds 不能是负数")
    except (TypeError, ValueError):
        errors.append(f"{where}: combat.rounds 不是整数")
    return errors


def _check_world(w: Any, where: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(w, dict):
        return [f"{where}: world 必须是对象"]
    loc = w.get("location")
    if loc is not None and not str(loc).strip():
        errors.append(f"{where}: world.location 不能是空字符串")
    for key in ("date", "period", "weather", "location_description"):
        v = w.get(key)
        if v is not None and not isinstance(v, str):
            errors.append(f"{where}: world.{key} 必须是字符串")
    return errors


# --------------------------------------------------------------------------- #
# 解析 / 加载
# --------------------------------------------------------------------------- #

def _s(obj: dict, key: str) -> str:
    v = obj.get(key)
    return "" if v is None else str(v).strip()


def _list(obj: dict, key: str, where: str) -> list:
    v = obj.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        raise AdventureError([f"{where}.{key} 必须是数组"])
    return v


def _objs(obj: dict, key: str, where: str) -> list[dict]:
    out = []
    for i, item in enumerate(_list(obj, key, where)):
        if not isinstance(item, dict):
            raise AdventureError([f"{where}.{key}[{i}] 必须是对象"])
        out.append(item)
    return out


def _parse_clues(s: dict, where: str, source: str) -> list[SceneClue]:
    """解析这一场的 `clues`。**写错了就报，不静默跳过**（线索静默消失最难查）。"""
    out: list[SceneClue] = []
    for j, raw in enumerate(_objs(s, "clues", where)):
        try:
            out.append(parse_clue(raw, f"{where}.clues[{j}]"))
        except ClueError as exc:
            raise AdventureError([str(exc)], source=source) from exc
    return out


def _str_list(obj: dict, key: str, where: str) -> list[str]:
    """一个可选字符串数组（缺省 []）；写错了直接报，不静默丢。"""
    v = obj.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        raise AdventureError([f"{where}.{key} 必须是数组"])
    return [str(x).strip() for x in v if str(x).strip()]


def _check_pay_external(items: list[str], where: str, source: str) -> list[str]:
    """`pays_external` 只校验**写法**：`章 id:伏笔 id`。

    章/伏笔是否真的存在，得等整本战役加载时才知道（见 `campaign.py`）。
    """
    out: list[str] = []
    for raw in items:
        text = str(raw).strip()
        chapter, _, pid = text.partition(":")
        if not chapter or not pid or ":" in pid:
            raise AdventureError(
                [f"{where}.pays_external 写法是 \"章id:伏笔id\"（收到 {raw!r}）"],
                source=source)
        out.append(f"{chapter}:{pid}")
    return out


def _str_map(obj: dict, key: str, where: str) -> dict[str, str]:
    """一个可选的「键 → 字符串」对象（缺省 {}）；写错了直接报。"""
    v = obj.get(key)
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise AdventureError([f"{where}.{key} 必须是对象（伏笔 id → 一句话）"])
    out = {}
    for k, val in v.items():
        if not isinstance(val, str) or not val.strip():
            raise AdventureError(
                [f"{where}.{key}.{k} 必须是非空字符串（写清这一场推进了什么）"])
        out[str(k)] = val.strip()
    return out


def parse_adventure(raw: Any, *, source: str = "") -> Adventure:
    """把 JSON 结构变成对象。**只做类型解析，语义交给 `validate()`。**"""
    if not isinstance(raw, dict):
        raise AdventureError(["剧本根必须是 JSON 对象"], source=source)

    adv = Adventure(title=_s(raw, "title"), source=source,
                    meta=dict(raw.get("meta") or {}) if isinstance(
                        raw.get("meta"), dict) else {})

    scenes = []
    for i, s in enumerate(_objs(raw, "scenes", "根")):
        where = f"scenes[{i}]"
        brief_raw = s.get("brief")
        if brief_raw is None:
            brief_raw = {}
        if not isinstance(brief_raw, dict):
            raise AdventureError([f"{where}.brief 必须是对象"], source=source)
        combat = s.get("combat")
        if combat is not None and not isinstance(combat, dict):
            raise AdventureError([f"{where}.combat 必须是对象"], source=source)
        world = s.get("world")
        if world is None:
            world = {}
        if not isinstance(world, dict):
            raise AdventureError([f"{where}.world 必须是对象"], source=source)
        scenes.append(Scene(
            id=_s(s, "id"), title=_s(s, "title"), situation=_s(s, "situation"),
            brief=Brief(present=_s(brief_raw, "present"),
                        span=_s(brief_raw, "span"),
                        offstage=_s(brief_raw, "offstage")),
            threads=[str(x) for x in _list(s, "threads", where)],
            promises=[str(x) for x in _list(s, "promises", where)],
            pays=[str(x) for x in _list(s, "pays", where)],
            pays_external=_check_pay_external(
                [str(x) for x in _list(s, "pays_external", where)], where, source),
            narration=_s(s, "narration"),
            combat=combat,
            clues=_parse_clues(s, where, source),
            effects=[dict(e) for e in _objs(s, "effects", where)],
            world=dict(world),
            beat_notes=_str_map(s, "beat_notes", where),
        ))
    adv.scenes = scenes

    adv.acts = [Act(id=_s(a, "id"), title=_s(a, "title"),
                    scenes=[str(x) for x in _list(a, "scenes", f"acts[{i}]")])
                for i, a in enumerate(_objs(raw, "acts", "根"))]
    adv.threads = [Thread(id=_s(t, "id"), title=_s(t, "title"),
                          kind=_s(t, "kind") or "main",
                          summary=_s(t, "summary"))
                   for t in _objs(raw, "threads", "根")]
    adv.promises = [Promise(id=_s(p, "id"), title=_s(p, "title"),
                            tag=_s(p, "tag") or "open_question",
                            status=_s(p, "status") or "open",
                            summary=_s(p, "summary"))
                    for p in _objs(raw, "promises", "根")]
    adv.decisions = [Decision(id=_s(d, "id"), what=_s(d, "what"),
                              why=_s(d, "why"), risk=_s(d, "risk"))
                     for d in _objs(raw, "decisions", "根")]
    adv.agenda = [Agenda(who=_s(a, "who"), next=_s(a, "next"),
                         reentry=_s(a, "reentry"), note=_s(a, "note"))
                  for a in _objs(raw, "agenda", "根")]
    adv.npcs = [{"name": _s(n, "name"), "role": _s(n, "role")}
                for n in _objs(raw, "npcs", "根")]

    # ---- 世界节点图（U13）----
    map_raw = raw.get("map")
    if map_raw is None:
        map_raw = {}
    if not isinstance(map_raw, dict):
        raise AdventureError(["map 必须是对象"], source=source)

    def _bool_at(obj: dict, key: str, where: str, default: bool) -> bool:
        v = obj.get(key, default)
        if not isinstance(v, bool):      # "false" 字符串不算 false（§4.15.3）
            raise AdventureError(
                [f"{where}.{key} 必须是 true/false（拿到 {v!r}）"], source=source)
        return v

    adv.map = {
        "places": [
            {"name": _s(p, "name"), "region": _s(p, "region"),
             "description": _s(p, "description"),
             "discovered": _bool_at(p, "discovered", f"map.places[{i}]", False),
             "heard": _bool_at(p, "heard", f"map.places[{i}]", False),
             "shop": _str_list(p, "shop", f"map.places[{i}]")}
            for i, p in enumerate(_objs(map_raw, "places", "map"))],
        "passages": [
            {"from": _s(p, "from"), "to": _s(p, "to"),
             "note": _s(p, "note"), "secret": _s(p, "secret"),
             "known": _bool_at(p, "known", f"map.passages[{i}]", True)}
            for i, p in enumerate(_objs(map_raw, "passages", "map"))],
    }
    return adv


def load_adventure(path: os.PathLike | str) -> Adventure:
    """读一个剧本文件。**不合法的剧本不许跑** —— 逐条报错，不猜。"""
    path = os.fspath(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise AdventureError([f"读不到剧本 {path!r}：{exc}"], source=path) from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise AdventureError([f"剧本不是合法 JSON（{exc}）"], source=path) from exc

    adv = parse_adventure(raw, source=path)
    errors, warnings = adv.validate()
    adv.warnings = warnings
    if errors:
        raise AdventureError(errors, source=path)
    return adv


# --------------------------------------------------------------------------- #
# 渲染（**输出是视图，不是真相**）
# --------------------------------------------------------------------------- #

GUIDE_KEYS = ("流程关键词", "剧本特色", "玩法体感", "剧本目标")


def _meta_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return " → ".join(str(x).strip() for x in value if str(x).strip())
    return str(value or "").strip()


def campaign_guide(adv: Adventure | None) -> str:
    """DM 全程方向：四要素只从 `meta` 读，不替剧本编。"""
    if adv is None:
        return ""
    meta = getattr(adv, "meta", {}) or {}
    lines = []
    for key in GUIDE_KEYS:
        value = _meta_text(meta.get(key))
        if value:
            lines.append(f"· {key}：{value}")
    return "\n".join(lines)


def campaign_player_goal(adv: Adventure | None) -> str:
    """玩家可见的长期目标 + 流程关键词；不含 DM 秘密与单场简报。"""
    if adv is None:
        return ""
    meta = getattr(adv, "meta", {}) or {}
    lines = []
    target = _meta_text(meta.get("剧本目标"))
    flow = _meta_text(meta.get("流程关键词"))
    if target:
        lines.append(f"队伍目标：{target}")
    if flow:
        lines.append(f"流程关键词：{flow}")
    return "\n".join(lines)


def dm_brief_text(scene: Scene, adv: Adventure | None = None) -> str:
    """单场简报 → 给 DM 的一块文本。**只进 DM 的提示词，不进玩家的。**

    `adv` 给了就把这一场挂的**因果线与伏笔**也带上（只带本场的，
    不是把整本台账倒进去 —— 台账越大，越要按场取）。
    """
    lines = ["【本场简报 · 只有 DM 看得到，别把离场当玩家已知】",
             f"· 在场与主动性：{scene.brief.present}",
             f"· 时间跨度：{scene.brief.span}",
             f"· 离场：{scene.brief.offstage}"]
    if adv is not None:
        threads = [adv.thread(t) for t in scene.threads]
        threads = [t for t in threads if t is not None]
        if threads:
            lines.append("· 本场因果线：" + "；".join(
                f"{t.title}（{t.summary}）" if t.summary else t.title
                for t in threads))
        promises = [adv.promise(p) for p in scene.promises]
        promises = [p for p in promises if p is not None]
        if promises:
            lines.append("· 本场伏笔：" + "；".join(
                f"{p.title}［{p.tag}/{p.status}］"
                + (f"（{p.summary}）" if p.summary else "")
                for p in promises))
    return "\n".join(lines)


def clue_brief_text(scene: Scene, found=()) -> str:
    """本场可发现的痕迹 → 给 DM 的一段。**玩家拿不到。**

    `found` 是"已经发现过的线索 id"（世界树里有）。已发现的也列出来，
    但标成"已发现"—— DM 要据此决定还能不能拿它做文章（别让玩家再"第一次发现"）。
    """
    if not scene.clues:
        return ""
    found_set = {str(x) for x in (found or ())}
    lines = ["【本场可发现的痕迹 · 只有 DM 看得到，别替玩家说破】"]
    for c in scene.clues:
        mark = "已发现" if c.id in found_set else "未发现"
        lines.append(f"· {c.id}［{c.skill_cn} DC {c.dc}｜{mark}］{c.text}")
        if c.source:
            lines.append(f"    背后：{c.source}")
    lines.append("（玩家要用「观察」或「搜索」掷检定才可能拿到；"
                 "失败时**不要暗示这里有东西**）")
    return "\n".join(lines)


def render_skeleton(adv: Adventure) -> str:
    """人看的骨架视图（幕 → 场 → 简报 → 痕迹）。**别拿它当第二个真相。**"""
    out = [f"# {adv.title or '（无标题剧本）'}",
           "",
           "> 由 `adventure.json` 生成，只作阅读视图；要改就改 JSON 再重新生成。",
           ""]
    if adv.acts:
        out.append("## 幕")
        out.append("")
        for act in adv.acts:
            out.append(f"### {act.id} {act.title}")
            for sid in act.scenes:
                sc = next((s for s in adv.scenes if s.id == sid), None)
                if sc is None:
                    out.append(f"- [缺场] {sid}")
                    continue
                out.append(f"- **{sc.id} {sc.title}** —— {_short(sc.situation, 46)}")
                out.append(f"  - 在场与主动性：{_short(sc.brief.present, 60)}")
                out.append(f"  - 时间跨度：{sc.brief.span}")
                out.append(f"  - 离场：{_short(sc.brief.offstage, 60)}")
                if sc.pays:
                    out.append(f"  - 兑现：{'、'.join(sc.pays)}")
                for c in sc.clues:
                    out.append(f"  - 痕迹 `{c.id}`［{c.skill_cn} DC {c.dc}｜"
                               f"{'秘密' if c.secret else '公开'}］"
                               f"{_short(c.text, 60)}")
                if sc.combat:
                    enemies = sc.combat.get("enemies") or []
                    out.append(f"  - 战斗："
                               + "、".join(f"{e[0]}×{e[1]}" for e in enemies
                                           if isinstance(e, (list, tuple))
                                           and len(e) == 2))
            out.append("")
    else:
        out.append("## 场景")
        out.append("")
        for sc in adv.scenes:
            out.append(f"- **{sc.id} {sc.title}** —— {_short(sc.situation, 46)}")
            for c in sc.clues:
                out.append(f"  - 痕迹 `{c.id}`［{c.skill_cn} DC {c.dc}｜"
                           f"{'秘密' if c.secret else '公开'}］"
                           f"{_short(c.text, 60)}")
        out.append("")

    if adv.threads:
        out += ["## 因果线", ""]
        out += [f"- `{t.id}`（{t.kind}）{t.title}"
                + (f" —— {t.summary}" if t.summary else "")
                for t in adv.threads]
        out.append("")
    if adv.promises:
        out += ["## 伏笔", ""]
        out += [f"- `{p.id}`（{p.tag} / {p.status}）{p.title}"
                + (f" —— {p.summary}" if p.summary else "")
                for p in adv.promises]
        out.append("")
    if adv.agenda:
        out += ["## NPC 议程", ""]
        out += [f"- **{a.who}**：{a.next}"
                + (f"（再入场：{a.reentry}）" if a.reentry else "")
                for a in adv.agenda]
        out.append("")
    if adv.npcs:
        out += ["## NPC（跑起来掷五骰）", ""]
        out += [f"- **{n.get('name')}**（{n.get('role') or '身份未定'}）"
                for n in adv.npcs]
        out.append("")
    if adv.map.get("places") or adv.map.get("passages"):
        out += ["## 地图（节点图）", ""]
        for p in adv.map.get("places") or []:
            tags = []
            if p.get("discovered"):
                tags.append("去过")
            if p.get("heard"):
                tags.append("只听说过")
            s = f"- 点 **{p.get('name')}**"
            if p.get("region"):
                s += f"（{p['region']}）"
            if tags:
                s += " ［" + "、".join(tags) + "］"
            if p.get("description"):
                s += f"：{_short(p['description'], 50)}"
            out.append(s)
        for p in adv.map.get("passages") or []:
            s = f"- 路 **{p.get('from')}** ↔ **{p.get('to')}**"
            if p.get("note"):
                s += f" —— {p['note']}"
            if not p.get("known", True):
                s += "（玩家未知）"
            out.append(s)
        out.append("")
    if adv.decisions:
        out += ["## 决策", ""]
        out += [f"- `{d.id}` {d.what} —— 理由：{d.why}"
                + (f"；风险：{d.risk}" if d.risk else "")
                for d in adv.decisions]
    return "\n".join(out).rstrip() + "\n"


def _short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"
