# coding=utf-8
"""护甲与 AC —— **装备必须真的改变数值，不能只换图标**。

规则（2024 口径）：

- 无甲：10 + 敏捷调整值；
- 轻甲：甲值 + 敏捷调整值（不封顶）；
- 中甲：甲值 + 敏捷调整值（最多 +2）；
- 重甲：甲值，不加敏捷；
- 盾牌：+2（SRD 的盾牌记录 `ac_base=2`）。

只认 SRD 的 armor 表，不手搓数值。中文物品名通过 `_ARMOR_CN` 对应到英文名。
"""

from __future__ import annotations

from ..srd.store import NotFound, get_store
from .constants import ability_modifier
from .equipment import is_accessory, is_daily, slot_of

__all__ = ["ArmorError", "armor_class", "apply_armor_class", "armor_profile",
           "homebrew_armor_profile"]

_ARMOR_KIND_CN = {"轻甲": "light", "中甲": "medium", "重甲": "heavy",
                  "盾牌": "shield", "light": "light", "medium": "medium",
                  "heavy": "heavy", "shield": "shield"}

_ARMOR_CN = {
    "链甲": "Chain Mail",
    "链甲衫": "Chain Shirt",
    "皮甲": "Leather Armor",
    "镶钉皮甲": "Studded Leather Armor",
    "鳞甲": "Scale Mail",
    "板甲": "Plate Armor",
    "半板甲": "Half Plate Armor",
    "胸甲": "Breastplate",
    "环甲": "Ring Mail",
    "皮板甲": "Hide Armor",
    "软甲": "Padded Armor",
    "盾牌": "Shield",
}


class ArmorError(ValueError):
    """护甲数据不合法（SRD 里没有、类别不对……）。"""


def armor_profile(name: str) -> dict | None:
    """按名字找护甲；先原样，再中文映射。找不到返回 None（不静默猜）。"""
    try:
        store = get_store()
        try:
            return store.by_name("armor", name)
        except NotFound:
            en = _ARMOR_CN.get(name)
            if en:
                return store.by_name("armor", en)
    except NotFound:
        return None
    return None


def _category(rec: dict) -> str:
    cat = str(rec.get("category") or "").lower()
    if cat == "heavy" and str(rec.get("name") or "").lower() == "shield":
        return "shield"
    return cat


def homebrew_armor_profile(item: dict | None) -> dict | None:
    """DM 临场护甲：从物品上的 `护甲加值` / `护甲类型` 造一条和 SRD 同形的记录。

    **只有 DM 明确写了护甲加值才算**；没写就返回 None，回落到 SRD 查表。
    """
    if not isinstance(item, dict):
        return None
    raw = item.get("护甲加值")
    if raw in (None, ""):
        return None
    try:
        base = int(raw)
    except (TypeError, ValueError):
        return None
    kind = _ARMOR_KIND_CN.get(str(item.get("护甲类型") or "").strip().lower())
    if kind is None:
        kind = _ARMOR_KIND_CN.get(str(item.get("护甲类型") or "").strip())
    if kind is None:
        return None
    return {
        "name": str(item.get("名称") or item.get("name") or "DM 护甲"),
        "category": kind,
        "ac_base": base,
        "ac_add_dexmod": kind in ("light", "medium"),
        "ac_cap_dexmod": 2 if kind == "medium" else None,
    }


def armor_class(state, character: str) -> tuple[int, list[str], list[str]]:
    """算 AC，返回 `(ac, 明细, 警告)`。**不改状态。**"""
    c = state.read(f"/角色列表/{character}") or {}
    abilities = c.get("属性") or {}
    dex = ability_modifier(int(abilities.get("敏捷", 10)))
    items = c.get("物品") or {}
    body: list[tuple[str, dict]] = []
    shields: list[tuple[str, dict]] = []
    warnings: list[str] = []
    accessory_ac = 0
    #: 头盔/下装/靴子上的固定 `护甲加值`（H3）：只加，不改变护甲公式
    slot_ac = 0
    slot_parts: list[str] = []
    for name, item in items.items():
        if not isinstance(item, dict) or not item.get("已装备"):
            continue
        if is_daily(item):
            continue
        if is_accessory(item):
            try:
                accessory_ac += int(item.get("护甲加值") or 0)
            except (TypeError, ValueError):
                warnings.append(f"饰品「{name}」的护甲加值不是数字，未计入")
            continue
        slot = slot_of(item, name)
        if slot in ("头盔", "下装", "靴子"):
            try:
                val = int(item.get("护甲加值") or 0)
            except (TypeError, ValueError):
                warnings.append(f"「{name}」的护甲加值不是数字，未计入")
                continue
            if val:
                slot_ac += val
                slot_parts.append(f"{name} {val:+d}")
            continue
        cat = str(item.get("类别") or "").lower()
        if slot == "左手" and cat in ("盾牌", "shield"):
            rec = homebrew_armor_profile(item) or armor_profile(name)
            if rec is None:
                warnings.append(f"「{name}」不是 SRD 护甲，未计入 AC")
            else:
                shields.append((name, rec))
            continue
        if slot != "上装" or cat not in ("护甲", "armor"):
            continue
        rec = homebrew_armor_profile(item) or armor_profile(name)
        if rec is None:
            warnings.append(f"「{name}」不是 SRD 护甲，未计入 AC")
            continue
        body.append((name, rec))

    parts: list[str] = []
    if len(body) > 1:
        warnings.append("同时装备了多件护甲：只按第一件算，请检查背包")
    if len(shields) > 1:
        warnings.append("同时装备了多面盾牌：只按第一面算")

    if body:
        name, rec = body[0]
        base = int(rec.get("ac_base") or 10)
        add = bool(rec.get("ac_add_dexmod"))
        cap = rec.get("ac_cap_dexmod")
        bonus = dex if add else 0
        if add and cap is not None:
            bonus = min(bonus, int(cap))
        ac = base + bonus
        parts.append(f"{name} {base}")
        if add:
            parts.append(f"敏捷 {bonus:+d}" + (f"（上限 {cap}）" if cap is not None else ""))
        req = rec.get("strength_score_required")
        if req is not None and int(abilities.get("力量", 10)) < int(req):
            warnings.append(f"「{name}」需要力量 {req}，当前力量不足：速度 -10 尺")
    else:
        ac = 10 + dex
        parts.append(f"无甲 10 敏捷 {dex:+d}")

    if shields:
        name, rec = shields[0]
        bonus = int(rec.get("ac_base") or 0)
        ac += bonus
        parts.append(f"{name} {bonus:+d}")

    if accessory_ac:
        ac += accessory_ac
        parts.append(f"饰品 {accessory_ac:+d}")

    if slot_ac:
        ac += slot_ac
        parts.extend(slot_parts)

    return ac, parts, warnings


def apply_armor_class(state, character: str, *, actor: str,
                      reason: str) -> tuple[int, list[str], list[str]]:
    """重算并写回 `护甲等级`（走 commit + schema）。"""
    ac, parts, warnings = armor_class(state, character)
    state.commit([{"op": "replace",
                   "path": f"/角色列表/{character}/护甲等级",
                   "value": int(ac)}],
                 actor=actor, reason=reason)
    return ac, parts, warnings