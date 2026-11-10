# coding=utf-8
"""U12 经济与消耗品：钱包 / 物品数量 / 买卖 / 喝药。**数值全在引擎，零模型调用。**

口径（先写清楚，免得以后把"简化"当成"做完了"）：

1. **钱是状态的一部分**：`角色列表.<名>.钱币 = {金币, 银币, 铜币}`；
   1 金币 = 10 银币 = 100 铜币（换算只在需要"总价值"时用，付款只扣金币）。
2. **物品有数量**：`物品.<名>.数量`（缺省 1）；数量归零就把那条删掉，
   不留下"数量 0 的幽灵物品"。
3. **价格只认 SRD 给的**：治疗药水的价格来自 SRD 魔法物品的 `cost`；
   SRD 没给价的（强效/高等/至强治疗药水）**不猜**，要买就由 DM 显式报价。
4. **治疗效果从 SRD 原文的表格抽**（"Potions of Healing (table)"）；
   抽不到就抛"需 DM 裁定"，不静默按一个错数值回血。
5. **买卖是一个事务**：扣钱与给物品在**同一个 commit** 里，不会出现
   "钱扣了东西没到手"；钱不够直接报错，不赊账。
6. **喝药不在战斗里做**（v1）：战斗中喝药要走附赠动作，还没接进动作菜单；
   本模块只管战斗外/网页背包，战斗中调用会被上层拒绝。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from ..dice import roll
from ..srd.store import NotFound, get_store
from ..state.patch import escape_token
from ..state.tree import StateTree

__all__ = [
    "EconomyError", "COINS", "POTION_CN", "wallet", "add_coins", "spend_coins",
    "item_entry", "item_qty", "give_item", "take_item", "adjust_item",
    "healing_potions", "consumable_spec", "shop_stock", "buy", "sell",
    "use_consumable",
]

COINS = ("金币", "银币", "铜币")
_COPPER = {"金币": 100, "银币": 10, "铜币": 1}

#: SRD 英文名 → 中文名（背包里用中文，查表用英文；查不到就明说）
POTION_CN = {
    "Potion of Healing": "治疗药水",
    "Potion of Greater Healing": "强效治疗药水",
    "Potion of Superior Healing": "高等治疗药水",
    "Potion of Supreme Healing": "至强治疗药水",
}


class EconomyError(ValueError):
    """经济动作不成立（钱不够、物品没有、价格不明、抽不出治疗量……）。"""


def _cp(state: StateTree, who: str) -> str:
    return f"/角色列表/{escape_token(who)}"


def _item_path(who: str, name: str) -> str:
    return f"/角色列表/{escape_token(who)}/物品/{escape_token(name)}"


def _read_int(state: StateTree, path: str, default: int = 0) -> int:
    v = state.read(path, default)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        raise EconomyError(f"{path} 应该是整数（拿到 {v!r}）")
    return int(v)


# --------------------------------------------------------------------------- #
# 钱包
# --------------------------------------------------------------------------- #

def wallet(state: StateTree, who: str) -> dict[str, int]:
    """这个人的钱袋（缺字段按 0 算；非整数直接报错，不 bool() 兜底）。"""
    raw = state.read(f"{_cp(state, who)}/钱币", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise EconomyError(f"{who} 的钱币字段不是对象（{type(raw).__name__}）")
    out = {}
    for coin in COINS:
        v = raw.get(coin, 0)
        if isinstance(v, bool) or not isinstance(v, int):
            raise EconomyError(f"{who} 的{coin}不是整数（{v!r}）")
        out[coin] = int(v)
    return out


def _total_copper(w: dict[str, int]) -> int:
    return sum(int(w.get(c, 0)) * _COPPER[c] for c in COINS)


def add_coins(state: StateTree, who: str, amount: int, *, coin: str = "金币",
              actor: str = "DM", reason: str = "收入") -> dict[str, int]:
    """加钱。**金额必须是正整数**（负数是"扣钱"，走 `spend_coins`）。"""
    if coin not in COINS:
        raise EconomyError(f"币种必须是 {COINS} 之一，拿到 {coin!r}")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise EconomyError(f"金额必须是整数（拿到 {amount!r}）")
    if amount <= 0:
        raise EconomyError(f"加钱的金额必须是正数（拿到 {amount}）")
    w = wallet(state, who)
    patches = []
    if state.read(f"{_cp(state, who)}/钱币") is None:
        patches.append({"op": "insert", "path": f"{_cp(state, who)}/钱币",
                        "value": dict(w)})
    patches.append({"op": "replace", "path": f"{_cp(state, who)}/钱币/{coin}",
                    "value": int(w[coin]) + int(amount)})
    state.commit(patches, actor=actor, reason=f"{reason}：+{amount} {coin}")
    w[coin] = int(w[coin]) + int(amount)
    return w


def spend_coins(state: StateTree, who: str, amount: int, *, coin: str = "金币",
                actor: str = "DM", reason: str = "支出") -> dict[str, int]:
    """扣钱。**钱不够就报错，不把余额扣成负数。**"""
    if coin not in COINS:
        raise EconomyError(f"币种必须是 {COINS} 之一，拿到 {coin!r}")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise EconomyError(f"金额必须是整数（拿到 {amount!r}）")
    if amount <= 0:
        raise EconomyError(f"扣钱的金额必须是正数（拿到 {amount}）")
    w = wallet(state, who)
    if int(w[coin]) < int(amount):
        raise EconomyError(
            f"{who} 付不起：要 {amount} {coin}，只有 {w[coin]}"
            f"（总额 {_total_copper(w)} 铜）")
    state.commit([{"op": "replace", "path": f"{_cp(state, who)}/钱币/{coin}",
                   "value": int(w[coin]) - int(amount)}],
                 actor=actor, reason=f"{reason}：-{amount} {coin}")
    w[coin] = int(w[coin]) - int(amount)
    return w


# --------------------------------------------------------------------------- #
# 物品与数量
# --------------------------------------------------------------------------- #

def item_entry(state: StateTree, who: str, name: str) -> dict | None:
    v = state.read(_item_path(who, name))
    return v if isinstance(v, dict) else None


def item_qty(item: dict | None) -> int:
    """物品数量：缺省 1；**0 表示该删掉了**（不留在背包里）。"""
    if not isinstance(item, dict):
        return 0
    v = item.get("数量", 1)
    if isinstance(v, bool) or not isinstance(v, int):
        raise EconomyError(f"物品「{item.get('名称') or '?'}」的数量不是整数（{v!r}）")
    return int(v)


def give_item(state: StateTree, who: str, name: str, value: dict, *,
              qty: int = 1, actor: str = "DM",
              reason: str = "获得物品") -> dict:
    """给物品：已有就**累加数量**，没有就新建（数量写在 `数量`）。"""
    name = (name or "").strip()
    if not name:
        raise EconomyError("物品名不能为空")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise EconomyError(f"数量必须是正整数（拿到 {qty!r}）")
    cur = item_entry(state, who, name)
    body = dict(value or {})
    body.pop("数量", None)
    if cur is None:
        merged = dict(body)
        merged["数量"] = int(qty)
        state.commit([{"op": "insert", "path": _item_path(who, name),
                       "value": merged}],
                     actor=actor, reason=f"{reason}：{name} ×{qty}")
        return merged
    new = item_qty(cur) + int(qty)
    merged = {**cur, **body, "数量": new}
    state.commit([{"op": "replace", "path": _item_path(who, name),
                   "value": merged}],
                 actor=actor, reason=f"{reason}：{name} ×{qty}")
    return merged


def take_item(state: StateTree, who: str, name: str, *, qty: int = 1,
              actor: str = "DM", reason: str = "消耗物品") -> dict | None:
    """拿走 N 个：扣到 0 就删掉整条。不够就报错，不扣成负数。"""
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise EconomyError(f"数量必须是正整数（拿到 {qty!r}）")
    cur = item_entry(state, who, name)
    if cur is None:
        raise EconomyError(f"{who} 的背包里没有「{name}」")
    have = item_qty(cur)
    if have < qty:
        raise EconomyError(f"「{name}」不够：要 {qty}，只有 {have}")
    left = have - qty
    if left == 0:
        state.commit([{"op": "remove", "path": _item_path(who, name)}],
                     actor=actor, reason=f"{reason}：{name} ×{qty}（用完，移出背包）")
        return None
    state.commit([{"op": "replace", "path": _item_path(who, name),
                   "value": {**cur, "数量": left}}],
                 actor=actor, reason=f"{reason}：{name} ×{qty}（剩 {left}）")
    return {**cur, "数量": left}


def adjust_item(state: StateTree, who: str, name: str, delta: int, *,
                actor: str = "DM", reason: str = "调整数量") -> dict | None:
    """增减数量（DM 发奖励/没收）：正数加、负数减、减到 0 删除。"""
    if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
        raise EconomyError(f"增量必须是非零整数（拿到 {delta!r}）")
    cur = item_entry(state, who, name)
    if delta < 0:
        return take_item(state, who, name, qty=-delta, actor=actor, reason=reason)
    if cur is None:
        raise EconomyError(f"{who} 的背包里没有「{name}」，不能「加数量」")
    merged = {**cur, "数量": item_qty(cur) + int(delta)}
    state.commit([{"op": "replace", "path": _item_path(who, name),
                   "value": merged}], actor=actor,
                 reason=f"{reason}：{name} +{delta}")
    return merged


# --------------------------------------------------------------------------- #
# 消耗品：治疗药水（数值从 SRD 原文的表格抽）
# --------------------------------------------------------------------------- #

_TABLE_RE = re.compile(r"^\|\s*([A-Za-z ]+?)\s*\|\s*[A-Za-z ]+\s*\|"
                       r"\s*([0-9dD+\s]+?)\s*\|", re.M)


@lru_cache(maxsize=1)
def healing_potions() -> dict[str, dict]:
    """`{英文名: {治疗: "2d4+2", 价格: 50 | None, 中文名}}`。

    **数值只从 SRD 原文抽**：找不到那张表就返回空字典（调用方据此说"不知道"）。
    """
    store = get_store()
    try:
        base = store.by_name("magicitems", "Potion of Healing")
    except NotFound:
        return {}
    desc = str(base.get("desc") or "")
    #: SRD 快照里的换行是**字面量的 `\n` 两字符**（JSON 被二次转义过），
    #: 表格是 markdown 行，必须先把它们还原成真换行，`^` 才匹配得到。
    desc = desc.replace("\\r\\n", "\n").replace("\\n", "\n")
    if "Potions of Healing" not in desc:
        return {}
    rows: dict[str, str] = {}
    for m in _TABLE_RE.finditer(desc):
        label = m.group(1).strip().lower()
        formula = m.group(2).replace(" ", "").lower()
        if re.fullmatch(r"\d+d\d+(?:[+-]\d+)?", formula):
            rows[label] = formula
    if not rows:
        return {}
    out: dict[str, dict] = {}
    for rec in store.records("magicitems"):
        name = str(rec.get("name") or "")
        if not name.startswith("Potion of ") or not name.endswith("Healing"):
            continue
        label = name[len("Potion of "):].strip().lower()
        if label not in rows:
            continue
        raw_cost = rec.get("cost")
        price = None
        try:
            price = int(round(float(raw_cost))) if raw_cost is not None else None
        except (TypeError, ValueError):
            price = None
        if price is not None and price <= 0:
            price = None            # SRD 写 0.00 = 没给价，别当成免费
        out[name] = {"治疗": rows[label], "价格": price,
                     "中文名": POTION_CN.get(name, name)}
    return out


def consumable_spec(name: str) -> dict:
    """按名字（中英都认）查消耗品规格；查不到**明说**，不瞎回血。"""
    name = (name or "").strip()
    potions = healing_potions()
    if name in potions:
        return {"英文名": name, **potions[name]}
    for en, info in potions.items():
        if info.get("中文名") == name:
            return {"英文名": en, **info}
    raise EconomyError(
        f"引擎不认识消耗品「{name}」的数值效果（没有 SRD 数据），需 DM 裁定，"
        f"不自动结算")


def use_consumable(state: StateTree, who: str, name: str, *, rng,  # noqa: ANN001
                   actor: str = "玩家", reason: str = "使用消耗品") -> dict:
    """喝药：掷 SRD 骰 → 回血（封顶最大 HP）→ 数量 -1。**一个事务。**"""
    cur = item_entry(state, who, name)
    if cur is None:
        raise EconomyError(f"{who} 的背包里没有「{name}」")
    cat = str(cur.get("类别") or "")
    if cat not in ("消耗品", "consumable"):
        raise EconomyError(f"「{name}」不是消耗品（类别 {cat!r}）")
    effect = cur.get("效果") if isinstance(cur.get("效果"), dict) else {}
    formula = str(effect.get("治疗") or "").strip()
    if not formula:
        formula = str(consumable_spec(name).get("治疗") or "")
    if not formula:
        raise EconomyError(f"「{name}」没有治疗数值，需 DM 裁定")
    healed = int(roll(formula, rng=rng).total)
    hp = state.read(f"{_cp(state, who)}/生命值") or {}
    cur_hp = _read_int(state, f"{_cp(state, who)}/生命值/当前", 0)
    hp_max = _read_int(state, f"{_cp(state, who)}/生命值/最大", cur_hp)
    new_hp = min(hp_max, cur_hp + max(0, healed))
    qty = item_qty(cur)
    patches: list[dict[str, Any]] = [
        {"op": "replace", "path": f"{_cp(state, who)}/生命值/当前",
         "value": int(new_hp)}]
    if qty <= 1:
        patches.append({"op": "remove", "path": _item_path(who, name)})
    else:
        patches.append({"op": "replace", "path": _item_path(who, name),
                        "value": {**cur, "数量": qty - 1}})
    state.commit(patches, actor=actor,
                 reason=f"{reason}：{name}（治疗 {formula} → {healed}）")
    return {"healed": healed, "hp": new_hp, "hp_max": hp_max,
            "left": max(0, qty - 1), "formula": formula,
            "浪费": max(0, healed - (new_hp - cur_hp))}


# --------------------------------------------------------------------------- #
# 商店（数据由世界树给出：某个地点有没有货、卖什么）
# --------------------------------------------------------------------------- #

def shop_stock(state: StateTree, *, place: str | None = None) -> list[str]:
    """当前地点（或指定地点）的货架。**没有商店就是空清单**，不凭空进货。"""
    if place is None:
        place = str(state.read("/世界/当前地点") or "")
    if not place:
        return []
    raw = state.read(f"/世界/地点/{escape_token(place)}/商店", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EconomyError(f"地点「{place}」的商店字段不是数组")
    return [str(x) for x in raw if str(x).strip()]


def _price_of(name: str) -> int | None:
    try:
        return consumable_spec(name).get("价格")
    except EconomyError:
        return None


def buy(state: StateTree, who: str, name: str, *, qty: int = 1,
        unit_price: int | None = None, place: str | None = None,
        actor: str = "玩家", reason: str = "购买") -> dict:
    """买：**货架上有 + 钱够**，扣钱与给物品在同一个 commit。

    价格：优先用显式 `unit_price`；否则查 SRD 的 `cost`；SRD 没给价就报错。
    """
    name = (name or "").strip()
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise EconomyError(f"数量必须是正整数（拿到 {qty!r}）")
    stock = shop_stock(state, place=place)
    if not stock:
        raise EconomyError("这里没有可交易的地方（世界树没给这个地点货架）")
    if name not in stock:
        raise EconomyError(f"这里不卖「{name}」（货架：{stock}）")
    price = unit_price if unit_price is not None else _price_of(name)
    if price is None:
        raise EconomyError(f"SRD 没给「{name}」的价格，需 DM 报价（不猜）")
    if isinstance(price, bool) or not isinstance(price, int) or price < 0:
        raise EconomyError(f"单价必须是非负整数（拿到 {price!r}）")
    total = int(price) * int(qty)
    w = wallet(state, who)
    if int(w["金币"]) < total:
        raise EconomyError(f"{who} 付不起：{qty} × {name} = {total} 金币，"
                           f"只有 {w['金币']}")
    spec = consumable_spec(name)
    item = {"类别": "消耗品", "已装备": False, "数量": int(qty),
            "效果": {"治疗": spec["治疗"]}, "价格": int(price),
            "英文名": spec["英文名"], "描述": "喝下后恢复生命值"}
    patches: list[dict[str, Any]] = [
        {"op": "replace", "path": f"{_cp(state, who)}/钱币/金币",
         "value": int(w["金币"]) - total},
        {"op": "insert", "path": _item_path(who, name), "value": item},
    ]
    cur = item_entry(state, who, name)
    if cur is not None:
        patches[-1] = {"op": "replace", "path": _item_path(who, name),
                       "value": {**cur, "数量": item_qty(cur) + int(qty)}}
    state.commit(patches, actor=actor,
                 reason=f"{reason}：{name} ×{qty}，花费 {total} 金币")
    return {"paid": total, "qty": int(qty), "left_gold": int(w["金币"]) - total,
            "price": int(price)}


def sell(state: StateTree, who: str, name: str, *, qty: int = 1,
         unit_price: int | None = None, place: str | None = None,
         actor: str = "玩家", reason: str = "出售") -> dict:
    """卖：**背包里有 + 这里能交易**；默认可卖价 = SRD 价的一半（向下取整）。"""
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise EconomyError(f"数量必须是正整数（拿到 {qty!r}）")
    cur = item_entry(state, who, name)
    if cur is None:
        raise EconomyError(f"{who} 的背包里没有「{name}」")
    if not shop_stock(state, place=place):
        raise EconomyError("这里没有可交易的地方（世界树没给这个地点货架）")
    if int(item_qty(cur)) < int(qty):
        raise EconomyError(f"「{name}」不够：要卖 {qty}，只有 {item_qty(cur)}")
    price = unit_price
    if price is None:
        base = cur.get("价格")
        if isinstance(base, int) and not isinstance(base, bool):
            price = int(base) // 2
        else:
            price = (_price_of(name) or 0) // 2 or None
    if price is None:
        raise EconomyError(f"SRD 没给「{name}」的价格，需 DM 报价（不猜）")
    if isinstance(price, bool) or not isinstance(price, int) or price < 0:
        raise EconomyError(f"单价必须是非负整数（拿到 {price!r}）")
    total = int(price) * int(qty)
    left = int(item_qty(cur)) - int(qty)
    patches: list[dict[str, Any]] = [
        {"op": "replace", "path": f"{_cp(state, who)}/钱币/金币",
         "value": wallet(state, who)["金币"] + total}]
    if left <= 0:
        patches.append({"op": "remove", "path": _item_path(who, name)})
    else:
        patches.append({"op": "replace", "path": _item_path(who, name),
                        "value": {**cur, "数量": left}})
    state.commit(patches, actor=actor,
                 reason=f"{reason}：{name} ×{qty}，得到 {total} 金币")
    return {"earned": total, "left": max(0, left),
            "gold": wallet(state, who)["金币"]}