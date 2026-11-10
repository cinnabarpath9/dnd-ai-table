# coding=utf-8
"""NPC 事件驱动：挨打 / 被欺骗 / 被帮助 / 同伙倒下 怎么改后台状态。

P5-F 只做了"状态长什么样"，这一层补"状态什么时候变"：

    damage     挨打：情绪按剩余血量走恐惧/愤怒，对打人者记敌意，知情记一笔
    deceived   被欺骗：情绪转猜忌，对说谎者记"不可信"，谎言进「误解」
    helped     被帮助：情绪转感激，对帮手记友善
    ally_down  同伴倒下：情绪变恐惧/愤怒，对凶手记敌意

**全部是代码规则，不调模型**；改完写回世界树（`World.update_npc_state`），有审计。

⚠️ 它不是"心理模拟"：只改四个字段（情绪/强度/印象/知情），不写长段心理活动，
也不替 DM 决定 NPC 下一句说什么。
"""

from __future__ import annotations

from .persona import NpcState

__all__ = ["EVENT_KINDS", "apply_event", "event_note"]

EVENT_KINDS = ("damage", "deceived", "helped", "ally_down")


def _req(event: dict, key: str) -> str:
    v = str((event or {}).get(key) or "").strip()
    if not v:
        raise ValueError(f"事件缺少 {key!r}")
    return v


def _clamp(v: int) -> int:
    return max(0, min(100, int(v)))


def apply_event(state: dict, event: dict) -> tuple[dict, list[str]]:
    """把一次事件应用到 NPC 状态，返回 (新状态 dict, 给日志的说明行)。"""
    if not isinstance(event, dict):
        raise ValueError("事件必须是对象")
    kind = str(event.get("kind") or "").strip()
    if kind not in EVENT_KINDS:
        raise ValueError(f"不认识的事件 kind={kind!r}（可选 {EVENT_KINDS}）")
    st = NpcState.from_dict(state or {})
    notes: list[str] = []

    if kind == "damage":
        source = _req(event, "source")
        try:
            amount = max(0, int(event.get("amount", 0)))
        except (TypeError, ValueError):
            amount = 0
        try:
            ratio = float(event.get("hp_ratio", 1.0))
        except (TypeError, ValueError):
            ratio = 1.0
        if ratio <= 0.25:
            st.emotion, st.intensity = "恐惧", _clamp(50 + amount * 2)
            notes.append(f"{st.emotion}({st.intensity})：血快见底了")
        else:
            st.emotion, st.intensity = "愤怒", _clamp(30 + amount * 2)
            notes.append(f"{st.emotion}({st.intensity})：被打了 {amount} 点")
        st.impression[source] = "敌意"
        st.know(f"{source} 攻击了我")
        if not st.goal_short:
            st.goal_short = "先解决眼前的威胁"
        st.memory.append(f"被 {source} 打了 {amount} 点（剩余血量 {ratio:.0%}）")
        notes.append(f"对 {source} 的印象：敌意")

    elif kind == "deceived":
        by = _req(event, "by")
        lie = _req(event, "lie")
        st.emotion = "猜忌"
        st.intensity = max(st.intensity, 60)
        st.impression[by] = "不可信"
        st.know(lie, "误解")
        truth = str(event.get("truth") or "").strip()
        if truth:
            st.know(truth, "已知")
        st.memory.append(f"{by} 对我说了「{lie}」（我信了）")
        notes.append(f"{by} 的谎言进了「误解」：{lie}")
        if truth:
            notes.append(f"同时知道了真相：{truth}")

    elif kind == "helped":
        by = _req(event, "by")
        st.emotion = "感激"
        st.intensity = max(st.intensity, 50)
        st.impression[by] = "友善"
        note = str(event.get("note") or "帮了我一把").strip()
        st.memory.append(f"{by} {note}")
        notes.append(f"对 {by} 的印象：友善")

    else:  # ally_down
        who = _req(event, "who")
        by = str(event.get("by") or "").strip()
        st.emotion = "愤怒" if st.intensity >= 50 else "恐惧"
        st.intensity = _clamp(max(st.intensity, 70))
        if by:
            st.impression[by] = "敌意"
            st.know(f"{by} 打倒了 {who}")
        st.memory.append(f"{who} 倒下了" + (f"（{by} 干的）" if by else ""))
        notes.append(f"{who} 倒下 → {st.emotion}({st.intensity})")

    return st.to_dict(), notes


def event_note(kind: str, notes: list[str]) -> str:
    """拼一行给台账/日志的说明。"""
    return f"NPC 事件[{kind}]：" + "；".join(notes or ["（无变化）"])