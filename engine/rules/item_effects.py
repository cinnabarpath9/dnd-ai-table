# coding=utf-8
"""装备特效（H3）：DM 写得出、引擎算得动的"触发式效果"。

不是"加个 AC 就完了"：一件斗篷可以写"每场战斗一次，受致命伤时免死并回复 8"。
这里定义**受支持的词汇表**，写不进去的组合直接报错 —— 不让人以为写了就有效。

支持（v1）：

    触发          效果        说明
    致命伤害      免死        本次伤害不把人打到 0；保留 `保留生命` 点，再回复 `数值`
    战斗开始      临时生命    开战时获得 `数值` 点临时生命（2024：取高不叠加）
    回合开始      回复        自己回合开始回复 `数值` 点

每一条都可以带：
    `每场`  每场战斗可用次数（0 = 不限）；开战时重置
    `冷却`  用掉之后要等几个自己的回合

⚠️ 只改**引擎里的结算**：触发点在 `take_damage` / `start_combat` / `reset_turn`。
没接的（条件伤害、光环、叠层、改骰）不要写成"已支持"。
"""

from __future__ import annotations

__all__ = [
    "TRIGGERS", "EFFECTS", "VALID_PAIRS", "coerce_item_effects",
    "effects_from_item", "item_mods_from_item", "build_charges",
    "reset_charges", "tick_cooldowns", "available", "consume",
    "try_death_ward", "trigger_turn_start", "trigger_battle_start",
    "describe",
]

from .effect_catalog import (EFFECTS, TRIGGERS, VALID_PAIRS, get_entry,
                             normalize_pair)
from ..state.coerce import int_or

_LIMITS = {"数值": (0, 100), "保留生命": (1, 20), "每场": (0, 10),
           "冷却": (0, 10)}


def _int_field(field: str, val, *, low: int, high: int) -> int:
    try:
        num = int(str(val).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数（拿到 {val!r}）") from exc
    if not (low <= num <= high):
        raise ValueError(f"{field} 超出范围 {low}..{high}（拿到 {num}）")
    return num


def coerce_effect(spec: dict) -> dict:
    """一条装备特效的写入口校验。"""
    if not isinstance(spec, dict):
        raise ValueError("特效必须是对象")
    name = str(spec.get("名称") or spec.get("name") or "").strip()
    if not name:
        raise ValueError("特效要有名称")
    if len(name) > 40:
        raise ValueError("特效名称最多 40 字")
    raw_trigger = str(spec.get("触发") or spec.get("trigger") or "").strip()
    raw_effect = str(spec.get("效果") or spec.get("effect") or "").strip()
    trigger, effect = normalize_pair(raw_trigger, raw_effect)
    if trigger not in TRIGGERS:
        raise ValueError(f"触发必须是 {TRIGGERS}（拿到 {raw_trigger!r}）")
    if effect not in EFFECTS:
        raise ValueError(f"效果必须是 {EFFECTS}（拿到 {raw_effect!r}）")
    if (trigger, effect) not in VALID_PAIRS:
        raise ValueError(f"「{trigger}」不能配「{effect}」；"
                         f"可用的组合只有 {sorted(VALID_PAIRS)}")

    out = {"名称": name, "触发": trigger, "效果": effect}
    for field, rng in _LIMITS.items():
        if field in spec and spec.get(field) not in (None, ""):
            out[field] = _int_field(field, spec[field], low=rng[0], high=rng[1])
    if "数值" not in out:
        if effect in ("临时生命", "回复"):
            raise ValueError(f"「{effect}」必须写数值")
        out["数值"] = 0
    if effect == "免死" and "保留生命" not in out:
        out["保留生命"] = 1
    if "每场" not in out:
        out["每场"] = 0 if effect == "回复" else 1
    if "冷却" not in out:
        out["冷却"] = 0
    return out


def coerce_item_effects(raw) -> list[dict]:
    """物品上的 `特效` 字段：单条或数组都认。"""
    if raw in (None, ""):
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [coerce_effect(x) for x in items]


def effects_from_item(item_name: str, item: dict,
                      extra: dict | None = None) -> list[dict]:
    """读入口（宽容）：物品上写坏的特效跳过并留原因，不让整桌崩。

    来源：
      · `特效`：自由写法（旧/高级用法，写入口仍逐条校验）；
      · `词条`：从词条库按 id 解析（网页默认走这条）。
    返回的每条都会补上 `物品` 字段（充能键用）。
    """
    out: list[dict] = []
    for spec in (item or {}).get("特效") or []:
        try:
            eff = coerce_effect(spec)
        except ValueError:
            continue
        eff["物品"] = str(item_name)
        out.append(eff)
    for eid in (item or {}).get("词条") or []:
        entry = get_entry(str(eid), extra)
        if entry is None or entry.get("触发") == "常驻":
            continue
        if entry.get("适用") == "生物":
            continue
        try:
            eff = coerce_effect(entry)
        except ValueError:
            continue                 # 坏词条（手改存档）跳过，不炸穿建单位
        eff["物品"] = str(item_name)
        eff["词条"] = str(eid)
        out.append(eff)
    return out


def item_mods_from_item(item_name: str, item: dict,
                        extra: dict | None = None) -> list[dict]:
    """`常驻` 词条 → 挂在单位上的永久修正（scope="item"）。"""
    mods: list[dict] = []
    for eid in (item or {}).get("词条") or []:
        entry = get_entry(str(eid), extra)
        if entry is None or entry.get("触发") != "常驻":
            continue
        if entry.get("适用") == "生物":
            continue
        try:
            value = int(entry.get("数值") or 0)
        except (TypeError, ValueError):
            continue                 # 手改存档的坏数值：跳过，不炸建单位
        mods.append({
            "name": str(entry.get("名称") or eid),
            "kind": str(entry.get("kind") or ""),
            "value": value,
            "scope": "item",
            "rounds": 0,
            "target": str(entry.get("object") or ""),
            "source": f"物品:{item_name}",
            "词条": str(eid),
        })
    return mods


def resolve_item(item_name: str, item: dict,
                 extra: dict | None = None) -> tuple[list[dict], list[dict], int]:
    """把一件物品的词条/特效整理成 `(触发式效果, 常驻修正, 生命上限加值)`。"""
    mods = item_mods_from_item(item_name, item, extra)
    hp_bonus = sum(int(m["value"]) for m in mods if m["kind"] == "hp")
    mods = [m for m in mods if m["kind"] != "hp"]
    return effects_from_item(item_name, item, extra), mods, hp_bonus


def resolve_creature(name: str, ids,
                     extra: dict | None = None) -> tuple[list[dict], list[dict], int]:
    """生物词条（坐骑/野兽/人）→ `(触发式效果, 常驻修正, 生命上限加值)`。"""
    effects: list[dict] = []
    mods: list[dict] = []
    hp_bonus = 0
    for eid in ids or []:
        entry = get_entry(str(eid), extra)
        if entry is None:
            continue
        if entry.get("适用") == "装备":
            continue
        if entry.get("触发") == "常驻":
            kind = str(entry.get("kind") or "")
            try:
                val = int(entry.get("数值") or 0)
            except (TypeError, ValueError):
                continue
            if kind == "hp":
                hp_bonus += val
                continue
            mods.append({
                "name": str(entry.get("名称") or eid), "kind": kind,
                "value": val, "scope": "item", "rounds": 0,
                "target": str(entry.get("object") or ""),
                "source": f"生物:{name}", "词条": str(eid),
            })
            continue
        try:
            eff = coerce_effect(entry)
        except ValueError:
            continue
        eff["物品"] = name
        eff["词条"] = str(eid)
        effects.append(eff)
    return effects, mods, hp_bonus


def _key(eff: dict) -> str:
    return f"{eff.get('物品') or ''}:{eff.get('名称') or ''}"


def build_charges(effects: list[dict]) -> dict:
    """每场次数 → 充能字典。

    key 默认 `物品:特效名`；**同一件物品上重名**时追加 `#2`、`#3`，
    并把最终 key 记在 `eff["_charge_key"]` —— 否则同名特效会互相覆盖（外审 GLM A 第 10 条）。
    """
    charges: dict[str, dict] = {}
    used: dict[str, int] = {}
    for eff in effects:
        base = _key(eff)
        used[base] = used.get(base, 0) + 1
        key = base if used[base] == 1 else f"{base}#{used[base]}"
        eff["_charge_key"] = key
        per = int_or(eff.get("每场"))
        charges[key] = {"max": per, "current": per, "cooldown": 0}
    return charges


def _lookup_charge(unit, eff: dict) -> dict | None:
    """取这条特效的充能记录。**兼容旧档**：

    · 新档：用 `build_charges` 写下的 `_charge_key`；
    · 旧档/改名后：key 对不上时，若这件物品只有唯一一条充能记录，就认它
      （外审 GLM A 第 16 条：特效改名后旧 key 失配，`available` 静默 False）。
    """
    charges = unit.item_charges or {}
    key = eff.get("_charge_key") or _key(eff)
    rec = charges.get(key)
    if isinstance(rec, dict):
        return rec
    item = str(eff.get("物品") or "")
    if item:
        cands = [k for k in charges if str(k).startswith(item + ":")]
        if len(cands) == 1:
            rec = charges.get(cands[0])
            if isinstance(rec, dict):
                return rec
    return None


def apply_unit_traits(unit, effects: list[dict], mods: list[dict],
                      hp_bonus: int = 0, *, label: str = "装备特效") -> None:
    """把 `(效果, 常驻修正, 生命加值)` 挂到单位上 —— 两个建单位入口共用。

    外审 GLM A：`make_hero_unit` 与 `build_npc_unit` 各写一遍这段，
    漏一处就漂移。
    """
    unit.item_effects.extend(effects)
    unit.mods.extend(mods)
    if hp_bonus:
        unit.hp_max += hp_bonus
        unit.hp += hp_bonus
    if effects:
        unit.notes.append(label + "：" + "、".join(
            f"{e.get('名称')}（{e.get('物品')}）" if e.get("物品")
            else str(e.get("名称")) for e in effects))


def reset_charges(unit) -> None:
    """开战：每场次数回满、冷却清零。"""
    for rec in (unit.item_charges or {}).values():
        rec["current"] = rec.get("max") or 0
        rec["cooldown"] = 0


def tick_cooldowns(unit) -> None:
    """自己回合开始：冷却 -1（到 0 为止）。

    坏条目（非 dict / cooldown 非数字）跳过或归零：回合开始不能因为手改存档的
    脏数据打断整轮（2026-11-08 外审 P2-4）。
    """
    for rec in (unit.item_charges or {}).values():
        if not isinstance(rec, dict):
            continue
        if not rec.get("cooldown"):
            continue
        rec["cooldown"] = max(0, int_or(rec.get("cooldown")) - 1)


def available(unit, eff: dict) -> bool:
    rec = _lookup_charge(unit, eff)
    if not isinstance(rec, dict):
        return False
    if int_or(rec.get("cooldown")) > 0:
        return False
    per = int_or(rec.get("max"))
    return per == 0 or int_or(rec.get("current")) > 0


def consume(unit, eff: dict) -> None:
    rec = _lookup_charge(unit, eff)
    if rec is None:
        return
    if int(rec.get("max") or 0) > 0:
        rec["current"] = max(0, int(rec.get("current") or 0) - 1)
    rec["cooldown"] = int(eff.get("冷却") or 0)


def try_death_ward(unit, amount: int, res=None) -> str:
    """致命伤害检查：有可用"免死"就挡住，返回给日志的一句话；没有返回空串。

    **只在"本来还有 HP、这一下要归零"时触发**；已经 0 HP 再受伤不触发
    （否则倒地角色被免死原地拉起）。
    """
    if int(amount) <= 0:
        return ""
    hp_before = int(getattr(res, "hp_before", unit.hp + int(amount)))
    if hp_before <= 0 or hp_before - int(amount) > 0:
        return ""
    eff = next((e for e in (unit.item_effects or [])
                if e.get("触发") == "致命伤害" and e.get("效果") == "免死"
                and available(unit, e)), None)
    if eff is None:
        return ""
    consume(unit, eff)
    keep = max(1, int(eff.get("保留生命") or 1))
    unit.hp = min(unit.hp_max, keep)
    healed = unit.heal(int(eff.get("数值") or 0))
    # 留给调用方写 res：这里只改单位，避免同一句笔记写两遍
    return (f"「{eff.get('物品')}」的「{eff.get('名称')}」触发："
            f"免死，HP 保留 {unit.hp}/{unit.hp_max}"
            + (f"（回复 {healed}）" if healed else ""))


def trigger_turn_start(unit) -> list[str]:
    """回合开始：冷却先扣，再触发可用的「回复」。

    冷却口径是"用完后的第 N 个自己的回合开始时跳过"：
    先记下这回合开始时还在冷却的条目，扣完仍跳过，下一回合才可能触发。
    （旧写法先扣后判，`冷却=1` 会在同一回合立刻又触发，等于没冷却。）
    """
    cooling: set[str] = set()
    for k, rec in (unit.item_charges or {}).items():
        # 手改存档可能把条目写成非 dict、或 cooldown 写成 "x"/"1.5"：
        # 回合开始不能因此抛异常打断整轮（2026-11-08 外审 P2-4）。
        if not isinstance(rec, dict):
            continue
        try:
            if int(rec.get("cooldown") or 0) > 0:
                cooling.add(k)
        except (TypeError, ValueError):
            continue
    tick_cooldowns(unit)
    notes: list[str] = []
    for eff in unit.item_effects or []:
        if eff.get("触发") != "回合开始":
            continue
        if _key(eff) in cooling or not available(unit, eff):
            continue
        consume(unit, eff)
        got = unit.heal(int(eff.get("数值") or 0))
        notes.append(f"「{eff.get('物品')}」{eff.get('名称')}：回复 {got}"
                     f"（{unit.hp}/{unit.hp_max}）")
    return notes


def trigger_battle_start(unit) -> list[str]:
    """开战：先重置次数，再触发「临时生命」。"""
    reset_charges(unit)
    notes: list[str] = []
    for eff in unit.item_effects or []:
        if eff.get("触发") != "战斗开始" or not available(unit, eff):
            continue
        consume(unit, eff)
        got = unit.grant_temp_hp(int(eff.get("数值") or 0))
        notes.append(f"「{eff.get('物品')}」{eff.get('名称')}："
                     f"临时生命 +{got}（共 {unit.temp_hp}）")
    return notes


def describe(effect: dict) -> str:
    """一条特效的人话摘要。"""
    eff = effect or {}
    bits = [f"{eff.get('名称')}：{eff.get('触发')} → {eff.get('效果')}"]
    if eff.get("效果") == "免死":
        bits.append(f"保留 {eff.get('保留生命', 1)} 点")
        if int(eff.get("数值") or 0):
            bits.append(f"回复 {eff.get('数值')}")
    elif int(eff.get("数值") or 0):
        bits.append(f"{eff.get('数值')} 点")
    per = int(eff.get("每场") or 0)
    bits.append(f"{per}/场" if per else "不限次数")
    if int(eff.get("冷却") or 0):
        bits.append(f"冷却 {eff.get('冷却')} 回合")
    return "｜".join(bits)