# coding=utf-8
"""U14 角色创建 / 升级向导：**数值由 SRD + 规则算，模型不参与**。

口径（先说清楚哪些是"照 SRD"、哪些是"这一版的简化"）：

1. **职业/种族必须能在本地 SRD 里查到**；查到的是基础职业（不是子职），
   生命骰、豁免熟练、施法类型都从 SRD 记录里读，不手搓；
2. **属性**要显式给（六项齐全、1..30）；向导可以掷 4d6 取高 3（可复现）
   或用标准数组，但**不替玩家选**；
3. **HP**：1 级取生命骰满值 + 体质调整；之后每级 +(骰面/2+1+体质)；
   **生命骰**跟着等级走；
4. **法术位**：v1 只自动配**全施法者**（吟游/牧师/德鲁伊/术士/法师）1~5 级的表；
   半施法（圣武士/游侠）与契约法术（邪术师）**不自动配**，明说"需 DM 处理"；
5. **法术**必须在该职业的法术表里（按 SRD spell 的 `classes` 字段查），
   等级不超过最高环阶；查不到/不在表里 → 报错，不静默跳过；
6. **技能熟练**：2024 口径大多职业选 2 项（吟游 3、游荡者 4、游侠 3）；
   v1 **不校验"该职业可选哪几项"**（SRD 没有结构化列表），只校验数量与技能名；
7. **升级**：v1 只支持单职业、1~5 级；升级时 HP/生命骰/法术位按表涨，
   法术位**恢复到满**（视作升级后长休）；属性提升要显式给。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .dice import roll
from .srd.store import NotFound, get_store
from .state import StateTree
from .state.patch import escape_token
from .state.tree import new_character
from .rules.constants import (
    ABILITY_CN, SKILL_CN, SKILLS, ability_modifier, proficiency_bonus,
)

__all__ = [
    "CreationError", "BASE_CLASSES", "CLASS_CN", "SKILL_CHOICES",
    "standard_array", "roll_abilities", "suggest_abilities",
    "list_options", "class_info",
    "species_info", "spells_for", "create_character", "level_up",
]

#: 标准数组的**推荐分配顺序**（v1 的向导便利，不是规则要求）。
#: 顺序按 2024 各职业主属性排：主属性拿 15，其次 14/13…
_CLASS_PRIORITY = {
    "Barbarian": ["力量", "体质", "敏捷", "感知", "魅力", "智力"],
    "Bard": ["魅力", "敏捷", "体质", "智力", "感知", "力量"],
    "Cleric": ["感知", "体质", "力量", "敏捷", "魅力", "智力"],
    "Druid": ["感知", "体质", "敏捷", "智力", "魅力", "力量"],
    "Fighter": ["力量", "体质", "敏捷", "感知", "魅力", "智力"],
    "Monk": ["敏捷", "感知", "体质", "力量", "魅力", "智力"],
    "Paladin": ["力量", "魅力", "体质", "感知", "敏捷", "智力"],
    "Ranger": ["敏捷", "感知", "体质", "力量", "魅力", "智力"],
    "Rogue": ["敏捷", "体质", "感知", "智力", "魅力", "力量"],
    "Sorcerer": ["魅力", "体质", "敏捷", "感知", "智力", "力量"],
    "Warlock": ["魅力", "体质", "敏捷", "感知", "智力", "力量"],
    "Wizard": ["智力", "体质", "敏捷", "感知", "魅力", "力量"],
}

_MAX_LEVEL = 5

CLASS_CN = {
    "Barbarian": "野蛮人", "Bard": "吟游诗人", "Cleric": "牧师",
    "Druid": "德鲁伊", "Fighter": "战士", "Monk": "武僧",
    "Paladin": "圣武士", "Ranger": "游侠", "Rogue": "游荡者",
    "Sorcerer": "术士", "Warlock": "邪术师", "Wizard": "法师",
}
#: 12 个基础职业（子职不在建卡入口里）
BASE_CLASSES = tuple(CLASS_CN)

SPECIES_CN = {
    "Dragonborn": "龙裔", "Dwarf": "矮人", "Elf": "精灵", "Gnome": "侏儒",
    "Goliath": "歌利亚", "Halfling": "半身人", "Human": "人类",
    "Orc": "兽人", "Tiefling": "提夫林",
}

#: 2024 每个职业能选的技能数量（吟游 3 / 游荡者 4 / 游侠 3，其余 2）
SKILL_CHOICES = {"Bard": 3, "Rogue": 4, "Ranger": 3}

#: 全施法者 1~5 级的法术位（SRD 2024 法术位表）
_FULL_SLOTS: dict[int, dict[int, int]] = {
    1: {1: 2},
    2: {1: 3},
    3: {1: 4, 2: 2},
    4: {1: 4, 2: 3},
    5: {1: 4, 2: 3, 3: 2},
}

_FULL_CASTERS = ("Bard", "Cleric", "Druid", "Sorcerer", "Wizard")
_HALF_CASTERS = ("Paladin", "Ranger")

_EN_TO_CN = {k: v for k, v in ABILITY_CN.items()}


class CreationError(ValueError):
    """建卡/升级不成立（职业查不到、技能数量不对、法术不在表里……）。"""


# --------------------------------------------------------------------------- #
# 掷属性 / 选项清单
# --------------------------------------------------------------------------- #

def standard_array() -> list[int]:
    """标准数组（15/14/13/12/10/8）——**只给数字，不替你分配**。"""
    return [15, 14, 13, 12, 10, 8]


def roll_abilities(rng) -> dict[str, int]:  # noqa: ANN001
    """掷六组 4d6 取高 3（可复现：同一个 rng 次序给同一组数）。"""
    out = {}
    for key in ("力量", "敏捷", "体质", "智力", "感知", "魅力"):
        out[key] = int(roll("4d6kh3", rng=rng).total)
    return out


def suggest_abilities(klass: str) -> dict[str, int]:
    """把标准数组按职业推荐顺序分配（15 给主属性）。

    ⚠️ 这是**向导的便利**，不是规则要求：SRD 只要求"把六个数分配下去"，
    怎么分是玩家的选择。网页"推荐"按钮用它，玩家仍可改。
    """
    en = _resolve(klass, CLASS_CN)
    order = _CLASS_PRIORITY.get(en) or ["力量", "敏捷", "体质", "智力", "感知", "魅力"]
    base = standard_array()
    return {key: base[i] for i, key in enumerate(order)}


@lru_cache(maxsize=1)
def _store():                                        # noqa: ANN202
    return get_store()


def _resolve(name: str, mapping: dict[str, str]) -> str:
    """接受英文名或中文名，返回英文名；都不匹配就报错。"""
    name = (name or "").strip()
    if name in mapping:
        return name
    for en, cn in mapping.items():
        if cn == name:
            return en
    raise CreationError(f"不认识的名字：{name!r}（可用：{list(mapping.values())}）")


@lru_cache(maxsize=1)
def list_options() -> dict[str, Any]:
    """向导用的选项清单（职业 / 种族 / 技能）。"""
    return {
        "classes": [{"en": en, "cn": cn}
                    for en, cn in CLASS_CN.items()],
        "species": [{"en": en, "cn": cn}
                    for en, cn in SPECIES_CN.items()],
        "skills": [{"en": k, "cn": SKILL_CN.get(k, k)}
                   for k in sorted(SKILLS)],
    }


# --------------------------------------------------------------------------- #
# SRD 查询
# --------------------------------------------------------------------------- #

def class_info(klass: str) -> dict[str, Any]:
    """基础职业信息：生命骰 / 豁免 / 施法类型 / 技能可选数。"""
    en = _resolve(klass, CLASS_CN)
    try:
        rec = _store().by_name("classes", en)
    except NotFound as exc:
        raise CreationError(f"SRD 里查不到职业 {en!r}") from exc
    if rec.get("subclass_of"):
        raise CreationError(f"{en!r} 是子职业，不是基础职业（先选它的主职业）")
    hit = str(rec.get("hit_dice") or "").upper().lstrip("D")
    if not hit.isdigit():
        raise CreationError(f"职业 {en!r} 的生命骰抽不出来（{rec.get('hit_dice')!r}）")
    saves = []
    for s in rec.get("saving_throws") or []:
        nm = str((s or {}).get("name") or "")
        saves.append(_EN_TO_CN.get(nm.lower(), nm))
    return {"en": en, "cn": CLASS_CN[en], "hit_die": int(hit),
            "saves": saves,
            "caster": str(rec.get("caster_type") or "NONE"),
            "skill_choices": SKILL_CHOICES.get(en, 2)}


def species_info(species: str) -> dict[str, Any]:
    """基础种族信息：速度 / 体型 / 特性原文（特性只作说明，不自动结算）。"""
    en = _resolve(species, SPECIES_CN)
    try:
        rec = _store().by_name("species", en)
    except NotFound as exc:
        raise CreationError(f"SRD 里查不到种族 {en!r}") from exc
    if rec.get("is_subspecies"):
        raise CreationError(f"{en!r} 是亚种，不是基础种族")
    speed, size, traits = 30, "", []
    for tr in rec.get("traits") or []:
        name = str(tr.get("name") or "")
        desc = str(tr.get("desc") or "")
        if name.lower() == "speed":
            digits = "".join(ch for ch in desc.split("feet")[0] if ch.isdigit())
            if digits:
                speed = int(digits)
        elif name.lower() == "size":
            size = desc
        if name:
            traits.append({"名": name, "来源": en, "描述": desc})
    return {"en": en, "cn": SPECIES_CN[en], "speed": speed,
            "size": size, "traits": traits}


def spells_for(klass: str, *, max_level: int = 1) -> list[dict[str, Any]]:
    """该职业可选的法术（按 SRD `classes` 字段查，含戏法 level 0）。"""
    info = class_info(klass)
    out = []
    for rec in _store().records("spells"):
        classes = {(c or {}).get("name") for c in (rec.get("classes") or [])}
        if info["en"] not in classes:
            continue
        lv = int(rec.get("level") or 0)
        if lv > int(max_level):
            continue
        out.append({"name": rec.get("name"), "level": lv,
                    "school": (rec.get("school") or {}).get("name", ""),
                    "desc": str(rec.get("desc") or "")})
    out.sort(key=lambda x: (x["level"], x["name"] or ""))
    return out


# --------------------------------------------------------------------------- #
# 建卡
# --------------------------------------------------------------------------- #

def _norm_abilities(abilities: dict | None) -> dict[str, int]:
    if not isinstance(abilities, dict):
        raise CreationError("要建卡就得有六项属性（用 roll_abilities 或手填）")
    out: dict[str, int] = {}
    for key, cn in ABILITY_CN.items():
        if cn in abilities:
            val = abilities[cn]
        elif key in abilities:
            val = abilities[key]
        else:
            raise CreationError(f"属性缺「{cn}」")
        if isinstance(val, bool) or not isinstance(val, int):
            raise CreationError(f"「{cn}」必须是整数（拿到 {val!r}）")
        if not 1 <= int(val) <= 30:
            raise CreationError(f"「{cn}」超出 1..30（拿到 {val}）")
        out[cn] = int(val)
    return out


def _norm_skills(skills) -> list[str]:                   # noqa: ANN001
    out = []
    for s in skills or ():
        key = str(s).strip()
        if not key:
            continue
        low = key.lower()
        found = None
        for en, cn in SKILL_CN.items():
            if low == en or key == cn:
                found = en
                break
        if found is None:
            raise CreationError(f"不认识的技能：{key!r}（用 list_options()['skills']）")
        if found not in out:
            out.append(found)
    return out


def _spell_entry(klass: str, spell_name: str, *, allowed: set[str],
                 top_slot: int) -> tuple[str, dict]:
    try:
        rec = _store().by_name("spells", spell_name)
    except NotFound as exc:
        raise CreationError(f"SRD 里查不到法术 {spell_name!r}") from exc
    en = str(rec.get("name") or "")
    if en not in allowed:
        raise CreationError(f"「{en}」不在 {CLASS_CN.get(klass, klass)} 的法术表里")
    lv = int(rec.get("level") or 0)
    if lv > top_slot:
        raise CreationError(f"「{en}」是 {lv} 环，超出当前最高环阶 {top_slot}")
    school = (rec.get("school") or {}).get("name", "")
    parts = []
    if rec.get("verbal"):
        parts.append("言语")
    if rec.get("somatic"):
        parts.append("姿势")
    if rec.get("material"):
        parts.append("材料")
    return en, {"等级": lv, "准备中": True, "学派": school,
                "成分": "、".join(parts), "描述": str(rec.get("desc") or "")}


def create_character(name: str, *, klass: str, species: str, level: int = 1,
                     abilities: dict | None = None, skills=(),  # noqa: ANN001
                     spells=(), ac: int | None = None) -> dict:  # noqa: ANN001
    """造一个角色的 `stat_data` 片段。**不写进状态树**，由调用方 insert。"""
    name = (name or "").strip()
    if not name:
        raise CreationError("角色名不能为空")
    lv = int(level)
    if not 1 <= lv <= _MAX_LEVEL:
        raise CreationError(f"v1 只支持 1~{_MAX_LEVEL} 级（拿到 {lv}）")
    cls = class_info(klass)
    sp = species_info(species)
    ab = _norm_abilities(abilities)
    picked = _norm_skills(skills)
    if len(picked) != cls["skill_choices"]:
        raise CreationError(
            f"{cls['cn']} 要选 {cls['skill_choices']} 项技能熟练"
            f"（拿到 {len(picked)} 项：{picked}）")

    con = ability_modifier(ab["体质"])
    dex = ability_modifier(ab["敏捷"])
    per_level = max(1, cls["hit_die"] // 2 + 1 + con)
    hp_max = max(1, cls["hit_die"] + con + (lv - 1) * per_level)

    prof_skills = {en: 1 for en in picked}
    char = new_character(
        name, level=lv, job=cls["cn"], abilities=ab, hp_max=hp_max,
        ac=int(ac) if ac is not None else 10 + dex, speed=sp["speed"],
        熟练配置={"技能": prof_skills, "豁免": list(cls["saves"]),
              "武器": [], "护甲": [], "工具": []},
        特性={sp["en"]: {t["名"]: {"描述": t["描述"], "来源": t["来源"]}
                    for t in sp["traits"]}},
        钱币={"金币": 0, "银币": 0, "铜币": 0},
    )
    char["生命值"]["生命骰"] = {"当前": lv, "总数": lv,
                             "面数": f"d{cls['hit_die']}"}

    slots = _FULL_SLOTS.get(lv, {}) if cls["en"] in _FULL_CASTERS else {}
    top_slot = max(slots) if slots else 0
    book: dict[str, dict] = {}
    if spells:
        if cls["en"] in _HALF_CASTERS or cls["en"] == "Warlock":
            raise CreationError(
                f"{cls['cn']} 的法术位 v1 不自动配（半施法/契约法术），"
                f"请让 DM 手填，或先选全施法职业")
        if not cls["caster"] == "FULL" and cls["en"] not in _FULL_CASTERS:
            raise CreationError(f"{cls['cn']} 不是施法职业，不能选法术")
        allowed = {s["name"] for s in spells_for(cls["en"], max_level=top_slot)}
        for sname in spells:
            en, entry = _spell_entry(cls["en"], str(sname), allowed=allowed,
                                     top_slot=top_slot)
            book[en] = entry
    char["施法"] = {
        "法术位": {str(k): {"当前": v, "最大": v} for k, v in slots.items()},
        "法术书": book,
    }
    return char


# --------------------------------------------------------------------------- #
# 升级
# --------------------------------------------------------------------------- #

def _cp(name: str) -> str:
    return f"/角色列表/{escape_token(name)}"


def _single_class(char: dict) -> tuple[str, int]:
    jobs = char.get("职业") or {}
    if not isinstance(jobs, dict) or len(jobs) != 1:
        raise CreationError(f"v1 只支持单职业（现在是 {jobs!r}）")
    cn, lv = next(iter(jobs.items()))
    return str(cn), int(lv)


def level_up(state: StateTree, name: str, *, abilities: dict | None = None,
             spells=(), actor: str = "玩家",  # noqa: ANN001
             reason: str = "升级") -> dict:
    """升一级：HP / 生命骰 / 法术位按表涨；返回升级摘要。"""
    char = state.read(_cp(name))
    if not isinstance(char, dict):
        raise CreationError(f"角色 {name!r} 不存在")
    cn, lv = _single_class(char)
    if lv >= _MAX_LEVEL:
        raise CreationError(f"v1 最高升到 {_MAX_LEVEL} 级（现在 {lv} 级）")
    en = _resolve(cn, CLASS_CN)
    cls = class_info(en)
    new_lv = lv + 1
    ab = dict(char.get("属性") or {})
    if abilities:
        merged = {cn: ab.get(cn, 10) for cn in ABILITY_CN.values()}
        merged.update(abilities)
        ab.update(_norm_abilities(merged))
    con = ability_modifier(ab.get("体质", 10))
    gain = max(1, cls["hit_die"] // 2 + 1 + con)
    hp = char.get("生命值") or {}
    hp_max = int(hp.get("最大") or 0) + gain
    hp_cur = min(hp_max, int(hp.get("当前") or 0) + gain)
    dice = hp.get("生命骰") or {}
    slots = _FULL_SLOTS.get(new_lv, {}) if en in _FULL_CASTERS else {}
    book = dict((char.get("施法") or {}).get("法术书") or {})
    if spells:
        top = max(slots) if slots else 0
        allowed = {s["name"] for s in spells_for(en, max_level=top)}
        for sname in spells:
            key, entry = _spell_entry(en, str(sname), allowed=allowed,
                                      top_slot=top)
            book[key] = entry
    patches = [
        {"op": "replace", "path": f"{_cp(name)}/职业/{escape_token(cn)}",
         "value": new_lv},
        {"op": "replace", "path": f"{_cp(name)}/生命值/最大", "value": hp_max},
        {"op": "replace", "path": f"{_cp(name)}/生命值/当前", "value": hp_cur},
        {"op": "replace", "path": f"{_cp(name)}/生命值/生命骰/当前",
         "value": int(dice.get("当前") or 0) + 1},
        {"op": "replace", "path": f"{_cp(name)}/生命值/生命骰/总数",
         "value": int(dice.get("总数") or 0) + 1},
        {"op": "replace", "path": f"{_cp(name)}/属性", "value": ab},
        {"op": "replace", "path": f"{_cp(name)}/施法",
         "value": {"法术位": {str(k): {"当前": v, "最大": v}
                          for k, v in slots.items()},
                   "法术书": book}},
        {"op": "replace", "path": f"{_cp(name)}/熟练配置/豁免",
         "value": list(cls["saves"])},
    ]
    state.commit(patches, actor=actor,
                 reason=f"{reason}：{cn} {lv} → {new_lv}"
                        f"（HP +{gain}，熟练加值 +{proficiency_bonus(new_lv)}）")
    return {"level": new_lv, "hp_max": hp_max, "hp_gain": gain,
            "slots": slots, "spellbook": len(book)}