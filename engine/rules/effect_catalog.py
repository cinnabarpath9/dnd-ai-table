# coding=utf-8
"""词条库（H3）：引擎承认的**全部**装备/生物效果，按品级分档。

DM 不直接写数值，而是挑词条；引擎再把词条组装成真实效果：

    · 词条是**封闭词汇**：只认下面这些「触发 × 效果」与「常驻 × 属性」，
      写不进库的效果，引擎不认，界面也不假装有；
    · 品级 = 稀有度/强度，越高越稀有；引擎会按效果**兜底最低品级**，
      防止"自称常见"的免死；
    · 数值固定，不让 DM 手填出"每回合免死"这种怪物；
    · 调平衡只改这里一处；旧物品按 id 重新解析。

载体：
    · 装备：`物品.词条 = ["ward_grace"]`（适用=装备/通用）
    · 生物：`世界.角色.<名>.词条` 或 `角色列表.<名>.词条`（适用=生物/通用）
    · DM 自写：`世界.词条库.<id>`（只在本战役有效，跟存档）

品级：1 常见 / 2 精良 / 3 稀有 / 4 传说。一件装备最多 3 条、Σ品级 ≤ 6。
"""

from __future__ import annotations

__all__ = [
    "TIER_NAMES", "MAX_ENTRIES", "TIER_BUDGET", "ENTRIES",
    "TRIGGERS", "EFFECTS", "TRIGGER_ALIASES", "EFFECT_ALIASES",
    "normalize_pair", "is_builtin",
    "PASSIVE_KINDS", "APPLIES", "get_entry", "catalog_payload",
    "validate_entry", "required_tier", "validate_item_entries",
    "summarize", "item_tier_total", "character_budget",
    "equipped_tier_total", "check_equip_budget",
]

TIER_NAMES = {1: "常见", 2: "精良", 3: "稀有", 4: "传说"}
MAX_ENTRIES = 3
TIER_BUDGET = 6
PASSIVE_KINDS = ("ac", "attack", "save", "speed", "hp")
APPLIES = ("装备", "生物", "通用")

#: 触发式效果只认这三对；`常驻` 的走 PASSIVE_KINDS
TRIGGERS = ("致命伤害", "战斗开始", "回合开始")
EFFECTS = ("免死", "临时生命", "回复")
VALID_PAIRS = {("致命伤害", "免死"), ("战斗开始", "临时生命"),
               ("回合开始", "回复")}
#: 读/写入口共用同一套别名（写入口以前只认中文原词，外审 GLM A 第 8 条）
TRIGGER_ALIASES = {"lethal": "致命伤害", "lethal_damage": "致命伤害",
                   "battle_start": "战斗开始", "combat_start": "战斗开始",
                   "turn_start": "回合开始"}
EFFECT_ALIASES = {"death_ward": "免死", "temp_hp": "临时生命",
                  "heal": "回复"}


def normalize_pair(raw_trigger, raw_effect) -> tuple[str, str]:
    """把中/英/别名统一成词汇表里的词；**不做合法性判断**（交给调用方）。"""
    trig = str(raw_trigger or "").strip()
    eff = str(raw_effect or "").strip()
    return (TRIGGER_ALIASES.get(trig, trig),
            EFFECT_ALIASES.get(eff, eff))

ENTRIES: list[dict] = [
    {
        "id": "guard_1", "名称": "轻甲片", "品级": 1, "适用": "通用",
        "触发": "常驻", "kind": "ac", "数值": 1,
        "说明": "装备期间 AC +1。",
    },
    {
        "id": "guard_2", "名称": "守御纹", "品级": 2, "适用": "装备",
        "触发": "常驻", "kind": "ac", "数值": 2,
        "说明": "装备期间 AC +2。",
    },
    {
        "id": "aim_1", "名称": "鹰眼", "品级": 2, "适用": "装备",
        "触发": "常驻", "kind": "attack", "数值": 1,
        "说明": "装备期间攻击检定 +1。",
    },
    {
        "id": "steady_1", "名称": "定神", "品级": 2, "适用": "装备",
        "触发": "常驻", "kind": "save", "数值": 1,
        "说明": "装备期间所有豁免 +1。",
    },
    {
        "id": "vigor_minor", "名称": "战前鼓舞", "品级": 1, "适用": "装备",
        "触发": "战斗开始", "效果": "临时生命", "数值": 3, "每场": 1, "冷却": 0,
        "说明": "每场战斗开始时获得 3 点临时生命。",
    },
    {
        "id": "vigor_major", "名称": "不落之旗", "品级": 2, "适用": "装备",
        "触发": "战斗开始", "效果": "临时生命", "数值": 6, "每场": 1, "冷却": 0,
        "说明": "每场战斗开始时获得 6 点临时生命。",
    },
    {
        "id": "moss_minor", "名称": "苔藓再生", "品级": 2, "适用": "装备",
        "触发": "回合开始", "效果": "回复", "数值": 1, "每场": 0, "冷却": 1,
        "说明": "每个自己的回合开始回复 1 点（隔一回合）。",
    },
    {
        "id": "moss_major", "名称": "常青之心", "品级": 3, "适用": "装备",
        "触发": "回合开始", "效果": "回复", "数值": 3, "每场": 0, "冷却": 2,
        "说明": "每个自己的回合开始回复 3 点（隔两回合）。",
    },
    {
        "id": "ward_minor", "名称": "余荫（次）", "品级": 3, "适用": "装备",
        "触发": "致命伤害", "效果": "免死", "数值": 0,
        "保留生命": 1, "每场": 1, "冷却": 0,
        "说明": "每场一次：致命伤害不把你打到 0，保留 1 点生命。",
    },
    {
        "id": "ward_grace", "名称": "生命之神的余荫", "品级": 4, "适用": "装备",
        "触发": "致命伤害", "效果": "免死", "数值": 8,
        "保留生命": 1, "每场": 1, "冷却": 0,
        "说明": "每场一次：致命伤害保留 1 点生命，并立即回复 8 点。",
    },
    {
        "id": "swift_10", "名称": "千里之足", "品级": 2, "适用": "生物",
        "触发": "常驻", "kind": "speed", "数值": 10,
        "说明": "速度 +10 尺（坐骑/生物词条）。",
    },
    {
        "id": "tough_8", "名称": "厚皮", "品级": 2, "适用": "生物",
        "触发": "常驻", "kind": "hp", "数值": 8,
        "说明": "生命上限 +8（生物词条）。",
    },
]

_BY_ID = {e["id"]: e for e in ENTRIES}


def _merge(extra: dict | None) -> dict:
    """内置 + 本战役自写词条（id 冲突时以本战役为准？不允许冲突，写入时挡）。"""
    if not extra:
        return _BY_ID
    merged = dict(_BY_ID)
    for k, v in extra.items():
        if isinstance(v, dict) and str(k) not in merged:
            merged[str(k)] = v
    return merged


def is_builtin(entry_id: str) -> bool:
    """这个 id 是不是内置词条（DM 自写不能覆盖）。"""
    return str(entry_id or "").strip() in _BY_ID


def get_entry(entry_id: str, extra: dict | None = None) -> dict | None:
    return _merge(extra).get(str(entry_id or "").strip())


def _int(val, field: str, low: int, high: int) -> int:
    try:
        num = int(str(val).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数（拿到 {val!r}）") from exc
    if not (low <= num <= high):
        raise ValueError(f"{field} 超出范围 {low}..{high}（拿到 {num}）")
    return num


def required_tier(entry: dict) -> int:
    """按效果强度算**最低**品级；DM 自写词条不能低于这个数。"""
    trigger = str(entry.get("触发") or "")
    if trigger == "常驻":
        kind = str(entry.get("kind") or "")
        val = int(entry.get("数值") or 0)
        if kind == "ac":
            return 2 if val >= 2 else 1
        if kind == "attack":
            return 2 if val >= 1 else 1
        if kind == "save":
            return 2
        if kind == "speed":
            return 1 if val <= 5 else 2
        if kind == "hp":
            return 2 if val <= 8 else 3
        return 1
    effect = str(entry.get("效果") or "")
    val = int(entry.get("数值") or 0)
    if effect == "免死":
        return 4 if val > 0 else 3
    if effect == "临时生命":
        return 3 if val > 6 else 2
    if effect == "回复":
        return 3 if (val >= 3 or int(entry.get("冷却") or 0) == 0) else 2
    return 1


def validate_entry(entry: dict, *, allow_id: str = "") -> dict:
    """校验/归一一条词条（内置或 DM 自写都走这里）。不合法抛 ValueError。"""
    if not isinstance(entry, dict):
        raise ValueError("词条必须是对象")
    name = str(entry.get("名称") or entry.get("name") or "").strip()
    if not name:
        raise ValueError("词条要有名称")
    if len(name) > 40:
        raise ValueError("词条名称最多 40 字")
    applies = str(entry.get("适用") or "通用").strip()
    if applies not in APPLIES:
        raise ValueError(f"适用必须是 {APPLIES}（拿到 {applies!r}）")
    trigger, eff_norm = normalize_pair(entry.get("触发") or "常驻",
                                       entry.get("效果") or "")
    out = {"名称": name, "品级": _int(entry.get("品级"), "品级", 1, 4),
           "适用": applies, "触发": trigger}
    if trigger == "常驻":
        kind = str(entry.get("kind") or entry.get("属性") or "").strip()
        if kind not in PASSIVE_KINDS:
            raise ValueError(f"常驻词条的属性必须是 {PASSIVE_KINDS}（拿到 {kind!r}）")
        out["kind"] = kind
        hi = 50 if kind in ("hp", "speed") else 10
        out["数值"] = _int(entry.get("数值"), "数值", -20, hi) if kind in (
            "ac", "attack", "save") else _int(entry.get("数值"), "数值", 0, hi)
        note = str(entry.get("说明") or "").strip()
    else:
        effect = eff_norm
        if (trigger, effect) not in VALID_PAIRS:
            raise ValueError(f"「{trigger}」不能配「{effect}」；"
                             f"可用组合 {sorted(VALID_PAIRS)}")
        out["效果"] = effect
        out["数值"] = _int(entry.get("数值", 0), "数值", 0, 100)
        if effect in ("临时生命", "回复") and out["数值"] <= 0:
            raise ValueError(f"「{effect}」的数值必须大于 0（不然是空效果）")
        if effect == "免死":
            out["保留生命"] = _int(entry.get("保留生命", 1), "保留生命", 1, 20)
        out["每场"] = _int(entry.get("每场",
                                   0 if effect == "回复" else 1), "每场", 0, 10)
        out["冷却"] = _int(entry.get("冷却", 0), "冷却", 0, 10)
        note = str(entry.get("说明") or "").strip()
    if len(note) > 200:
        raise ValueError("说明最多 200 字")
    if note:
        out["说明"] = note
    floor = required_tier(out)
    if out["品级"] < floor:
        raise ValueError(f"「{name}」的效果至少要 {TIER_NAMES[floor]}"
                         f"（品级 {floor}）；自称 {TIER_NAMES[out['品级']]} 不允许")
    if allow_id:
        out["id"] = allow_id
    return out


def catalog_payload(extra: dict | None = None) -> list[dict]:
    """给网页下拉用的词条表（内置 + 本战役自写）。"""
    out = []
    for eid, e in _merge(extra).items():
        row = dict(e)
        row["id"] = eid
        row["品级名"] = TIER_NAMES.get(int(e.get("品级") or 1), "")
        row["自定义"] = eid not in _BY_ID
        out.append(row)
    out.sort(key=lambda r: (int(r.get("品级") or 1), r.get("id") or ""))
    return out


def validate_item_entries(ids, extra: dict | None = None) -> tuple[list[str], list[str]]:
    """校验一件物品的词条组合。返回 `(干净的 id 列表, 错误)`。"""
    if ids in (None, ""):
        return [], []
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, (list, tuple)):
        return [], ["词条必须是数组（如 [\"ward_grace\"]）"]
    catalog = _merge(extra)
    clean: list[str] = []
    errors: list[str] = []
    total = 0
    for raw in ids:
        eid = str(raw or "").strip()
        entry = catalog.get(eid)
        if entry is None:
            errors.append(f"没有这个词条：{eid!r}")
            continue
        if entry.get("适用") == "生物":
            errors.append(f"「{entry.get('名称')}」是生物词条，不能装在物品上")
            continue
        if eid in clean:
            errors.append(f"词条重复：{eid}")
            continue
        clean.append(eid)
        total += int(entry["品级"])
    if len(clean) > MAX_ENTRIES:
        errors.append(f"一件装备最多 {MAX_ENTRIES} 条词条（现在 {len(clean)} 条）")
    if total > TIER_BUDGET:
        errors.append(f"词条总品级 {total} 超过上限 {TIER_BUDGET}"
                      f"（品级：常见1/精良2/稀有3/传说4）")
    return clean, errors


def summarize(ids, extra: dict | None = None) -> str:
    """`生命之神的余荫(传说) + 轻甲片(常见)` 这样的一行。"""
    catalog = _merge(extra)
    bits = []
    for eid in ids or []:
        entry = catalog.get(str(eid))
        if entry is None:
            bits.append(f"{eid}(未知)")
            continue
        bits.append(f"{entry.get('名称')}({TIER_NAMES.get(int(entry.get('品级') or 1), '?')})")
    return " + ".join(bits) if bits else "无词条"


def item_tier_total(item: dict, extra: dict | None = None) -> int:
    """一件物品的词条品级合计（未知 id 不计）。"""
    catalog = _merge(extra)
    total = 0
    for eid in (item or {}).get("词条") or []:
        entry = catalog.get(str(eid))
        if entry is not None:
            total += int(entry.get("品级") or 0)
    return total


def character_budget(level: int) -> int:
    """一个角色**已装备**词条的品级总预算：2 + 等级（最低 3）。"""
    return max(3, 2 + int(level or 1))


def equipped_tier_total(items: dict, *, exclude: str = "",
                        extra: dict | None = None) -> int:
    """当前已装备、非日常物品的词条品级合计。"""
    from .equipment import is_daily

    total = 0
    for name, item in (items or {}).items():
        if name == exclude or not isinstance(item, dict):
            continue
        if not item.get("已装备") or is_daily(item):
            continue
        total += item_tier_total(item, extra)
    return total


def check_equip_budget(items: dict, name: str, item: dict, level: int,
                       extra: dict | None = None) -> str:
    """装备前的预算闸。返回错误文案（空 = 可以穿）。"""
    add = item_tier_total(item, extra)
    if not add:
        return ""
    used = equipped_tier_total(items, exclude=name, extra=extra)
    limit = character_budget(level)
    if used + add > limit:
        return (f"词条品级预算不够：已装备 {used} + 这件 {add} > "
                f"等级 {level} 的上限 {limit}"
                f"（先换掉高品级装备，或升级后再穿）")
    return ""