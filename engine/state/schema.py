# coding=utf-8
"""`stat_data` 的结构 schema —— **写坏之前拦住，而不是写坏之后在远处崩**。

参考酒馆跑团卡的 MVU/Zod 做法，但只保留我们真正需要的部分：

- 只校验**出现**的字段（模板/旧档允许缺字段，不做"缺了就补"）；
- 类型/范围/枚举不合格 → `StateError`，`commit` 回滚；
- 未知字段放行（角色卡可能带扩展字段，不因此拒写）；
- `SCHEMA_VERSION` 跟存档版本一起走，字段语义变了就 bump。

`世界` 根不在这里管——世界树有自己的校验。
"""

from __future__ import annotations

import re

__all__ = ["SCHEMA_VERSION", "validate_stats"]

SCHEMA_VERSION = 1

#: 属性值：2024 口径 1..30（实际卡面 3..20，留点余量）
_ABILITY_RANGE = (1, 30)
#: 生命值/AC/先攻/速度的合理范围（不是规则上下限，只挡"明显写坏"）
_HP_MAX = 100000
_AC_RANGE = (0, 60)
_INIT_RANGE = (-20, 40)
_SPEED_MAX = 500

_DICE_RE = re.compile(r"^d(4|6|8|10|12|20|100)$", re.I)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_int(problems: list[str], where: str, v, lo: int, hi: int) -> None:
    if not _is_int(v):
        problems.append(f"{where} 必须是整数（拿到 {type(v).__name__}）")
    elif not (lo <= int(v) <= hi):
        problems.append(f"{where} 超出范围 {lo}..{hi}（拿到 {v}）")


def _check_dict(problems: list[str], where: str, v) -> bool:
    if not isinstance(v, dict):
        problems.append(f"{where} 必须是对象（拿到 {type(v).__name__}）")
        return False
    return True


def _check_str(problems: list[str], where: str, v) -> None:
    if not isinstance(v, str):
        problems.append(f"{where} 必须是字符串（拿到 {type(v).__name__}）")


def _validate_character(problems: list[str], name: str, c) -> None:
    base = f"角色列表.{name}"
    if not _check_dict(problems, base, c):
        return

    for key in ("职业",):
        if key in c:
            if _check_dict(problems, f"{base}.{key}", c[key]):
                for k, v in c[key].items():
                    _check_int(problems, f"{base}.{key}.{k}", v, 0, 60)

    if "档案与个性" in c:
        _check_str(problems, f"{base}.档案与个性", c["档案与个性"])

    if "属性" in c and _check_dict(problems, f"{base}.属性", c["属性"]):
        for k, v in c["属性"].items():
            _check_int(problems, f"{base}.属性.{k}", v, *_ABILITY_RANGE)

    if "生命值" in c and _check_dict(problems, f"{base}.生命值", c["生命值"]):
        hp = c["生命值"]
        for k in ("当前", "最大", "临时"):
            if k in hp:
                _check_int(problems, f"{base}.生命值.{k}", hp[k], 0, _HP_MAX)
        if "生命骰" in hp and _check_dict(problems, f"{base}.生命值.生命骰",
                                          hp["生命骰"]):
            hd = hp["生命骰"]
            for k in ("当前", "总数"):
                if k in hd:
                    _check_int(problems, f"{base}.生命值.生命骰.{k}", hd[k],
                               0, 99)
            if "面数" in hd and not _DICE_RE.match(str(hd["面数"])):
                problems.append(
                    f"{base}.生命值.生命骰.面数 必须是 d4/d6/d8/d10/d12/d20/d100"
                    f"（拿到 {hd['面数']!r}）")

    for key, rng in (("护甲等级", _AC_RANGE), ("先攻", _INIT_RANGE)):
        if key in c:
            _check_int(problems, f"{base}.{key}", c[key], *rng)

    if "速度" in c and _check_dict(problems, f"{base}.速度", c["速度"]):
        for k, v in c["速度"].items():
            _check_int(problems, f"{base}.速度.{k}", v, 0, _SPEED_MAX)

    if "当前状态" in c:
        _check_str(problems, f"{base}.当前状态", c["当前状态"])

    if "熟练配置" in c and _check_dict(problems, f"{base}.熟练配置", c["熟练配置"]):
        prof = c["熟练配置"]
        if "技能" in prof and _check_dict(problems, f"{base}.熟练配置.技能",
                                          prof["技能"]):
            for k, v in prof["技能"].items():
                if not (_is_int(v) or isinstance(v, str)):
                    problems.append(
                        f"{base}.熟练配置.技能.{k} 必须是整数或熟练标签"
                        f"（拿到 {type(v).__name__}）")
        for k in ("豁免", "武器", "护甲", "工具"):
            if k in prof and not isinstance(prof[k], (list, tuple)):
                problems.append(
                    f"{base}.熟练配置.{k} 必须是数组"
                    f"（拿到 {type(prof[k]).__name__}）")

    if "物品" in c and _check_dict(problems, f"{base}.物品", c["物品"]):
        for k, v in c["物品"].items():
            if not isinstance(v, dict):
                problems.append(
                    f"{base}.物品.{k} 必须是对象（拿到 {type(v).__name__}）")

    if "动作" in c and _check_dict(problems, f"{base}.动作", c["动作"]):
        # DM 技能（H1）：只挡"明显写坏"；语义校验在 web_actions/homebrew。
        for k, v in c["动作"].items():
            if not _check_dict(problems, f"{base}.动作.{k}", v):
                continue
            for key in ("攻击加值", "DC", "范围", "射程", "次数"):
                if key in v:
                    _check_int(problems, f"{base}.动作.{k}.{key}", v[key], -10, 600)
            for key in ("类型", "动作槽", "伤害骰", "伤害类型", "豁免",
                        "治疗骰", "范围形状", "描述", "来源"):
                if key in v:
                    _check_str(problems, f"{base}.动作.{k}.{key}", v[key])
            if "半伤" in v and not isinstance(v["半伤"], bool):
                problems.append(f"{base}.动作.{k}.半伤 必须是开关值"
                                f"（拿到 {type(v['半伤']).__name__}）")

    if "资源" in c and _check_dict(problems, f"{base}.资源", c["资源"]):
        for k, v in c["资源"].items():
            if isinstance(v, bool):
                continue
            if _is_int(v):
                continue
            if _check_dict(problems, f"{base}.资源.{k}", v):
                for kk in ("current", "max", "当前", "最大"):
                    if kk in v:
                        _check_int(problems, f"{base}.资源.{k}.{kk}", v[kk],
                                   0, _HP_MAX)

    if "施法" in c and _check_dict(problems, f"{base}.施法", c["施法"]):
        cast = c["施法"]
        if "法术位" in cast and _check_dict(problems, f"{base}.施法.法术位",
                                            cast["法术位"]):
            for lv, rec in cast["法术位"].items():
                if not _check_dict(problems, f"{base}.施法.法术位.{lv}", rec):
                    continue
                for kk in ("当前", "最大"):
                    if kk in rec:
                        _check_int(problems, f"{base}.施法.法术位.{lv}.{kk}",
                                   rec[kk], 0, 99)
        if "法术书" in cast and _check_dict(problems, f"{base}.施法.法术书",
                                            cast["法术书"]):
            for k, v in cast["法术书"].items():
                if not isinstance(v, dict):
                    problems.append(
                        f"{base}.施法.法术书.{k} 必须是对象"
                        f"（拿到 {type(v).__name__}）")


def validate_stats(stats: dict) -> list[str]:
    """校验 `stat_data`，返回问题列表（空 = 通过）。**只校验出现字段。**"""
    problems: list[str] = []
    if not isinstance(stats, dict):
        return [f"stat_data 必须是对象（拿到 {type(stats).__name__}）"]
    chars = stats.get("角色列表")
    if chars is not None:
        if not _check_dict(problems, "角色列表", chars):
            return problems
        for name, c in chars.items():
            _validate_character(problems, str(name), c)
    if "当前角色ID" in stats:
        _check_str(problems, "当前角色ID", stats["当前角色ID"])
    return problems