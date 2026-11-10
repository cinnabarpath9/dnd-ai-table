# coding=utf-8
"""NPC 人格与后台状态：五骰生成 + 情绪/目标/底线/知情。

借自那张卡里「五骰人格引擎」的思路，**改成 D&D 口径、去掉 RP 专属内容**：

    五个 D12：核心动机 / 行为面具 / 暗面破绽 / 核心欲望 / 核心恐惧

五骰不是标签，是**行为约束**：面具决定"平时怎么演"，破绽决定"哪里会失控"，
欲望/恐惧决定"追什么、躲什么"。骰子由 `engine/dice` 掷，**模型不能自己编人格**。

后台状态（`NpcState`）只记引擎能回答的事：

    emotion 情绪+强度 / goal 短中长期目标 / boundary 底线
    impression 对他人的判断 / knowledge 已知·未知·误解 / secrets / memory

三条纪律（照抄项目已有的反全知口径）：

1. **无信息不得知晓**：`knowledge` 只有 `已知/未知/误解` 三态，不给"全知"留口子；
2. **秘密只给 DM**：人格与状态写在世界树里，`World.render` 只在 DM 视角渲染；
3. **不自动猜**：情绪/目标不会因为"剧情应该紧张"就自己变；要变就显式调用更新。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..dice import Random, roll

__all__ = [
    "MOTIVATIONS", "MASKS", "FLAWS", "DESIRES", "FEARS",
    "NpcPersona", "NpcState", "roll_persona", "persona_block",
]

#: 核心动机（为什么做）
MOTIVATIONS = (
    "秩序型：渴望规则与稳定，恐惧混乱失控",
    "助人型：渴望被需要，靠付出确认自己的价值",
    "成就型：渴望认可，把自我价值绑在结果上",
    "独特型：渴望真实与不同，怕被当成普通",
    "探索型：渴望理解与掌控，习惯囤积信息、保持距离",
    "忠诚型：渴望安全可靠，预判风险、反复确认",
    "享乐型：渴望自由新鲜，用快乐回避难受",
    "掌控型：渴望力量与自主，怕被控制",
    "平和型：渴望和谐安宁，回避冲突、偏被动",
    "完美型：渴望正确完善，对己对人都苛刻",
    "自保型：渴望生存与资源，务实利己",
    "虚无型：渴望意义与答案，疏离超脱",
)

#: 行为面具（怎么做）
MASKS = (
    "清冷疏离：话少表情淡，不主动不回应",
    "温和客套：礼貌周全，永远保持安全距离",
    "张扬跳脱：话多活跃，情绪外放",
    "严谨刻板：一板一眼，讲规则讲流程",
    "风趣幽默：爱开玩笑，用玩笑回避真心",
    "沉默寡言：只做不说，情绪不外露",
    "强势霸道：气场强，直接命令，喜欢掌控局面",
    "体贴周到：先照顾别人感受，习惯迁就",
    "尖酸刻薄：嘴毒爱怼，习惯性否定",
    "散漫慵懒：慢吞吞无所谓，怎么都行",
    "世故圆滑：见人说人话，擅长打圆场",
    "腼腆羞怯：容易紧张，说话小声，回避目光",
)

#: 暗面破绽（哪里失控）+ 触发点
FLAWS = (
    ("控制欲", "别人乱动他的东西、计划被打乱、局面脱离掌控"),
    ("自卑情结", "被当众评价、被拿来比较、被否定能力"),
    ("道德洁癖", "有人破坏规则、看到不公正、信仰被质疑"),
    ("恐惧失控", "突发意外、计划外变化、信息不足"),
    ("过往创伤", "特定词语、场景、日期，或相似的人"),
    ("执念深重", "执念对象被威胁、被要求放弃、时间紧迫"),
    ("情感冷漠", "别人表达深情、被要求付出真心、关系拉近"),
    ("过度负责", "别人出错、有人受伤、任务失败"),
    ("猜忌多疑", "别人私下交谈、被隐瞒信息、过于热情"),
    ("理想主义", "发现现实不完美、信仰崩塌、理想被嘲笑"),
    ("享乐逃避", "压力增大、困难出现、需要做决定"),
    ("脆弱敏感", "被戳中痛处、被误解、感受到敌意"),
)

#: 核心欲望（追什么）
DESIRES = (
    "被爱", "被认可", "自由", "安全", "掌控", "知识",
    "归属", "赎罪", "复仇", "平静", "财富", "真相",
)

#: 核心恐惧（躲什么）
FEARS = (
    "被抛弃", "失败", "失去控制", "被看穿", "死亡", "平庸",
    "背叛", "孤独", "欠下还不清的债", "再次失控",
    "失去所爱", "一切毫无意义",
)


def _pick12(table, rng: Random):
    r = roll("1d12", rng)
    idx = max(1, min(12, int(r.total)))
    return table[idx - 1]


@dataclass
class NpcPersona:
    """一个 NPC 的五骰结果 + 由它推出的矛盾线。"""
    name: str
    role: str = ""
    motivation: str = ""
    mask: str = ""
    flaw: str = ""
    flaw_trigger: str = ""
    desire: str = ""
    fear: str = ""
    conflict: str = ""

    def to_dict(self) -> dict:
        return {
            "姓名": self.name, "身份": self.role,
            "核心动机": self.motivation, "行为面具": self.mask,
            "暗面破绽": self.flaw, "破绽触发": self.flaw_trigger,
            "核心欲望": self.desire, "核心恐惧": self.fear,
            "矛盾线": self.conflict,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NpcPersona":
        g = lambda k: str((d or {}).get(k) or "")
        return cls(name=g("姓名"), role=g("身份"), motivation=g("核心动机"),
                   mask=g("行为面具"), flaw=g("暗面破绽"),
                   flaw_trigger=g("破绽触发"), desire=g("核心欲望"),
                   fear=g("核心恐惧"), conflict=g("矛盾线"))


def roll_persona(rng: Random, *, name: str, role: str = "") -> NpcPersona:
    """掷五个 D12，生成一个人格。**所有随机性都走 dice 包。**"""
    motivation = _pick12(MOTIVATIONS, rng)
    mask = _pick12(MASKS, rng)
    flaw, trigger = _pick12(FLAWS, rng)
    desire = _pick12(DESIRES, rng)
    fear = _pick12(FEARS, rng)
    conflict = (f"想要「{desire}」，又怕「{fear}」；"
                f"对外用「{mask.split('：')[0]}」遮住「{flaw}」，"
                f"一旦{trigger}就会破功。")
    return NpcPersona(name=str(name), role=str(role), motivation=motivation,
                      mask=mask, flaw=flaw, flaw_trigger=trigger,
                      desire=desire, fear=fear, conflict=conflict)


@dataclass
class NpcState:
    """后台状态机。**只记引擎能回答的事实，不写心理小说。**"""
    emotion: str = "平静"
    intensity: int = 30
    goal_short: str = ""
    goal_mid: str = ""
    goal_long: str = ""
    boundary: str = ""
    impression: dict[str, str] = field(default_factory=dict)
    #: 每条形如 {"fact": "...", "state": "已知"|"未知"|"误解"}
    knowledge: list[dict] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    memory: list[str] = field(default_factory=list)

    def know(self, fact: str, state: str = "已知") -> None:
        if state not in ("已知", "未知", "误解"):
            raise ValueError(f"knowledge.state 只能是 已知/未知/误解，拿到 {state!r}")
        fact = str(fact).strip()
        if not fact:
            return
        for row in self.knowledge:
            if row.get("fact") == fact:
                row["state"] = state
                return
        self.knowledge.append({"fact": fact, "state": state})

    def render(self) -> str:
        bits = [f"情绪 {self.emotion}({max(0, min(100, int(self.intensity)))})"]
        if self.goal_short:
            bits.append(f"短期目标：{self.goal_short}")
        if self.boundary:
            bits.append(f"底线：{self.boundary}")
        lines = ["  " + "｜".join(bits)]
        if self.impression:
            lines.append("  对他人的判断：" + "；".join(
                f"{k}={v}" for k, v in self.impression.items()))
        known = [x["fact"] for x in self.knowledge if x.get("state") == "已知"]
        wrong = [x["fact"] for x in self.knowledge if x.get("state") == "误解"]
        if known:
            lines.append("  已知：" + "；".join(known))
        if wrong:
            lines.append("  误解：" + "；".join(wrong))
        if self.secrets:
            lines.append("  🔒秘密：" + "；".join(self.secrets))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "情绪": self.emotion, "强度": int(self.intensity),
            "短期目标": self.goal_short, "中期目标": self.goal_mid,
            "长期目标": self.goal_long, "底线": self.boundary,
            "印象": dict(self.impression),
            "知情": [dict(x) for x in self.knowledge],
            "秘密": list(self.secrets), "记忆": list(self.memory),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NpcState":
        d = d or {}
        try:
            intensity = int(d.get("强度", 30))
        except (TypeError, ValueError):
            intensity = 30
        return cls(
            emotion=str(d.get("情绪") or "平静"),
            intensity=max(0, min(100, intensity)),
            goal_short=str(d.get("短期目标") or ""),
            goal_mid=str(d.get("中期目标") or ""),
            goal_long=str(d.get("长期目标") or ""),
            boundary=str(d.get("底线") or ""),
            impression={str(k): str(v) for k, v in (d.get("印象") or {}).items()},
            knowledge=[dict(x) for x in (d.get("知情") or [])],
            secrets=[str(x) for x in (d.get("秘密") or [])],
            memory=[str(x) for x in (d.get("记忆") or [])],
        )


def persona_block(persona: NpcPersona, state: NpcState | None = None) -> str:
    """DM 视角的一段 NPC 卡（**玩家视角不会渲染**）。"""
    lines = [
        f"【NPC · {persona.name}｜{persona.role or '身份未定'}】",
        f"  核心动机：{persona.motivation}",
        f"  行为面具：{persona.mask}",
        f"  暗面破绽：{persona.flaw}（触发：{persona.flaw_trigger}）",
        f"  欲望 / 恐惧：{persona.desire} / {persona.fear}",
        f"  矛盾线：{persona.conflict}",
    ]
    if state is not None:
        lines.append(state.render())
    return "\n".join(lines)