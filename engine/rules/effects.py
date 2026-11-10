# coding=utf-8
"""临时修正（H2b）：场景/事件给单位加 **AC / 攻击 / 豁免 / 检定 / 伤害** 修正。

和"造物/人物数值"的区别：那些是**常驻**数值，这里都是**临时的**，到期就掉。

    · 修正挂在 `Unit.mods`（普通 dict，进存档）；
    · `scope="scene"`：换场景时整批清掉；
    · `scope="round"`：每次该单位回合开始（`reset_turn`）扣 1 轮，到 0 就掉；
    · 只改引擎算出来的数，**不改基础数值** —— 所以 `Unit.ac` 永远是"没修正的 AC"，
      要看有效值用 `Unit.ac_total`。

来源有两处：
    1. 剧本 JSON：`scenes[].effects`（见 `docs/DM结构化设定协议.md` §5）；
    2. 网页动作 `dm_apply_effect`（DM 临场加一条）。

⚠️ 不做的事：不改基础属性、不做"叠层/同名叠加"（同名同来源只保留一条），
也不猜持续轮数 —— 写多少就是多少。
"""

from __future__ import annotations

from .constants import ABILITY_CN, ABILITIES, SKILL_ABILITY, SKILL_CN

__all__ = [
    "KINDS", "SCOPES", "EFFECT_TARGETS", "coerce_effect",
    "add_mod", "clear_mods", "tick_round", "total", "describe", "describe_one",
]

KINDS = ("ac", "attack", "save", "skill", "check", "damage")
SCOPES = ("scene", "round")
#: 剧本效果能指定"给谁"：队伍 / 敌人 / 双方
EFFECT_TARGETS = ("party", "enemy", "all")

_KIND_CN = {"ac": "AC", "attack": "攻击", "save": "豁免",
            "skill": "技能", "check": "属性检定", "damage": "伤害"}
_KIND_ALIASES = {**{v: k for k, v in _KIND_CN.items()},
                 **{k: k for k in KINDS},
                 "护甲": "ac", "命中": "attack", "检定": "check"}
_SCOPE_ALIASES = {"scene": "scene", "场景": "scene",
                  "round": "round", "轮": "round", "回合": "round"}

_ABILITY_ALIASES = {**{cn: en for en, cn in ABILITY_CN.items()},
                    **{en: en for en in ABILITIES},
                    "str": "strength", "dex": "dexterity", "con": "constitution",
                    "int": "intelligence", "wis": "wisdom", "cha": "charisma"}
_SKILL_ALIASES = {**{cn: en for en, cn in SKILL_CN.items()},
                  **{en: en for en in SKILL_ABILITY}}


def _ability(raw) -> str:
    text = str(raw or "").strip()
    return _ABILITY_ALIASES.get(text, _ABILITY_ALIASES.get(text.lower(), ""))


def _skill(raw) -> str:
    text = str(raw or "").strip()
    return _SKILL_ALIASES.get(text, _SKILL_ALIASES.get(text.lower(), ""))


def coerce_effect(spec: dict, *, for_scene: bool = False) -> dict:
    """写入口：校验并返回干净的修正 dict。**坏值直接报错，不写半截。**

    `for_scene=True` 时多认 `target`（party/enemy/all）；网页动作不带它。
    """
    if not isinstance(spec, dict):
        raise ValueError("修正必须是对象")
    raw_kind = str(spec.get("kind") or spec.get("类型") or "").strip()
    kind = _KIND_ALIASES.get(raw_kind, _KIND_ALIASES.get(raw_kind.lower(), ""))
    if kind not in KINDS:
        raise ValueError(f"类型必须是 {KINDS}（拿到 {raw_kind!r}）")
    try:
        value = int(str(spec.get("value") if "value" in spec
                        else spec.get("值")).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"值必须是整数（拿到 {spec.get('value')!r}）") from exc
    if not -20 <= value <= 20:
        raise ValueError(f"值超出范围 -20..20（拿到 {value}）")

    scope_raw = str(spec.get("scope") or spec.get("持续") or "scene").strip()
    scope = _SCOPE_ALIASES.get(scope_raw, _SCOPE_ALIASES.get(scope_raw.lower(), ""))
    if scope not in SCOPES:
        raise ValueError(f"持续必须是 scene / round（拿到 {scope_raw!r}）")

    rounds = 0
    if scope == "round":
        raw_rounds = spec.get("rounds", spec.get("轮数", 1))
        try:
            rounds = int(str(raw_rounds).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"轮数必须是整数（拿到 {raw_rounds!r}）") from exc
        if not 1 <= rounds <= 20:
            raise ValueError(f"轮数超出范围 1..20（拿到 {rounds}）")

    target = str(spec.get("object") or spec.get("对象") or "").strip()
    if kind in ("save", "skill", "check") and target:
        key = _ability(target) if kind in ("save", "check") else _skill(target)
        if not key:
            raise ValueError(f"「{target}」不是引擎认识的"
                             f"{'属性' if kind != 'skill' else '技能'}")
        target = key
    elif kind in ("ac", "attack", "damage"):
        target = ""          # 这三类不细分对象

    name = str(spec.get("name") or spec.get("名称") or "").strip()
    if not name:
        raise ValueError("修正要有名称")
    if len(name) > 40:
        raise ValueError("名称最多 40 字")

    out = {"name": name, "kind": kind, "value": value,
           "scope": scope, "rounds": rounds, "target": target}
    if for_scene:
        who = str(spec.get("target") or "party").strip().lower()
        if who not in EFFECT_TARGETS:
            raise ValueError(f"target 必须是 {EFFECT_TARGETS}（拿到 {who!r}）")
        out["target_who"] = who
    return out


def add_mod(unit, effect: dict, *, source: str = "") -> dict:
    """挂一条修正到单位上。同名同来源 → 替换（不叠层）。"""
    mod = dict(effect)
    mod["source"] = str(source or "")
    keep = [m for m in unit.mods
            if not (m.get("name") == mod["name"]
                    and m.get("source") == mod["source"])]
    keep.append(mod)
    unit.mods = keep
    return mod


def clear_mods(unit, *, scope: str | None = None) -> int:
    """清修正：给 scope 就只清那一档；不给就全清。返回清掉几条。"""
    before = len(unit.mods)
    if scope is None:
        unit.mods = []
    else:
        unit.mods = [m for m in unit.mods if m.get("scope") != scope]
    return before - len(unit.mods)


def tick_round(unit) -> list[str]:
    """回合开始：轮次修正 -1，到 0 掉。返回掉了哪些（给日志）。"""
    expired: list[str] = []
    keep = []
    for m in unit.mods:
        if m.get("scope") != "round":
            keep.append(m)
            continue
        left = int(m.get("rounds") or 0) - 1
        if left <= 0:
            expired.append(str(m.get("name") or "?"))
            continue
        m = dict(m)
        m["rounds"] = left
        keep.append(m)
    unit.mods = keep
    return expired


def total(unit, kind: str, target: str = "") -> int:
    """某类修正的合计值。`target` 为空 = 不区分对象。"""
    key = str(target or "").strip()
    out = 0
    for m in getattr(unit, "mods", None) or []:
        if m.get("kind") != kind:
            continue
        mt = str(m.get("target") or "")
        if key and mt and mt != key:
            continue
        out += int(m.get("value") or 0)
    return out


def describe_one(effect: dict) -> str:
    """一条修正的人话摘要（消息/日志用）。"""
    kind = _KIND_CN.get(str(effect.get("kind")), str(effect.get("kind")))
    obj = str(effect.get("target") or "")
    if obj:
        kind += (f"·{ABILITY_CN.get(obj, obj)}"
                 if effect.get("kind") in ("save", "check") else f"·{obj}")
    scope = ("本场景" if effect.get("scope") == "scene"
             else f"{effect.get('rounds')} 轮")
    return (f"{effect.get('name')} {int(effect.get('value') or 0):+d} {kind}"
            f"（{scope}）")


def describe(unit) -> list[str]:
    """给界面/日志的一行行说明：`高地 +1 攻击（本场景）`。"""
    out = []
    for m in getattr(unit, "mods", None) or []:
        kind = _KIND_CN.get(str(m.get("kind")), str(m.get("kind")))
        obj = str(m.get("target") or "")
        if obj:
            kind += f"·{ABILITY_CN.get(obj, obj)}" if m.get("kind") in ("save", "check") else f"·{obj}"
        scope = "本场景" if m.get("scope") == "scene" else f"{m.get('rounds')} 轮"
        out.append(f"{m.get('name')} {int(m.get('value') or 0):+d} {kind}（{scope}）")
    return out
