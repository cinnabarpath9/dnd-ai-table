# coding=utf-8
"""骰子与骰式。

这是"代码说了算"的第一道闸：**所有随机性只从这里出**，而且必须可复现（给定种子跑出同一局）。
模型永远不掷骰、也不算骰——它只声明意图。

支持的骰式：
    "d20"  "2d6"  "1d8+3"  "4d6-1"  "3d6+2d4+5"  "d%"  "2d20kh1"  "2d20kl1"
    "4d6dl1"（丢最低一颗）  "8d6/2"（先掷再整除）

关键规则口径（D&D 2024）：
    · **天然 20 / 天然 1 只看裸骰**（`natural`），与加值无关；见角色卡世界书「骰子规则」的强调。
    · 重击（Critical Hit）：攻击检定天然 20（或重击范围命中）→ **伤害骰数量翻倍**，加值不翻倍。
    · 优势/劣势：掷两个 d20 取高/取低；**同时有优劣势则抵消，只掷一个**。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

__all__ = [
    "RollMods", "Roll", "roll", "roll_d20", "parse", "RollError",
    "AbilityCheck", "advantage_of",
]


class RollError(ValueError):
    """骰式写错时抛这个 —— 不要静默当成 0，那会把 bug 藏起来。"""


# --------------------------------------------------------------------------- #
# 骰式解析
# --------------------------------------------------------------------------- #

_TERM = re.compile(r"^\s*(\d*)\s*[dD]\s*(\d+|%)\s*([a-zA-Z]*)(\d*)\s*$")
_MOD = re.compile(r"^\s*([+-])\s*(\d+)\s*$")


@dataclass(frozen=True)
class DiceTerm:
    """一个 XdY 项，可带 kh/kl/dl 后缀。"""
    count: int
    faces: int
    keep: str = ""      # "" | "kh" | "kl" | "dl"
    keep_n: int = 1

    def __str__(self) -> str:
        s = f"d{self.faces}" if self.count == 1 else f"{self.count}d{self.faces}"
        if self.keep:
            s += f"{self.keep}{self.keep_n}"
        return s


@dataclass(frozen=True)
class RollMods:
    """一次掷骰的修饰。"""
    advantage: bool = False
    disadvantage: bool = False
    crit_range: int = 20        # 天然 >= 此值即重击（默认只 20）
    crit_fail: int = 1          # 天然 <= 此值即必失败（默认只 1）

    def resolved_advantage(self) -> int:
        """+1 优势 / -1 劣势 / 0 平常。**两者同时存在则抵消。**"""
        if self.advantage and not self.disadvantage:
            return 1
        if self.disadvantage and not self.advantage:
            return -1
        return 0


@dataclass
class Roll:
    """一次掷骰的完整留痕。`detail` 保留每一颗骰子的原始面值，便于复盘。"""
    notation: str
    terms: list[DiceTerm]
    modifier: int = 0
    halves: int = 0                     # 末尾 /2 的层数
    detail: list[list[int]] = field(default_factory=list)
    dropped: list[list[int]] = field(default_factory=list)
    kept: list[list[int]] = field(default_factory=list)
    natural: int | None = None          # 若是单颗 d20，记裸骰（判天然 20/1 用）
    advantage_rolls: list[int] = field(default_factory=list)   # 优/劣势时两颗 d20
    mods: RollMods = field(default_factory=RollMods)
    display_modifier: int | None = None  # 摘要里显示的加值；**不参与 total**
    seed_note: str = ""

    # ---- 结果 ----
    @property
    def dice_total(self) -> int:
        return sum(sum(t) for t in self.kept)

    @property
    def total(self) -> int:
        """骰子自己的总点数。

        ⚠️ **不含 `display_modifier`。** 加值的唯一来源是 `AbilityCheck.bonus`，
        在这里再加一次就是"加值算两遍"——那是真出过的 bug（回归 D3~D5 钉住它）。
        """
        v = self.dice_total + self.modifier
        for _ in range(self.halves):
            v //= 2
        return v

    @property
    def is_crit(self) -> bool:
        return self.natural is not None and self.natural >= self.mods.crit_range

    @property
    def is_crit_fail(self) -> bool:
        return self.natural is not None and self.natural <= self.mods.crit_fail

    def summary(self) -> str:
        """给人看/给模型看的一行摘要。"""
        parts = []
        for term, d, k in zip(self.terms, self.detail, self.kept):
            if term.keep:
                parts.append(f"{term}[{','.join(map(str, d))}→{sum(k)}]")
            else:
                parts.append(f"{term}[{'+'.join(map(str, d))}]")
        body = " + ".join(parts)
        shown_mod = self.display_modifier if self.display_modifier is not None else self.modifier
        if shown_mod:
            body += f" {shown_mod:+d}"
        s = f"{self.notation} = {body} = {self.dice_total + shown_mod}"
        if self.halves:
            s += f"，÷2 → {self.total}"
        if self.advantage_rolls:
            s += f"（{'优势' if self.mods.resolved_advantage() > 0 else '劣势'}，"
            s += f"两颗 {self.advantage_rolls} 取 {'高' if self.mods.resolved_advantage() > 0 else '低'}）"
        if self.is_crit:
            s += " **天然20**"
        elif self.is_crit_fail:
            s += " **天然1**"
        return s

    def to_dict(self) -> dict:
        return {
            "notation": self.notation,
            "total": self.total,
            "dice_total": self.dice_total,
            "modifier": self.modifier,
            "natural": self.natural,
            "is_crit": self.is_crit,
            "is_crit_fail": self.is_crit_fail,
            "detail": self.detail,
            "kept": self.kept,
            "dropped": self.dropped,
            "advantage_rolls": self.advantage_rolls,
            "summary": self.summary(),
        }


def parse(notation: str) -> tuple[list[DiceTerm], int, int]:
    """把骰式拆成 (骰项列表, 固定加值, /2 层数)。"""
    raw = notation.strip().lower().replace(" ", "")
    if not raw:
        raise RollError("空骰式")

    halves = 0
    while raw.endswith("/2"):
        halves += 1
        raw = raw[:-2]

    # 在 + / - 处切段。**符号要跟在后一段前面**（`3d6+2d4` → ["3d6", "+2d4"]），
    # 否则第二段只剩 "2d4"、固定加值只剩 "+5"，本来能解析的式子会被判非法。
    # 开头符号（如 "-2"）属于第一段。
    segs: list[str] = []
    buf = ""
    for i, ch in enumerate(raw):
        if ch in "+-" and i > 0:
            segs.append(buf)
            buf = ch
        else:
            buf += ch
    if buf:
        segs.append(buf)

    terms: list[DiceTerm] = []
    modifier = 0
    for seg in segs:
        if not seg:
            continue
        if seg[0] in "+-":
            # 带符号的段：先看符号后面是不是骰子项（如 "+2d4"）
            body = seg[1:]
            m_signed = _TERM.match(body)
            if m_signed:
                cnt_s, faces_s, keep, keep_n = m_signed.groups()
                count = int(cnt_s) if cnt_s else 1
                faces = 100 if faces_s == "%" else int(faces_s)
                if count < 1:
                    raise RollError(f"骰子颗数必须 ≥1：{seg}")
                if faces < 2:
                    raise RollError(f"骰面数必须 ≥2：{seg}")
                keep = keep.lower()
                if keep not in ("", "kh", "kl", "dl"):
                    raise RollError(f"不支持的后缀 {keep!r}（只支持 kh/kl/dl）：{seg}")
                n = int(keep_n) if keep_n else 1
                if keep in ("kh", "kl") and n >= count:
                    raise RollError(f"保留数 {n} 不小于骰子颗数 {count}：{seg}")
                # 负号骰子（如 "-1d4"）：当作扣减项会破坏"骰子总点数"的口径，
                # SRD 里没有这种写法 —— 明确拒绝，别猜。
                if seg[0] == "-":
                    raise RollError(f"不支持负骰子项：{seg}（加值才能是负的）")
                terms.append(DiceTerm(count, faces, keep, n))
                continue

        m = _TERM.match(seg)
        if m:
            cnt_s, faces_s, keep, keep_n = m.groups()
            count = int(cnt_s) if cnt_s else 1
            faces = 100 if faces_s == "%" else int(faces_s)
            if count < 1:
                raise RollError(f"骰子颗数必须 ≥1：{seg}")
            if faces < 2:
                raise RollError(f"骰面数必须 ≥2：{seg}")
            keep = keep.lower()
            if keep not in ("", "kh", "kl", "dl"):
                raise RollError(f"不支持的后缀 {keep!r}（只支持 kh/kl/dl）：{seg}")
            n = int(keep_n) if keep_n else 1
            if keep and n >= count and keep in ("kh", "kl"):
                raise RollError(f"保留数 {n} 不小于骰子颗数 {count}：{seg}")
            terms.append(DiceTerm(count, faces, keep, n))
            continue
        m = _MOD.match(seg)
        if m:
            sign, val = m.groups()
            modifier += int(val) if sign == "+" else -int(val)
            continue
        raise RollError(f"看不懂的骰式片段：{seg!r}（整式 {notation!r}）")

    if not terms:
        raise RollError(f"骰式里没有任何骰子：{notation!r}")
    return terms, modifier, halves


# --------------------------------------------------------------------------- #
# 掷骰
# --------------------------------------------------------------------------- #

def roll(notation: str, rng: random.Random,
         mods: RollMods | None = None) -> Roll:
    """按骰式掷一次。`rng` 必须由调用方传入 —— 保证同种子可复现。"""
    mods = mods or RollMods()
    terms, modifier, halves = parse(notation)
    r = Roll(notation=notation, terms=terms, modifier=modifier,
             halves=halves, mods=mods)

    # 单颗 d20 + 优/劣势 → 掷两次取高/低，并记下裸骰
    single_d20 = (len(terms) == 1 and terms[0].count == 1
                  and terms[0].faces == 20 and not terms[0].keep)
    adv = mods.resolved_advantage()
    if single_d20 and adv != 0:
        a = rng.randint(1, 20)
        b = rng.randint(1, 20)
        r.advantage_rolls = [a, b]
        nat = max(a, b) if adv > 0 else min(a, b)
        r.detail = [[a, b]]
        r.kept = [[nat]]
        r.dropped = [[min(a, b) if adv > 0 else max(a, b)]]
        r.natural = nat
        return r

    for term in terms:
        faces = [rng.randint(1, term.faces) for _ in range(term.count)]
        r.detail.append(faces)
        if term.keep == "kh":
            n = term.keep_n
            order = sorted(range(len(faces)), key=lambda i: faces[i], reverse=True)
            keep_idx = set(order[:n])
        elif term.keep == "kl":
            n = term.keep_n
            order = sorted(range(len(faces)), key=lambda i: faces[i])
            keep_idx = set(order[:n])
        elif term.keep == "dl":
            n = term.keep_n
            order = sorted(range(len(faces)), key=lambda i: faces[i], reverse=True)
            keep_idx = set(order[:len(faces) - n]) if n < len(faces) else set()
        else:
            keep_idx = set(range(len(faces)))
        r.kept.append([faces[i] for i in sorted(keep_idx)])
        r.dropped.append([faces[i] for i in range(len(faces)) if i not in keep_idx])

    if single_d20 and not term_keeps_extra(terms[0]):
        r.natural = r.kept[0][0]
    return r


def term_keeps_extra(term: DiceTerm) -> bool:
    return bool(term.keep)


def roll_d20(rng: random.Random, modifier: int = 0,
             mods: RollMods | None = None) -> Roll:
    """掷一颗 d20（检定/攻击/豁免都走这里）。

    ⚠️ **`modifier` 只用于展示摘要，不参与 `total`。**
    加值的唯一来源是 `AbilityCheck.bonus` —— 加值只算一次，绝不在两处各算一次。
    这里传 modifier 只是为了让 `summary()` 能写出"d20+3 = [12] +3 = 15"。
    """
    r = roll("d20", rng, mods)
    r.display_modifier = int(modifier)   # 仅展示；total 是裸骰
    return r


def advantage_of(*flags: bool) -> RollMods:
    """把一串布尔折成 RollMods（有优势、有劣势 → 抵消）。"""
    return RollMods(advantage=any(flags[0::2]), disadvantage=any(flags[1::2]))


# --------------------------------------------------------------------------- #
# 一次检定的结果（检定/攻击/豁免共用）
# --------------------------------------------------------------------------- #

@dataclass
class AbilityCheck:
    """一次 d20 检定的完整结果。

    D&D 2024 的判定只有三种形态，全都落在这里：
      · 属性/技能检定：`dc` 是难度
      · 攻击检定：     `dc` 是目标 AC
      · 豁免检定：     `dc` 是施法者豁免 DC
    """
    kind: str                   # "check" | "attack" | "save"
    label: str                  # 给人看的一句，如 "隐匿检定"
    a: Roll                     # d20 那颗
    bonus: int                  # 总加值
    dc: int
    bonus_breakdown: dict[str, int] = field(default_factory=dict)
    #: 传奇抗性这类"失败改成功"：显式覆盖结论，**不伪造骰面**
    forced: bool | None = None

    @property
    def total(self) -> int:
        return self.a.total + self.bonus

    @property
    def success(self) -> bool:
        """天然 20 必成功、天然 1 必失败 —— **只对攻击检定成立**。

        D&D 2024：属性/技能检定和豁免只看总点数；攻击骰才吃天然 20/1。
        死亡豁免是独立的一套（`death_save`），不经过这里。
        """
        if self.forced is not None:
            return bool(self.forced)
        if self.kind == "attack":
            if self.a.is_crit:
                return True
            if self.a.is_crit_fail:
                return False
        return self.total >= self.dc

    @property
    def margin(self) -> int:
        return self.total - self.dc

    def summary(self) -> str:
        bits = " + ".join(f"{k} {v:+d}" for k, v in self.bonus_breakdown.items() if v)
        head = f"{self.label}：{self.a.summary()}"
        if bits:
            head += f"；加值 {bits} = {self.bonus:+d}"
        verdict = "成功" if self.success else "失败"
        return f"{head} ⇒ 总计 {self.total} vs DC/AC {self.dc} → **{verdict}**（差 {self.margin:+d}）"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "label": self.label, "roll": self.a.to_dict(),
            "bonus": self.bonus, "dc": self.dc, "total": self.total,
            "success": self.success, "margin": self.margin,
            "summary": self.summary(),
        }
