# coding=utf-8
"""线索检定：把「幕后事件」变成玩家**可能**发现的痕迹（P5-G2）。

用户 2026-11-05 拍板：**走检定，不走"DM 主动投放"**。
所以这一层不是"DM 随手丢一条提示"，而是"引擎守着一扇门"：

    · 痕迹写在剧本里（哪条痕迹、要什么技能、DC 多少、发现后给谁看）；
    · 玩家**主动**观察 / 搜索时，引擎才掷 `d20 + 技能加值 vs DC`；
    · 成功才把痕迹交到玩家手上；
    · 失败**连"这里有东西"都不说** —— 台账里只有"你没发现异常"；
    · 同一个人对同一条线索只能试一次（不许反复掷骰到成功）。

为什么要"检定"而不是"DM 投放"：
    投放是 DM 的心软，检定是玩家的选择。前者会让"有没有线索"取决于 DM 今天的心情，
    后者让"察觉"这个技能真的有用 —— 而骰子仍然由代码掷（D2 那条纪律）。

分工：
    clues.py   数据形状 + 一次检定的结果（**纯函数，不碰 World**）
    loop.py    接线（玩家动作 → 找线索 → 掷骰 → 写台账/记忆）
    world.py   存"哪条已发现、谁试过"（走 commit，有审计）
"""

from __future__ import annotations

from dataclasses import dataclass

from .adjudicate import skill_check
from .constants import SKILL_ABILITY, SKILL_CN, exhaustion_modifier

__all__ = [
    "CLUE_ACTIONS", "ClueError", "SceneClue", "ClueOutcome",
    "parse_clue", "check_clue", "check_scene_clue", "clue_outcome",
    "success_text", "failure_text", "skill_bonus", "passive_score",
]

#: 哪种日常动作能触发哪种技能。**没有映射的技能 = 这版触发不了**（明说，不假装）。
CLUE_ACTIONS: dict[str, str] = {
    "perception": "look",        # 感知（察觉）→ 观察
    "investigation": "search",   # 智力（调查）→ 搜索
}

#: 线索文字的下限（去掉标点后的实字数）。太短 = 没写清楚看到了什么。
MIN_TEXT_CHARS = 10


class ClueError(ValueError):
    """线索写错了。逐条说原因，不静默跳过。"""


@dataclass(frozen=True)
class SceneClue:
    """一条可发现的痕迹（剧本里写，引擎执行）。"""
    id: str
    text: str                  # 发现后玩家/ DM 看到的那句话（**就是"映射"的落点**）
    skill: str = "perception"   # 感知（察觉）/ 智力（调查）
    dc: int = 12
    #: True = 只有发现者 + DM 知道；False = 一发现就全场公开（如地上的拖痕）
    secret: bool = True
    #: 在哪个地点才可能撞见；空 = 本场场景地点
    place: str = ""
    #: 这条痕迹对应哪段离场简报（只给 DM，用来把幕后和眼前对上）
    source: str = ""

    @property
    def action(self) -> str:
        """触发它需要哪个日常动作（`look` / `search`）；触发不了返回空串。"""
        return CLUE_ACTIONS.get(self.skill, "")

    @property
    def skill_cn(self) -> str:
        return SKILL_CN.get(self.skill, self.skill)


def parse_clue(raw, where: str = "") -> SceneClue:
    """把 JSON 里的一条线索变成对象。**只做类型解析，语义交给 `check_clue`。**

    ⚠️ `secret` **只认真布尔**。写 `"secret": "false"` / `0` / `null` 一律报错 ——
    以前用 `bool(raw.get(...))`，"false" 这种非空字符串会被当成 True，
    把"公开痕迹"静默变成"秘密痕迹"（反过来 0 也会静默变公开）。
    """
    if not isinstance(raw, dict):
        raise ClueError(f"{where}: 线索必须是对象，拿到 {type(raw).__name__}")
    skill = str(raw.get("skill") or "perception").strip().lower()
    try:
        dc = int(raw.get("dc", 12))
    except (TypeError, ValueError) as exc:
        raise ClueError(f"{where}: dc 必须是整数") from exc
    secret = raw.get("secret", True)
    if not isinstance(secret, bool):
        raise ClueError(
            f"{where}: secret 必须是 true/false（拿到 {secret!r}）—— "
            f"字符串 \"false\" / 0 不会被当成 false")
    return SceneClue(
        id=str(raw.get("id") or "").strip(),
        text=str(raw.get("text") or "").strip(),
        skill=skill,
        dc=dc,
        secret=secret,
        place=str(raw.get("place") or "").strip(),
        source=str(raw.get("source") or "").strip(),
    )


def check_clue(raw, where: str = "") -> list[str]:
    """校验一条线索（原始 JSON）。返回错误清单（空 = 通过）。"""
    try:
        c = parse_clue(raw, where)
    except ClueError as exc:
        return [str(exc)]
    return check_scene_clue(c, where)


def check_scene_clue(c: SceneClue, where: str = "") -> list[str]:
    """校验已经解析好的一条线索。"""
    errors: list[str] = []
    if not c.id:
        errors.append(f"{where}: 线索缺少 id")
    plain = "".join(ch for ch in c.text if ch not in " \t\r\n，。、；：！？,.!?;:-—…\"'“”「」()（）")
    if not c.text:
        errors.append(f"{where}: 线索缺少 text（发现后玩家看到什么）")
    elif len(plain) < MIN_TEXT_CHARS:
        errors.append(f"{where}: 线索 text 太短（{len(plain)} 实字，至少 "
                      f"{MIN_TEXT_CHARS}）—— 写清「看到了什么 / 听到了什么」")
    if c.skill not in CLUE_ACTIONS:
        errors.append(f"{where}: skill 只能是 {sorted(CLUE_ACTIONS)}，"
                      f"拿到 {c.skill!r}（其它技能这版触发不了）")
    if not (5 <= c.dc <= 30):
        errors.append(f"{where}: dc 应在 5~30，拿到 {c.dc}")
    return errors


# --------------------------------------------------------------------------- #
# 一次检定
# --------------------------------------------------------------------------- #

@dataclass
class ClueOutcome:
    """一次线索检定的结果。**失败时 `found=False`，且不含任何线索内容。**"""
    clue: SceneClue
    found: bool
    total: int
    dc: int
    roll_summary: str = ""
    place: str = ""

    def visible_to(self, who: str) -> str:
        """发现后给谁看：公开的给全场，秘密的只给发现者（+DM 的可见性规则）。"""
        return "public" if not self.clue.secret else f"pc:{who}"

    def __str__(self) -> str:
        return (f"{self.clue.skill_cn}检定 → {self.total} vs DC {self.dc} → "
                f"{'发现痕迹' if self.found else '没发现异常'}")


def skill_bonus(unit, skill: str) -> int:
    """与 `skill_check` 完全同一份加值口径（`skill_bonuses` 优先，否则属性调整）。"""
    s = (skill or "").lower().replace(" ", "_")
    if s in unit.skill_bonuses:
        return int(unit.skill_bonuses[s]) + exhaustion_modifier(unit.exhaustion)
    ability = SKILL_ABILITY.get(s)
    if ability is None:
        raise ClueError(f"不认识的技能：{skill!r}")
    return unit.mod(ability) + exhaustion_modifier(unit.exhaustion)


def passive_score(unit, skill: str = "perception") -> int:
    """被动值 = 10 + 技能总加值。**2024 规则：站着不动也按这个数比 DC。**"""
    return 10 + skill_bonus(unit, skill)


def clue_outcome(unit, clue: SceneClue, rng, *, place: str = "") -> ClueOutcome:
    """掷一次。**成功/失败由骰子与 DC 决定，不由调用方决定。**"""
    r = skill_check(unit, clue.skill, clue.dc, rng)
    return ClueOutcome(clue=clue, found=bool(r.check.success),
                       total=int(r.check.total), dc=int(clue.dc),
                       roll_summary=r.check.a.summary(), place=place)


# --------------------------------------------------------------------------- #
# 两句话：给玩家的那半句、和给 DM 的那半句
# --------------------------------------------------------------------------- #

def success_text(outcome: ClueOutcome, who: str) -> str:
    """发现者看到的话。**公开线索用第三人称（别人也看得到）。**"""
    if outcome.clue.secret:
        return f"你发现了：{outcome.clue.text}"
    return f"{who}发现了：{outcome.clue.text}"


def failure_text() -> str:
    """失败时给玩家的话。**与"这里有没有东西"完全无关** —— 有没有都用这一句。"""
    return "你仔细看过、听过，没发现什么异常。"