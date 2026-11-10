# coding=utf-8
"""规则片段按需检索 —— 从本地 SRD `rules`（56 条）里取原文摘录。

对应决策记录 §5.19（U7）。**纯本地、零模型调用、零联网。**

之前 `GameLoop._rules_text()` 写死五行，规则覆盖窄；U7 改成：

    · 按"场景"给一组规则（explore / combat / spell / social）；
    · 每条规则从 SRD 原文里**截取**（默认到句子边界），不自己编规则；
    · 有总字数预算（默认 1700 字），装不下的跳过并如实说明；
    · 也支持按关键词检索（`search_rules`）和显式指定主题（`topics=`）。

⚠️ **只摘录、不改写、不补规则**：找不到就写"找不到规则「X」"，让 DM 裁定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from ..srd.store import NotFound, get_store

__all__ = [
    "RulePick", "PHASE_RULES", "PHASES", "CORE_LINE", "FOOTER",
    "get_rule", "all_rules", "search_rules", "build_rules_text",
    "rules_text_for", "available_topics",
]

#: 引擎自身的动作经济口径（不是 SRD 摘录，用它把"回合怎么算"钉死）
CORE_LINE = ("引擎口径：每回合 1 动作 / 1 附赠动作 / 1 反应 / 移动；"
             "先攻决定顺序；骰子与判定由引擎给出，模型不改数值。")

FOOTER = "（以上为 SRD 2024 原文摘录；未列出的规则交 DM 裁定，不猜。）"


@dataclass(frozen=True)
class RulePick:
    """要摘录哪条规则、从哪一段开始、截多长。"""
    key: str
    section: str = ""        # 在原文里找这段标题（可选）
    limit: int = 260         # 最多截多少字符


#: 场景 → 规则清单（顺序即优先级；预算装不下就往后跳）
PHASE_RULES: dict[str, tuple[RulePick, ...]] = {
    "explore": (
        RulePick("srd-2024_d20-tests_ability-checks", limit=240),
        RulePick("srd-2024_d20-tests_advantage-disadvantage", limit=240),
        RulePick("srd-2024_exploration_vision-and-light", limit=240),
        RulePick("srd-2024_exploration_hiding", limit=200),
        RulePick("srd-2024_exploration_travel", limit=240),
        RulePick("srd-2024_exploration_interacting-with-objects", limit=220),
        RulePick("srd-2024_damage-and-healing_resting", limit=200),
        RulePick("srd-2024_social-interaction_roleplaying", limit=220),
    ),
    "combat": (
        RulePick("srd-2024_combat_the-order-of-combat", limit=150),
        RulePick("srd-2024_d20-tests_attack-rolls", limit=150),
        RulePick("srd-2024_combat_making-an-attack", limit=130),
        RulePick("srd-2024_combat_melee-attacks",
                 section="Opportunity Attacks", limit=300),
        RulePick("srd-2024_combat_cover", limit=150),
        RulePick("srd-2024_combat_ranged-attacks", limit=120),
        RulePick("srd-2024_damage-and-healing_critical-hits", limit=140),
        RulePick("srd-2024_damage-and-healing_dropping-to-zero-hit-points",
                 limit=170),
        RulePick("srd-2024_damage-and-healing_resistance-and-vulnerability",
                 limit=150),
        RulePick("srd-2024_damage-and-healing_temporary-hit-points", limit=140),
        RulePick("srd-2024_combat_unseen-attackers-and-target", limit=140),
        RulePick("srd-2024_combat_playing-on-a-grid", limit=140),
    ),
    "spell": (
        RulePick("srd-2024_multiclassing_spellcasting", limit=300),
        RulePick("srd-2024_d20-tests_saving-throw", limit=240),
        RulePick("srd-2024_damage-and-healing_damage-rolls", limit=220),
        RulePick("srd-2024_damage-and-healing_critical-hits", limit=200),
    ),
    "social": (
        RulePick("srd-2024_social-interaction_ability-checks", limit=260),
        RulePick("srd-2024_social-interaction_roleplaying", limit=240),
        RulePick("srd-2024_proficiency_skill-proficiencies", limit=240),
    ),
}

PHASES = tuple(PHASE_RULES)

_PHASE_CN = {"explore": "探索", "combat": "战斗",
             "spell": "施法", "social": "社交"}


# --------------------------------------------------------------------------- #
# 查表
# --------------------------------------------------------------------------- #

def all_rules() -> list[dict]:
    """本地全部规则记录（56 条，只读）。"""
    return list(get_store().records("rules"))


def all_rule_keys() -> list[str]:
    return [str(r.get("key") or "") for r in all_rules()]


def get_rule(key_or_name: str) -> dict | None:
    """按 key 或名字取一条规则；找不到返回 None（**不猜、不编**）。"""
    name = (key_or_name or "").strip()
    if not name:
        return None
    store = get_store()
    try:
        return store.by_key("rules", name)
    except NotFound:
        pass
    try:
        return store.by_name("rules", name)
    except NotFound:
        return None


def available_topics() -> list[tuple[str, str]]:
    """(key, name) 清单，给排查/前端用。"""
    return [(str(r.get("key") or ""), str(r.get("name") or "")) for r in all_rules()]


def search_rules(query: str, *, limit: int = 3) -> list[dict]:
    """按关键词给规则排序：名字命中优先，其次正文出现次数。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    words = [w for w in re.split(r"[\s,，、]+", q) if w]

    def score(rec: dict) -> tuple:
        name = str(rec.get("name") or "").lower()
        desc = str(rec.get("desc") or "").lower()
        name_hits = sum(1 for w in words if w in name)
        desc_hits = sum(desc.count(w) for w in words)
        return (-name_hits, -desc_hits, str(rec.get("key") or ""))

    hits = [r for r in all_rules() if score(r)[:2] != (0, 0)]
    hits.sort(key=score)
    return hits[:max(0, int(limit))]


# --------------------------------------------------------------------------- #
# 摘录
# --------------------------------------------------------------------------- #

_QUOTES = (("\u2019", "'"), ("\u2018", "'"),
           ("\u201c", '"'), ("\u201d", '"'))


def _clean(text: str) -> str:
    """去 markdown 粗体、统一引号、压空白。**不改写内容。**"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for bad, good in _QUOTES:
        t = t.replace(bad, good)
    t = t.replace("**", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def _excerpt(desc: str, *, section: str = "", limit: int = 260) -> str:
    """截取原文。有 `section` 就从那一段开始；否则从头。

    截到句子边界为止（避免半句话）；实在没有边界就加省略号。
    """
    t = _clean(desc)
    if section:
        i = t.lower().find(section.lower())
        if i >= 0:
            t = t[i:]
    if not t:
        return ""
    if len(t) <= limit:
        return t
    cut = t[:limit]
    idx = -1
    for mark in (". ", "。", "! ", "? ", ".\n"):
        idx = max(idx, cut.rfind(mark))
    if idx >= max(40, int(limit * 0.5)):
        return cut[:idx + 1].strip()
    return cut.rstrip() + "…"


def _pick_key(topic) -> tuple[str, str, int]:
    """把主题解析成 (key, section, limit)。不认识的返回空 key。"""
    if isinstance(topic, RulePick):
        return topic.key, topic.section, topic.limit
    if isinstance(topic, (tuple, list)) and topic:
        key = str(topic[0])
        section = str(topic[1]) if len(topic) > 1 else ""
        limit = int(topic[2]) if len(topic) > 2 else 260
        return key, section, limit
    return str(topic), "", 260


# --------------------------------------------------------------------------- #
# 拼块
# --------------------------------------------------------------------------- #

def build_rules_text(*, phase: str = "explore", topics=None,
                     max_chars: int = 1700) -> str:
    """按场景或指定主题拼规则块。**超预算就跳过并说明，不硬塞。**"""
    if topics:
        picks = [_pick_key(t) for t in topics]
        title = "指定主题"
    else:
        ph = phase if phase in PHASE_RULES else "explore"
        picks = [(p.key, p.section, p.limit) for p in PHASE_RULES[ph]]
        title = _PHASE_CN.get(ph, ph)

    max_chars = max(200, int(max_chars))
    lines = [f"【规则摘录 · {title}（SRD 2024）】"]
    used = len(lines[0])
    # 给收尾两行（引擎口径 + 出处）留位置，保证整块不超预算
    body_budget = max(120, max_chars - len(CORE_LINE) - len(FOOTER) - 4)
    missing: list[str] = []
    skipped = 0

    for key, section, limit in picks:
        rec = get_rule(key)
        if rec is None:
            missing.append(key)
            continue
        name = str(rec.get("name") or key)
        body = _excerpt(str(rec.get("desc") or ""), section=section, limit=limit)
        if not body:
            missing.append(key)
            continue
        line = f"· {name}：{body}"
        room = body_budget - used
        if len(line) > room:
            if room >= 120:
                line = line[:room].rstrip() + "…"
            else:
                skipped += 1
                continue
        lines.append(line)
        used += len(line) + 1

    lines.append(CORE_LINE)
    if missing:
        lines.append("（找不到规则：" + "、".join(missing) + " —— 不编造，交 DM 裁定）")
    if skipped:
        lines.append(f"（还有 {skipped} 条规则因字数预算未收入）")
    lines.append(FOOTER)
    return "\n".join(lines)


@lru_cache(maxsize=64)
def _cached_rules(phase: str, topics: tuple, max_chars: int) -> str:
    return build_rules_text(phase=phase,
                            topics=(list(topics) or None),
                            max_chars=max_chars)


def rules_text_for(phase: str = "explore", topics=None,
                   max_chars: int = 1700) -> str:
    """`build_rules_text` 的缓存版（主题需是可哈希的字符串序列）。"""
    key = tuple(str(t) for t in (topics or ()))
    return _cached_rules(str(phase), key, int(max_chars))