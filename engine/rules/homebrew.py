# coding=utf-8
"""DM 临场造的动作/技能（H1）—— 结构化字段变成引擎的 `Action`。

和 `_homebrew_weapon_action` 同一条纪律：

    · 只有能进加减乘除的字段才进引擎；`描述/机制/背景` 只做说明文字；
    · **抽不到/写坏不要静默按错规则打** —— 标 `structured=False` 并写清原因，
      菜单会标"需 DM 裁定"，引擎只把动作槽算掉；
    · 存储是**当局状态**：`角色列表.<角色>.动作.<名称>`，跟存档，
      新开一局是新的 `StateTree`，不跟过去，也不写世界书。

字段契约见 `docs/DM结构化设定协议.md` §3。这里的 `coerce_*` 是**写入口**
（网页/HTTP）用的严格校验；`build_*` 是**读取口**用的宽容转换 —— 手改的存档
不该让整桌崩掉，但也不能假装算对了。
"""

from __future__ import annotations

from ..dice import RollError, parse
from .constants import ABILITY_CN, ABILITIES, DAMAGE_TYPE_CN, ability_modifier, proficiency_bonus
from .unit import Action, Attack

__all__ = [
    "ACTIONS_KEY", "ACTION_TYPES", "ACTION_TYPE_CN", "ACTION_SLOTS",
    "DAMAGE_CN_TO_EN", "damage_key", "coerce_action_spec", "build_action",
    "describe_spec",
]

#: stat_data 里的表名：`角色列表.<角色>.动作.<名称>`
ACTIONS_KEY = "动作"

ACTION_TYPES = ("attack", "save", "heal", "utility")
ACTION_TYPE_CN = {"攻击": "attack", "豁免": "save", "治疗": "heal",
                  "描述": "utility", "功能": "utility",
                  "attack": "attack", "save": "save", "heal": "heal",
                  "utility": "utility"}
ACTION_TYPE_LABEL = {"attack": "攻击型", "save": "豁免型",
                     "heal": "治疗型", "utility": "描述型"}

ACTION_SLOTS = ("action", "bonus_action")
_SLOT_ALIASES = {"动作": "action", "附赠动作": "bonus_action",
                 "附赠": "bonus_action", "action": "action",
                 "bonus_action": "bonus_action"}

DAMAGE_CN_TO_EN = {cn: en for en, cn in DAMAGE_TYPE_CN.items()}
#: 桌面口语别名（DM 写"斩击"也能认）
_DAMAGE_ALIASES = {"斩击": "slashing", "砍击": "slashing", "刺击": "piercing",
                   "打击": "bludgeoning", "火": "fire", "冰": "cold",
                   "电": "lightning", "酸": "acid", "毒": "poison",
                   "神圣": "radiant", "暗影": "necrotic"}

_SHAPES = ("cone", "sphere", "cube", "line", "emanation")
_SHAPE_ALIASES = {"锥形": "cone", "锥": "cone", "球形": "sphere",
                  "球": "sphere", "立方": "cube", "方形": "cube",
                  "线形": "line", "线": "line", "光环": "emanation",
                  "放射": "emanation"}

#: 豁免属性：英文键 / 中文 / 三字母缩写 都能认
_ABILITY_ALIASES = {**{cn: en for en, cn in ABILITY_CN.items()},
                    **{en: en for en in ABILITIES},
                    "str": "strength", "dex": "dexterity", "con": "constitution",
                    "int": "intelligence", "wis": "wisdom", "cha": "charisma"}

#: 写入口的范围（挡"明显写坏"，不是规则上下限）
_LIMITS = {"攻击加值": (-10, 40), "DC": (1, 40), "范围": (0, 600),
           "射程": (0, 600), "次数": (0, 20)}


#: 每种类型能用哪些字段 —— 填了别的类型专属字段就报错，**不静默丢掉**
_ALLOWED_FIELDS = {
    "attack": {"攻击加值", "伤害骰", "伤害类型", "射程", "触及", "次数",
               "动作槽", "描述"},
    "save": {"豁免", "DC", "半伤", "范围", "范围形状", "伤害骰", "伤害类型",
             "射程", "次数", "动作槽", "描述"},
    "heal": {"治疗骰", "射程", "次数", "动作槽", "描述"},
    "utility": {"次数", "动作槽", "描述"},
}
_KNOWN_FIELDS = set().union(*_ALLOWED_FIELDS.values())


def damage_key(raw) -> str:
    """伤害类型：中文/别名 → 引擎英文键；认不出就原样返回（并让调用方留痕）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low in DAMAGE_TYPE_CN:
        return low
    return DAMAGE_CN_TO_EN.get(text) or _DAMAGE_ALIASES.get(text) or low


def _as_int(field: str, val, *, low: int, high: int) -> int:
    try:
        num = int(str(val).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数（拿到 {val!r}）") from exc
    if not (low <= num <= high):
        raise ValueError(f"{field} 超出范围 {low}..{high}（拿到 {num}）")
    return num


def _as_bool(field: str, val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, str)):
        return str(val).strip().lower() in ("1", "true", "是", "yes", "on")
    raise ValueError(f"{field} 必须是开关值（拿到 {val!r}）")


def _dice_ok(text: str) -> bool:
    try:
        parse(text)
    except RollError:
        return False
    return True


def _slot_of(spec: dict) -> str:
    raw = str(spec.get("动作槽") or "action").strip()
    slot = _SLOT_ALIASES.get(raw, raw)
    if slot not in ACTION_SLOTS:
        raise ValueError(f"动作槽必须是 action 或 bonus_action（拿到 {raw!r}）")
    return slot


def coerce_action_spec(payload: dict) -> dict:
    """把网页/HTTP 传来的原始字段变成干净的当局存储值。

    严格模式：不合法就抛 `ValueError`（带人话），**不写半截**。
    只认协议里的字段；不认识的键直接丢掉，避免把随手备注写进存档。
    """
    if not isinstance(payload, dict):
        raise ValueError("技能必须是 JSON 对象")

    raw_type = str(payload.get("类型") or "").strip()
    typ = ACTION_TYPE_CN.get(raw_type, raw_type.lower())
    if typ not in ACTION_TYPES:
        raise ValueError(f"类型必须是 attack/save/heal/utility（拿到 {raw_type!r}）")

    spec: dict = {"类型": typ, "来源": "DM"}
    allowed = _ALLOWED_FIELDS[typ]
    for key in _KNOWN_FIELDS - allowed:
        if payload.get(key) not in (None, ""):
            raise ValueError(f"{ACTION_TYPE_LABEL[typ]}动作不需要「{key}」——"
                             f"要填的话请换对应类型，或清空该字段")
    slot = _slot_of(payload)
    if slot != "action":
        spec["动作槽"] = slot

    for field, rng in _LIMITS.items():
        if field not in payload or payload.get(field) in (None, ""):
            continue
        spec[field] = _as_int(field, payload[field], low=rng[0], high=rng[1])

    for field, key in (("伤害骰", "伤害骰"), ("治疗骰", "治疗骰")):
        if field not in payload or payload.get(field) in (None, ""):
            continue
        text = str(payload[field]).strip()
        if not _dice_ok(text):
            raise ValueError(f"{field} 看不懂：{text!r}（写成 2d6+4 这样）")
        spec[key] = text

    if "伤害类型" in payload and str(payload.get("伤害类型") or "").strip():
        spec["伤害类型"] = damage_key(payload["伤害类型"])

    if "豁免" in payload and str(payload.get("豁免") or "").strip():
        raw_ab = str(payload["豁免"]).strip()
        ab = _ABILITY_ALIASES.get(raw_ab, _ABILITY_ALIASES.get(raw_ab.lower(), ""))
        if not ab:
            raise ValueError(f"豁免属性看不懂：{raw_ab!r}（力量/敏捷/体质/智力/感知/魅力）")
        spec["豁免"] = ab

    if "半伤" in payload:
        spec["半伤"] = _as_bool("半伤", payload["半伤"])

    if "范围形状" in payload and str(payload.get("范围形状") or "").strip():
        raw_shape = str(payload["范围形状"]).strip()
        shape = _SHAPE_ALIASES.get(raw_shape, raw_shape.lower())
        if shape not in _SHAPES:
            raise ValueError(f"范围形状看不懂：{raw_shape!r}（cone/sphere/cube/line）")
        spec["范围形状"] = shape

    if "描述" in payload:
        text = str(payload.get("描述") or "").strip()
        if len(text) > 1000:
            raise ValueError("描述最多 1000 字")
        if text:
            spec["描述"] = text

    # ---- 类型专属要求：写不清就不让进引擎 ----
    if typ == "attack":
        if "伤害骰" not in spec:
            raise ValueError("攻击型动作要写伤害骰（如 2d6+4）")
        if spec.get("范围"):
            raise ValueError("攻击型动作不支持范围；要做范围伤害请选「豁免型」并写豁免/DC")
    elif typ == "save":
        if "豁免" not in spec:
            raise ValueError("豁免型动作要写豁免属性（如 敏捷）")
        if "DC" not in spec:
            raise ValueError("豁免型动作要写 DC（如 15）")
    elif typ == "heal":
        if "治疗骰" not in spec:
            raise ValueError("治疗型动作要写治疗骰（如 2d4+2）")
    return spec


def _descriptive(name: str, slot: str, why: str, desc: str,
                 notes: list[str], uses: int = 0) -> Action:
    notes.append(f"「{name}」{why} —— 引擎只算动作槽，效果交 DM 裁定")
    return Action(name=name, slot=slot, desc=desc or why,
                  uses_per_day=uses, structured=False)


def build_action(name: str, spec: dict, *, level: int = 1,
                 abilities: dict | None = None) -> tuple[Action, list[str]]:
    """把序列化的技能变成引擎 `Action`。**宽容**：字段坏了就退化成描述型。"""
    notes: list[str] = []
    name = str(name or "").strip()
    if not isinstance(spec, dict):
        return _descriptive(name or "?", "action", "不是对象", "", notes), notes
    typ = ACTION_TYPE_CN.get(str(spec.get("类型") or "").strip(),
                             str(spec.get("类型") or "").strip().lower())
    try:
        slot = _slot_of(spec)
    except ValueError as exc:
        notes.append(str(exc))
        slot = "action"
    uses = 0
    try:
        uses = _as_int("次数", spec.get("次数") or 0, low=0, high=20)
    except ValueError as exc:
        notes.append(str(exc))
    desc = str(spec.get("描述") or "").strip()

    if typ == "attack":
        dmg = str(spec.get("伤害骰") or "").strip()
        if not dmg or not _dice_ok(dmg):
            return _descriptive(name, slot, f"伤害骰「{dmg}」看不懂", desc, notes, uses), notes
        rng_ft = 0
        try:
            rng_ft = int(spec.get("射程") or 0)
        except (TypeError, ValueError):
            notes.append(f"「{name}」的射程不是整数，按近战 5 尺算")
        ranged = rng_ft >= 10
        raw_atk = spec.get("攻击加值")
        if raw_atk in (None, ""):
            ab = abilities or {}
            score = int(ab.get("敏捷" if ranged else "力量", 10))
            atk = ability_modifier(score) + proficiency_bonus(level)
            notes.append(f"「{name}」没写攻击加值，按"
                         f"{'敏捷' if ranged else '力量'}+熟练算成 {atk:+d}")
        else:
            try:
                atk = int(raw_atk)
            except (TypeError, ValueError):
                return _descriptive(name, slot, f"攻击加值「{raw_atk}」不是整数", desc, notes, uses), notes
        raw_dt = str(spec.get("伤害类型") or "").strip()
        dt = damage_key(raw_dt)
        if raw_dt and not (dt in DAMAGE_TYPE_CN):
            notes.append(f"「{name}」的伤害类型「{raw_dt}」不是标准类型，"
                         f"按原样记录（抗性/免疫不会生效）")
        reach = 5
        try:
            reach = int(spec.get("触及") or 5)
        except (TypeError, ValueError):
            notes.append(f"「{name}」的触及不是整数，按 5 尺算")
        attack = Attack(name=name, attack_bonus=atk,
                        damage_notation=dmg, damage_type=dt,
                        reach=None if ranged else reach,
                        range_normal=rng_ft if ranged else None,
                        is_melee=not ranged)
        mech = (f"攻击加值 {atk:+d}｜{dmg} {dt or '（未写伤害类型）'}｜"
                f"{'射程 ' + str(rng_ft) + ' 尺' if ranged else '触及 ' + str(reach) + ' 尺'}")
        return Action(name=name, slot=slot, attacks=[attack],
                      uses_per_day=uses,
                      desc=(desc + "\n（引擎结算：" + mech + "）") if desc else mech), notes

    if typ == "save":
        raw_ab = str(spec.get("豁免") or "").strip()
        ab = _ABILITY_ALIASES.get(raw_ab, _ABILITY_ALIASES.get(raw_ab.lower(), ""))
        dc_raw = spec.get("DC")
        if not ab or dc_raw in (None, ""):
            return _descriptive(name, slot, "缺豁免属性或 DC", desc, notes, uses), notes
        try:
            dc = int(dc_raw)
        except (TypeError, ValueError):
            return _descriptive(name, slot, f"DC「{dc_raw}」不是整数", desc, notes, uses), notes
        dmg = str(spec.get("伤害骰") or "").strip()
        if dmg and not _dice_ok(dmg):
            return _descriptive(name, slot, f"伤害骰「{dmg}」看不懂", desc, notes, uses), notes
        area = 0
        try:
            area = int(spec.get("范围") or 0)
        except (TypeError, ValueError):
            notes.append(f"「{name}」的范围不是整数，按单体结算")
        rng_ft = 0
        try:
            rng_ft = int(spec.get("射程") or 0)
        except (TypeError, ValueError):
            notes.append(f"「{name}」的射程不是整数，按范围尺寸算")
        shape = str(spec.get("范围形状") or "").strip() or ("sphere" if area else "")
        raw_dt = str(spec.get("伤害类型") or "").strip()
        dt = damage_key(raw_dt)
        half = spec.get("半伤")
        if half in (None, ""):
            half = True
        else:
            try:
                half = _as_bool("半伤", half)
            except ValueError as exc:
                notes.append(str(exc))
                half = True
        mech = (f"{ABILITY_CN.get(ab, ab)}豁免 DC {dc}"
                f"｜{dmg + ' ' + dt if dmg else '无伤害，只结算豁免'}"
                + (f"｜{area} 尺 {shape}，成功半伤" if area else "")
                + ("｜成功半伤" if (half and dmg) else ""))
        return Action(name=name, slot=slot, save_ability=ab, save_dc=dc,
                      save_success_half=half, damage_notation=dmg,
                      damage_type=dt, area_size=area, area_shape=shape,
                      range_ft=rng_ft or area, uses_per_day=uses,
                      desc=(desc + "\n（引擎结算：" + mech + "）") if desc else mech), notes

    if typ == "heal":
        heal = str(spec.get("治疗骰") or "").strip()
        if not heal or not _dice_ok(heal):
            return _descriptive(name, slot, f"治疗骰「{heal}」看不懂", desc, notes, uses), notes
        try:
            rng_ft = int(spec.get("射程") or 5)
        except (TypeError, ValueError):
            rng_ft = 5
            notes.append(f"「{name}」的射程不是整数，按 5 尺算")
        mech = f"回复 {heal}｜{rng_ft} 尺内自己或友方"
        return Action(name=name, slot=slot, heal_notation=heal,
                      range_ft=rng_ft or 5, uses_per_day=uses,
                      desc=(desc + "\n（引擎结算：" + mech + "）") if desc else mech), notes

    # utility / 认不出的类型
    if typ not in ACTION_TYPES:
        notes.append(f"「{name}」的类型「{spec.get('类型')}」不认识，按描述型处理")
    return _descriptive(name, slot, "是描述型动作", desc, notes, uses), notes


def describe_spec(spec: dict) -> str:
    """一行给人看的机械摘要（网页消息/日志用）。"""
    if not isinstance(spec, dict):
        return "—"
    typ = str(spec.get("类型") or "")
    bits = [ACTION_TYPE_LABEL.get(typ, typ or "?")]
    slot = spec.get("动作槽")
    if slot == "bonus_action":
        bits.append("附赠动作")
    if spec.get("攻击加值") not in (None, ""):
        bits.append(f"命中 {int(spec['攻击加值']):+d}")
    if spec.get("伤害骰"):
        dt = DAMAGE_TYPE_CN.get(str(spec.get("伤害类型") or ""),
                                str(spec.get("伤害类型") or ""))
        bits.append(f"{spec['伤害骰']} {dt}".strip())
    if spec.get("豁免"):
        bits.append(f"{ABILITY_CN.get(str(spec['豁免']), spec['豁免'])}豁免"
                    + (f" DC {spec['DC']}" if spec.get("DC") else ""))
    if spec.get("治疗骰"):
        bits.append(f"回复 {spec['治疗骰']}")
    if spec.get("范围"):
        bits.append(f"{spec['范围']} 尺{spec.get('范围形状') or ''}")
    if spec.get("次数"):
        bits.append(f"{spec['次数']}/日")
    return "｜".join(bits)

