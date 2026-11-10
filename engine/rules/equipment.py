# coding=utf-8
"""装备栏口径（2026-11-06）：8 个战斗栏 + 不限制的日常穿着。

战斗栏：头盔 / 上装 / 下装 / 靴子 / 左手 / 右手 / 饰品1 / 饰品2。

硬规则：

    · 盾牌固定占左手；有盾牌就不能用双手武器；
    · 双手武器占左右手两格，不能和盾牌/另一把武器同时用；
    · 战斗饰品最多 2 件（只算有增益效果的战斗饰品）；
    · 日常服装/饰品不占战斗栏、不限数量，也不参与 AC/攻击计算。

旧物品没有 `装备栏` 字段时按类别/名字推断；**推断是兼容，不是新规则**。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SLOT_NAMES", "ACCESSORY_SLOTS", "DAILY_SLOT", "slot_of", "is_daily",
    "is_accessory", "is_shield", "is_two_handed", "is_combat",
    "slot_map", "validate_equip", "equipped_combat",
]

SLOT_NAMES = ("头盔", "上装", "下装", "靴子", "左手", "右手", "饰品1", "饰品2")
ACCESSORY_SLOTS = ("饰品1", "饰品2")
DAILY_SLOT = "日常"

_TWO_HANDED_WORDS = ("two-handed", "two handed", "twohanded", "双手")


def _cat(item: dict) -> str:
    return str(item.get("类别") or item.get("category") or "").strip().lower()


def is_daily(item: dict | None) -> bool:
    """日常服装/饰品：可随便穿，不占战斗栏，不参与数值。"""
    if not isinstance(item, dict):
        return False
    return (item.get("装备类型") == "日常" or item.get("日常") is True
            or _cat(item) in ("日常", "daily", "服装", "clothing")
            or _cat(item).startswith("日常"))


def is_accessory(item: dict | None) -> bool:
    return isinstance(item, dict) and _cat(item) in (
        "饰品", "accessory", "首饰", "trinket")


def is_shield(item: dict | None) -> bool:
    return isinstance(item, dict) and _cat(item) in ("盾牌", "shield")


def is_two_handed(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("双手") is True or item.get("装备栏") in ("双手", "two-handed"):
        return True
    props = item.get("武器特性") or item.get("properties") or []
    if isinstance(props, str):
        props = [props]
    for p in props:
        if isinstance(p, dict):
            p = p.get("name") or p.get("key") or ""
        if any(w in str(p).lower() for w in _TWO_HANDED_WORDS):
            return True
    return False


def is_combat(item: dict | None) -> bool:
    return isinstance(item, dict) and not is_daily(item)


def slot_of(item: dict | None, name: str = "") -> str:
    """物品的默认/显式战斗栏；日常返回「日常」。**只在旧档推断时用。**"""
    if not isinstance(item, dict):
        return ""
    if is_daily(item):
        return DAILY_SLOT
    explicit = str(item.get("装备栏") or "").strip()
    if explicit in SLOT_NAMES:
        return explicit
    if explicit in ("双手", "two-handed", "双持"):
        return "双手"
    if is_shield(item):
        return "左手"
    cat = _cat(item)
    if cat in ("武器", "weapon"):
        return "双手" if is_two_handed(item) else "右手"
    if cat in ("护甲", "armor"):
        nm = str(name or item.get("名称") or item.get("name") or "")
        if any(w in nm for w in ("头盔", "头冠", "帽子", "帽", "盔")):
            return "头盔"
        if any(w in nm for w in ("靴", "鞋")):
            return "靴子"
        if any(w in nm for w in ("裤", "裙", "下装")):
            return "下装"
        return "上装"
    if is_accessory(item):
        return "饰品"
    nm = str(name or item.get("名称") or item.get("name") or "")
    if any(w in nm for w in ("头盔", "头冠", "帽子", "帽", "盔")):
        return "头盔"
    if any(w in nm for w in ("靴", "鞋")):
        return "靴子"
    if any(w in nm for w in ("裤", "裙")):
        return "下装"
    if any(w in nm for w in ("衣", "甲", "袍", "衫")):
        return "上装"
    return ""


def equipped_combat(items: dict | None) -> list[tuple[str, dict]]:
    return [(str(k), v) for k, v in (items or {}).items()
            if isinstance(v, dict) and v.get("已装备") and is_combat(v)]


def slot_map(items: dict | None) -> dict[str, Any]:
    """给页面/测试的当前栏位表。

    返回 8 个战斗栏 + `日常`（列表）。双手武器会同时占左手/右手；
    没有显式栏位的战斗饰品按出现顺序塞进饰品1/饰品2（最多 2）。
    """
    out: dict[str, Any] = {s: None for s in SLOT_NAMES}
    out[DAILY_SLOT] = []
    acc_i = 0
    for name, item in (items or {}).items():
        if not isinstance(item, dict) or not item.get("已装备"):
            continue
        if is_daily(item):
            out[DAILY_SLOT].append(str(name))
            continue
        if is_accessory(item):
            explicit = str(item.get("装备栏") or "").strip()
            if explicit in ACCESSORY_SLOTS:
                out[explicit] = str(name)
                continue
            while acc_i < len(ACCESSORY_SLOTS) and out[ACCESSORY_SLOTS[acc_i]]:
                acc_i += 1
            if acc_i < len(ACCESSORY_SLOTS):
                out[ACCESSORY_SLOTS[acc_i]] = str(name)
                acc_i += 1
            continue
        slot = slot_of(item, name)
        if slot == "双手":
            out["左手"] = str(name)
            out["右手"] = str(name)
        elif slot in ("左手", "右手"):
            if out[slot] is not None:
                other = "左手" if slot == "右手" else "右手"
                if out[other] is None:
                    slot = other
            out[slot] = str(name)
        elif slot in SLOT_NAMES:
            out[slot] = str(name)
    return out


def _occupied(items: dict, skip: str) -> tuple[set[str], list[str]]:
    """别的已装备战斗物品占了哪些栏、有哪些双手武器。"""
    slots: set[str] = set()
    two_handed: list[str] = []
    for name, item in (items or {}).items():
        if name == skip or not isinstance(item, dict) or not item.get("已装备"):
            continue
        if is_daily(item):
            continue
        if is_accessory(item):
            continue
        slot = slot_of(item, name)
        if slot == "双手":
            slots.update(("左手", "右手"))
            two_handed.append(str(name))
        elif slot:
            slots.add(slot)
    return slots, two_handed


def validate_equip(items: dict, name: str, item: dict,
                   *, target: str | None = None) -> tuple[str, list[str]]:
    """检查这件物品能不能装，返回 `(实际栏位, 错误)`。

    `target` 只对单手武器/护甲/头盔等有意义；饰品会自动选空位。
    日常物品返回 `("日常", [])`，不限数量。
    """
    errors: list[str] = []
    if is_daily(item):
        return DAILY_SLOT, []
    if is_accessory(item):
        count = 0
        free = []
        for other, ov in (items or {}).items():
            if other == name or not isinstance(ov, dict) or not ov.get("已装备"):
                continue
            if not is_accessory(ov) or is_daily(ov):
                continue
            count += 1
            slot = str(ov.get("装备栏") or "").strip()
            if slot in ACCESSORY_SLOTS:
                free.append(slot)
        if count >= 2:
            errors.append("战斗饰品最多 2 件；先卸下一件")
            return "", errors
        for s in ACCESSORY_SLOTS:
            if s not in free:
                return s, []
        errors.append("两个战斗饰品栏都满了")
        return "", errors

    occupied, two_handed = _occupied(items, name)
    if is_shield(item):
        if two_handed:
            errors.append(f"已装备双手武器「{two_handed[0]}」，不能同时拿盾牌")
            return "左手", errors
        if "左手" in occupied:
            errors.append("左手格已被占用")
            return "左手", errors
        return "左手", []

    if is_two_handed(item):
        if "左手" in occupied or "右手" in occupied:
            errors.append("双手武器要占左右手两格，当前手部栏不空")
            return "双手", errors
        return "双手", []

    slot = target if target in SLOT_NAMES else slot_of(item, name)
    if slot in ("左手", "右手"):
        if "左手" in occupied and "右手" in occupied:
            errors.append("左右手都满了")
            return slot, errors
        if slot in occupied:
            other = "左手" if slot == "左手" else "右手"
            if other not in occupied:
                return other, []
            errors.append("左右手都满了")
            return slot, errors
        return slot, []
    if slot in SLOT_NAMES:
        if slot in occupied:
            errors.append(f"{slot}栏已有装备")
            return slot, errors
        return slot, []
    errors.append("这件物品没有可用的战斗栏")
    return "", errors