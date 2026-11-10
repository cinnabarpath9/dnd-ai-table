# coding=utf-8
"""法术 → 引擎能结算的 `Spell`。

SRD 的法术数据是**半structured**的，实测覆盖率（339 条）：

    target_count          100%      damage_roll           35%
    saving_throw_ability   38%      attack_roll           12%
    concentration          39%      shape_type            15%
    casting_options        36%      higher_level          36%

**没覆盖的部分（治疗、召唤、变形、控制…）只能靠 `desc` 文本**。
所以按 P1 的同一纪律：**抽得到就结构化，抽不到就标记"引擎不自动结算"**，
交给 DM 裁定，**绝不猜**。

⚠️ 一条实测发现的坑：SRD 把"只有攻击加值、没有伤害骰"的法术
（如 `Guiding Bolt` 之外的一批）写成 `attack_roll: true` 但没有 `damage_roll`，
这时不能当成"0 伤害"——要标 `structured=False`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..srd.store import NotFound, get_store

__all__ = ["Spell", "SpellParseError", "parse_spell", "get_spell", "list_spells",
           "SpellSchool", "SCHOOL_CN"]


class SpellParseError(KeyError):
    """法术数据缺关键字段时抛这个。"""


SCHOOL_CN = {
    "abjuration": "防护", "conjuration": "咒法", "divination": "预言",
    "enchantment": "附魔", "evocation": "塑能", "illusion": "幻术",
    "necromancy": "死灵", "transmutation": "变化",
}
SpellSchool = str

#: 施法时间 → 该法术占哪个动作槽
#: ⚠️ SRD 里写的是 `bonus-action`（带连字符），不是 `bonus action` ——
#:    漏了这条会让所有附赠动作法术被当成普通动作（回归 A23 钉住）。
_CAST_SLOT = {
    "action": "action",
    "bonus action": "bonus_action",
    "bonus-action": "bonus_action",
    "bonusaction": "bonus_action",
    "reaction": "reaction",
    "1 minute": "action", "10 minutes": "action", "1 hour": "action",
    "8 hours": "action", "12 hours": "action", "24 hours": "action",
}

_RE_DICE = re.compile(r"(\d+d\d+(?:\s*[+-]\s*\d+)?)")
_RE_UPCAST_DICE = re.compile(r"increase[sd]?\s+by\s+(\d+d\d+)", re.I)
#: 治疗：`regains ... 2d8 plus your spellcasting ability modifier`
#: ⚠️ 用**先行断言**收尾 —— 写成 `((?:...)?)` 时正则会在 `2d8` 处就满足并收尾，
#:    "plus your spellcasting ability modifier" 永远匹配不到（回归 A19 钉住）。
_RE_HEAL = re.compile(
    r"(?:regains?\s+(?:a\s+number\s+of\s+)?(?:Temporary\s+)?Hit\s+Points\s+"
    r"(?:equal\s+to\s+)?(\d+d\d+(?:\s*[+-]\s*\d+)?)"
    r"|regains?\s+(\d+d\d+(?:\s*[+-]\s*\d+)?)\s+Hit\s+Points)",
    re.I)
#: 治疗骰后面是不是跟着"加施法属性调整值"
_RE_HEAL_ADDS_MOD = re.compile(
    r"(\d+d\d+(?:\s*[+-]\s*\d+)?)\s*\+?\s*(?:plus\s+)?your\s+spellcasting\s+ability\s+modifier",
    re.I)
_RE_TEMP_HP = re.compile(
    r"(\d+d\d+(?:\s*[+-]\s*\d+)?)\s+Temporary\s+Hit\s+Points", re.I)
#: 描述里出现这些词，才相信抽到的骰子是**伤害**
_RE_DAMAGE_WORD = re.compile(r"\bdamage\b", re.I)

#: 英文数词 → 数字（多投射物用）
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
#: “三发飞弹 / 三道射线”：只在描述前半段找，避开“高等级时变两道”的说明
_RE_PROJECTILES = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:[a-z-]+\s+){0,3}(darts?|rays?|beams?|bolts?)\b", re.I)
#: 升环多一发：`one more dart` / `one additional ray`
_RE_UPCAST_PROJECTILE = re.compile(
    r"\b(one|two|three)\s+(?:more|additional)\s+(dart|ray|beam|bolt)s?\b", re.I)
#: 描述里“高等级时……”的分界，投射物数量只看它之前
_RE_HIGHER_LEVEL_TEXT = re.compile(
    r"(?:at\s+(?:5th|11th|17th)\s+level|higher\s+levels?)", re.I)


@dataclass
class Spell:
    """一个法术的机械定义。"""
    key: str
    name: str
    level: int                       # 0 = 戏法
    school: str = ""
    casting_time: str = ""
    slot: str = "action"             # 占用哪个动作槽
    range_ft: int = 0                # 0 = 自身/接触（见 range_text）
    range_text: str = ""
    duration: str = "instantaneous"
    concentration: bool = False
    ritual: bool = False

    # --- 判定形态（三选一或都空） ---
    attack_roll: bool = False        # 法术攻击检定
    save_ability: str = ""           # 目标做这个豁免
    save_success_half: bool = False  # 豁免成功半伤

    # --- 效果 ---
    damage_roll: str = ""
    damage_type: str = ""
    heal_roll: str = ""              # 治疗（从 desc 抽，可能抽不到）
    heal_adds_modifier: bool = False
    temp_hp_roll: str = ""

    # --- 范围 ---
    shape_type: str = ""             # sphere / cone / cube / line / emanation
    shape_size: int = 0
    target_count: int = 1
    #: 一次施放发出几个独立投射物（魔法飞弹 3 发、灼热射线 3 道）
    projectiles: int = 1
    #: 每升一环多几个投射物（魔法飞弹/灼热射线 +1）
    upcast_projectiles: int = 0

    # --- 升环 ---
    upcast_damage: str = ""          # 每升一环多掷多少
    upcast_desc: str = ""
    #: 各环阶的具体数值（SRD 直接给了就用它，比正则算更准）
    slot_options: dict[int, dict] = field(default_factory=dict)
    #: 戏法的「角色等级」成长（SRD 的 `player_level_5/11/17`）：等级 → 数值
    level_options: dict[int, dict] = field(default_factory=dict)

    components: str = ""
    material_cost: str = ""
    material_consumed: bool = False
    desc: str = ""

    #: 结构化解析是否可信。False = 引擎只把它当"文字建议"，交给 DM 裁定
    structured: bool = True
    #: 为什么不可信（排查/给玩家看）
    unstructured_reason: str = ""

    # ---------------- 派生 ----------------

    @property
    def is_cantrip(self) -> bool:
        return self.level == 0

    @property
    def is_aoe(self) -> bool:
        return bool(self.shape_size) and self.shape_type != ""

    @property
    def needs_targets(self) -> bool:
        """施放时是否必须指定目标。

        ⚠️ **`Touch` 也算需要目标** —— SRD 把触碰法术的 `range` 写成 0，
        旧实现按 `range_ft == 0` 判成"自身法术"，结果治疗术只能治自己。
        判据看 `range_text`，不要只看数字（回归 A30 钉住）。
        """
        rt = (self.range_text or "").strip().lower()
        if rt in ("self", "自身"):
            return False
        if rt.startswith("touch"):
            return True
        return self.range_ft > 0 or rt in ("special", "sight", "unlimited")

    @property
    def negates_on_success(self) -> bool:
        """豁免成功是否完全无效（有伤害但非半伤，或有控制但无伤害）。"""
        return bool(self.save_ability) and not self.save_success_half

    @property
    def is_melee_attack(self) -> bool:
        """法术攻击是近战还是远程：Touch / 5 尺算近战（影响劣势/触及规则）。"""
        if not self.attack_roll:
            return False
        rt = (self.range_text or "").strip().lower()
        return rt in ("touch", "接触") or (0 < int(self.range_ft or 0) <= 5)

    def _keep_flat(self, expr: str) -> str:
        """升环表达式常常只写骰子段（`13d6`），基础式的固定加值（`+40`）要保住。"""
        m = re.search(r"([+-])\s*(\d+)\s*$", self.damage_roll or "")
        if m and not re.search(r"[+-]\s*\d+\s*$", expr):
            return f"{expr}{m.group(1)}{m.group(2)}"
        return expr

    def damage_at(self, slot_level: int, character_level: int = 0) -> str:
        """给定环阶的实际伤害骰（**单发**；多投射物由 projectiles_at 乘）。

        优先用 SRD 给的 `casting_options`（最准），其次按 `upcast_damage` 推算。
        戏法另看 `player_level_*`（5/11/17 级成长）；`character_level` 不传时按 0 环。
        """
        lv = int(slot_level)
        if self.is_cantrip and character_level and self.level_options:
            keys = [k for k in self.level_options if k <= int(character_level)]
            if keys:
                opt = self.level_options[max(keys)]
                if opt.get("damage_roll"):
                    return self._keep_flat(opt["damage_roll"])
        opt = self.slot_options.get(lv)
        if opt and opt.get("damage_roll"):
            return self._keep_flat(opt["damage_roll"])
        if not self.upcast_damage or lv <= self.level:
            return self.damage_roll
        m = re.match(r"(\d+)d(\d+)", self.upcast_damage)
        if not m:
            return self.damage_roll
        step_cnt, faces = int(m.group(1)), int(m.group(2))
        base = re.match(r"(\d+)d(\d+)", self.damage_roll or "0d6")
        if not base:
            return self.damage_roll
        cnt = int(base.group(1)) + step_cnt * (lv - self.level)
        return f"{cnt}d{faces}"

    def projectiles_at(self, slot_level: int) -> int:
        """这一次施放实际有几个独立投射物。

        ⚠️ SRD 的 `damage_roll` 是**单发**数值（魔法飞弹 1d4+1、灼热射线 2d6），
        描述里的“三发”不在数值字段里；不乘数量就会少算三分之二伤害。
        """
        lv = int(slot_level)
        extra = max(0, lv - self.level) * max(0, self.upcast_projectiles)
        return max(1, int(self.projectiles) + extra)

    def heal_at(self, slot_level: int) -> str:
        lv = int(slot_level)
        opt = self.slot_options.get(lv)
        if opt and opt.get("heal_roll"):
            return opt["heal_roll"]
        if not self.heal_roll or lv <= self.level:
            return self.heal_roll
        base = re.match(r"(\d+)d(\d+)", self.heal_roll)
        if not base:
            return self.heal_roll
        cnt, faces = int(base.group(1)), int(base.group(2))
        return f"{cnt + (lv - self.level)}d{faces}"

    # ---------------- 渲染 ----------------

    def components_text(self) -> str:
        bits = []
        if "V" in (self.components or "") or self.desc.count("Verbal"):
            bits.append("V")
        return self.components or "—"

    def menu_line(self, slot_level: int | None = None,
                  character_level: int = 0) -> str:
        """菜单里的一行（给模型看）。"""
        lv = self.level if slot_level is None else slot_level
        tag = "戏法" if self.is_cantrip else f"{lv} 环"
        slot_cn = {"action": "动作", "bonus_action": "附赠动作",
                   "reaction": "反应", "free": "自由"}.get(self.slot, self.slot)
        kind = []
        if self.attack_roll:
            kind.append("法术攻击检定")
        if self.save_ability:
            from ..rules.constants import ABILITY_CN
            kind.append(f"{ABILITY_CN.get(self.save_ability, self.save_ability)}豁免"
                        f"（成功{'半伤' if self.save_success_half else '无效'}）")
        if self.heal_roll:
            kind.append(f"治疗 {self.heal_at(lv)}")
        if self.damage_roll:
            kind.append(f"{self.damage_at(lv, character_level)} {self.damage_type}")
        if self.is_aoe:
            kind.append(f"{self.shape_size} 尺{self.shape_type}")
        if self.concentration:
            kind.append("需专注")
        rng = f"{self.range_ft} 尺" if self.range_ft else self.range_text or "自身"
        s = f"{self.name}（{tag}，{slot_cn}）｜{rng}｜" + "，".join(kind) if kind \
            else f"{self.name}（{tag}，{slot_cn}）｜{rng}"
        if not self.structured:
            s += "　⚠️引擎不自动结算，需 DM 裁定"
        return s


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

def _norm_ability(s: str) -> str:
    s = (s or "").strip().lower()
    if s.endswith("saving throw"):
        s = s[: -len("saving throw")].strip()
    return s


def parse_spell(rec: dict) -> Spell:
    """把一个 SRD 法术记录解析成 `Spell`。"""
    if not rec.get("name"):
        raise SpellParseError("法术记录没有 name")

    desc = rec.get("desc") or ""
    school = rec.get("school")
    school_key = (school or {}).get("key") if isinstance(school, dict) else str(school or "")

    dmg_types = rec.get("damage_types") or []
    dmg_type = ""
    if dmg_types:
        t = dmg_types[0]
        dmg_type = (t.get("key") if isinstance(t, dict) else str(t)).lower()

    casting_time = str(rec.get("casting_time") or "action").lower().strip()
    slot = _CAST_SLOT.get(casting_time, "action")

    range_ft = rec.get("range")
    try:
        range_ft = int(range_ft) if range_ft is not None else 0
    except (TypeError, ValueError):
        range_ft = 0

    sp = Spell(
        key=rec.get("key") or "",
        name=rec["name"],
        level=int(rec.get("level") or 0),
        school=school_key,
        casting_time=casting_time,
        slot=slot,
        range_ft=range_ft,
        range_text=str(rec.get("range_text") or ""),
        duration=str(rec.get("duration") or "instantaneous"),
        concentration=bool(rec.get("concentration")),
        ritual=bool(rec.get("ritual")),
        attack_roll=bool(rec.get("attack_roll")),
        save_ability=_norm_ability(rec.get("saving_throw_ability") or ""),
        damage_roll=(rec.get("damage_roll") or "").replace(" ", ""),
        damage_type=dmg_type,
        shape_type=str(rec.get("shape_type") or ""),
        shape_size=int(rec.get("shape_size") or 0),
        target_count=int(rec.get("target_count") or 1),
        upcast_desc=str(rec.get("higher_level") or ""),
        desc=desc,
    )

    # 升环：SRD 直接给 casting_options 时用它（比正则准）
    for opt in (rec.get("casting_options") or []):
        typ = str(opt.get("type") or "")
        m_lv = re.match(r"slot_level_(\d+)", typ)
        if m_lv:
            lv = int(m_lv.group(1))
            sp.slot_options[lv] = {
                "damage_roll": (opt.get("damage_roll") or "").replace(" ", ""),
                "heal_roll": (opt.get("heal_roll") or "").replace(" ", ""),
            }
            continue
        m_pc = re.match(r"player_level_(\d+)", typ)
        if m_pc:
            sp.level_options[int(m_pc.group(1))] = {
                "damage_roll": (opt.get("damage_roll") or "").replace(" ", ""),
                "heal_roll": (opt.get("heal_roll") or "").replace(" ", ""),
            }

    # 多投射物：只数“高等级时……”之前的那句，避开 Eldritch Blast 的“两道/三道”
    head = _RE_HIGHER_LEVEL_TEXT.split(desc or "", maxsplit=1)[0]
    m_proj = _RE_PROJECTILES.search(head)
    if m_proj and sp.damage_roll:
        sp.projectiles = _WORD_NUM.get(m_proj.group(1).lower(), 1)
    m_up_proj = _RE_UPCAST_PROJECTILE.search(
        " ".join(filter(None, [sp.upcast_desc, desc or ""])))
    if m_up_proj:
        sp.upcast_projectiles = _WORD_NUM.get(m_up_proj.group(1).lower(), 1)

    m_up = _RE_UPCAST_DICE.search(sp.upcast_desc)
    if m_up:
        sp.upcast_damage = m_up.group(1).replace(" ", "")

    # 豁免成功半伤：从 desc 找
    if sp.save_ability:
        sp.save_success_half = bool(
            re.search(r"Success:\s*Half\s+damage", desc, re.I)
            or re.search(r"half\s+as\s+much\s+damage", desc, re.I)
            or sp.damage_roll and re.search(r"half\s+damage", desc, re.I))

    # 治疗 / 临时生命（只能从文本抽）
    m_heal = _RE_HEAL.search(desc)
    if m_heal:
        expr = m_heal.group(1) or m_heal.group(2) or ""
        sp.heal_roll = expr.replace(" ", "")
        sp.heal_adds_modifier = bool(_RE_HEAL_ADDS_MOD.search(desc))
    m_temp = _RE_TEMP_HP.search(desc)
    if m_temp:
        sp.temp_hp_roll = m_temp.group(1).replace(" ", "")

    # ⚠️ SRD 的 `casting_options` 把**治疗量**也塞在 `damage_roll` 字段里
    #    （实测 Cure Wounds：slot_level_2 → damage_roll "4d8"）。
    #    对治疗法术那就是治疗骰，搬到 `heal_roll`；否则 `heal_at()` 会走
    #    "每升一环 +1 颗" 的兜底算法，把 4d8 算成 3d8（少一颗，实测踩过）。
    if sp.heal_roll:
        for _lv, _opt in sp.slot_options.items():
            if not _opt.get("heal_roll") and _opt.get("damage_roll"):
                _opt["heal_roll"] = _opt["damage_roll"]

    # ⚠️ 关键：治疗型法术**不许**再有伤害骰。
    #    SRD 里 `Cure Wounds` 的 desc 写着 "regains ... 2d8"，
    #    如果让下面的"兜底抽骰"跑，会把 2d8 当成伤害 → **治疗术会去打人**。
    #    （这是真出过的 bug：实测时受治疗的角色从 40 掉到 29。）
    if sp.heal_roll or sp.temp_hp_roll:
        sp.damage_roll = ""

    # ⚠️ 骰子只有在文本明确说 "damage" 时才是伤害骰。
    #    Guidance / False Life / Prayer / Regenerate 的描述里也有 d4/d8，
    #    以前的兜底抽骰会把它们当"自动命中伤害"（神导术打人 1d4）。
    if sp.damage_roll and not _RE_DAMAGE_WORD.search(desc or ""):
        sp.damage_roll = ""

    # ---- 结构化可信度判定 ----
    reasons = []
    if not sp.attack_roll and not sp.save_ability and not sp.heal_roll \
            and not sp.temp_hp_roll and not sp.damage_roll:
        reasons.append("没有可判定的机械字段（纯叙事/控制/召唤）")
    if sp.attack_roll and not sp.damage_roll and not sp.heal_roll:
        reasons.append("有攻击检定但没有伤害骰 —— 不当作 0 伤害")
    if sp.save_ability and not sp.damage_roll and not sp.heal_roll:
        # 控制型法术：豁免本身是机械的，但"被震慑几轮"这种效果在文本里
        reasons.append("豁免型控制法术：引擎只结算豁免，附加效果需 DM 裁定")
    if reasons:
        sp.structured = False
        sp.unstructured_reason = "；".join(reasons)

    sp.components = _components(rec)
    sp.material_cost = str(rec.get("material_cost") or "")
    sp.material_consumed = bool(rec.get("material_consumed"))
    return sp


def _components(rec: dict) -> str:
    bits = []
    if rec.get("verbal"):
        bits.append("V")
    if rec.get("somatic"):
        bits.append("S")
    if rec.get("material_specified") or rec.get("material"):
        bits.append("M")
    return "、".join(bits)


# --------------------------------------------------------------------------- #
# 查表
# --------------------------------------------------------------------------- #

def get_spell(name_or_key: str) -> Spell:
    """按名字或 key 取一个已解析的法术。"""
    store = get_store()
    try:
        rec = store.by_key("spells", name_or_key)
    except NotFound:
        rec = store.by_name("spells", name_or_key)
    return parse_spell(rec)


def list_spells(*, level: int | None = None, klass: str | None = None,
                only_structured: bool = False) -> list[Spell]:
    """列出法术（可按环阶/职业过滤）。

    `klass` 用法术记录里的 `classes` 字段（形如 `[{"name":"Wizard"}]`）。
    """
    store = get_store()
    out: list[Spell] = []
    for rec in store.records("spells"):
        if level is not None and int(rec.get("level") or 0) != int(level):
            continue
        if klass:
            names = [str((c or {}).get("name", "")).lower()
                     for c in (rec.get("classes") or [])]
            if klass.lower() not in names:
                continue
        sp = parse_spell(rec)
        if only_structured and not sp.structured:
            continue
        out.append(sp)
    out.sort(key=lambda s: (s.level, s.name))
    return out
