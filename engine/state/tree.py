# coding=utf-8
"""`stat_data` 状态树 —— 照 MVU 那棵树的结构，但**"谁算数"换成了代码**。

对应决策记录 §2：

    MVU 那棵树（本项目照抄当 schema）：
        角色列表.<角色名>.属性/生命值/护甲等级/先攻/速度/当前状态/职业/档案与个性
        角色列表.<角色名>.熟练配置.{技能,豁免,武器,护甲,工具}
        角色列表.<角色名>.资源.<资源名>       {当前值, 最大值, 恢复规则}
        角色列表.<角色名>.物品.<物品名>       {类别, 已装备, 伤害公式, 伤害类型, 描述}
        角色列表.<角色名>.特性.<来源>.<特性名> {描述, 来源}
        角色列表.<角色名>.施法.法术位.<环阶>   {当前, 最大}
        角色列表.<角色名>.施法.法术书.<法术名> {等级, 准备中, 学派, 成分, 描述}
        当前角色ID

    但 MVU 是"模型算 → 模型写 patch"；本项目是
        **"模型出意图 → 代码算 → 代码写 patch → 落库"**。

两条设计纪律（都是从 MVU 的世界书里学来、但加强了）：

    ① **只写变化**：改动只能通过 `commit(patch)` 落库，每次落库自动记一条审计
       （谁改的、为什么、改前改后）。这样"模型忘了扣法术位"这类问题能一眼查出来 ——
       MVU 靠提示词祈祷模型守规矩，我们靠审计。

    ② **下划线开头只读**：界面配置、备份、脚本存在树里但 patch 碰不到（见 patch.py）。

    `stats` 是**给模型看的那棵树**；`ui` 是界面配置，**不进制模型的包**（MVU 世界书
    也强调了这一点：不修改界面配置、备份、脚本或下划线开头的只读字段）。
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .patch import (
    Op, PatchError, apply_patch, diff, escape_token, parse_path, unescape_token,
)
from .schema import validate_stats

__all__ = [
    "StateError", "StateTree", "AuditEntry", "init_from_template",
    "new_character", "CHARACTER_TEMPLATE", "EMPTY_STAT_DATA",
]


class StateError(Exception):
    """状态树本身的问题（路径写错、模板非法…）。"""


# --------------------------------------------------------------------------- #
# 模板
# --------------------------------------------------------------------------- #

#: 空状态树：只有"角色列表"和"当前角色ID"两个根
EMPTY_STAT_DATA: dict[str, Any] = {"角色列表": {}, "当前角色ID": ""}

#: 一个角色的精简模板（字段名与 MVU 世界书一致，值是 2024 口径）
CHARACTER_TEMPLATE: dict[str, Any] = {
    "职业": {},                       # {战士: 3}
    "档案与个性": "",
    "属性": {},                       # {力量:16, 敏捷:14, ...}
    "生命值": {"当前": 0, "最大": 0, "临时": 0,
              "生命骰": {"当前": 0, "总数": 0, "面数": "d8"}},
    "护甲等级": 10,
    "先攻": 0,
    "速度": {"步行": 30},
    "当前状态": "",
    "熟练配置": {"技能": {}, "豁免": [], "武器": [], "护甲": [], "工具": []},
    "资源": {},
    "物品": {},
    #: U12 钱袋：付款只扣金币，银/铜先当零钱记账（换算见 rules/economy.py）
    "钱币": {"金币": 0, "银币": 0, "铜币": 0},
    "特性": {},
    "施法": {"法术位": {}, "法术书": {}},
}

#: 路径（相对 stat_data）
P_CHARACTERS = "/角色列表"
P_CURRENT = "/当前角色ID"


def _p(name: str, *rest: str) -> str:
    parts = ["角色列表", name, *rest]
    return "/" + "/".join(escape_token(x) for x in parts)


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #

@dataclass
class AuditEntry:
    """一次状态改动。**这是"模型说了算"和"代码说了算"的分界线证据。**"""
    seq: int
    actor: str                  # 谁触发的：的是"引擎回合"、"填表模型"、"DM 裁定"…
    reason: str                 # 为什么改
    patch: list[dict]           # 实际应用的 patch
    changes: list[str] = field(default_factory=list)   # 人话摘要
    at: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def line(self) -> str:
        c = "；".join(self.changes) if self.changes else "（无实际变化）"
        return f"#{self.seq} [{self.actor}] {self.reason} —— {c}"


# --------------------------------------------------------------------------- #
# 状态树
# --------------------------------------------------------------------------- #

class StateTree:
    """一份 `stat_data`。

    典型用法：
        st = StateTree.from_template({"艾尔玛": {...}, "罗恩": {...}})
        st.commit([{"op":"delta","path":"/角色列表/艾尔玛/生命值/当前","value":-8}],
                  actor="引擎", reason="地精弯刀命中")
        st.read("/角色列表/艾尔玛/生命值/当前")     # → 12
        st.snapshot()                              # → 不可变快照（可 json 序列化）
    """

    def __init__(self, stat_data: dict | None = None, *,
                 ui: dict | None = None, label: str = ""):
        if stat_data is None:
            stat_data = copy.deepcopy(EMPTY_STAT_DATA)
        self.stats: dict[str, Any] = copy.deepcopy(stat_data)
        #: 界面配置。**不进模型上下文**；patch 不许碰（下划线前缀 + 这里隔离）
        self.ui: dict[str, Any] = copy.deepcopy(ui or {})
        self.label = label
        self._audit: list[AuditEntry] = []
        self._seq = 0

    # ---------- 构造 ----------

    @classmethod
    def from_template(cls, characters: dict[str, dict] | None = None, *,
                      current: str = "", ui: dict | None = None,
                      label: str = "",
                      world: dict | None = None) -> "StateTree":
        st = cls(ui=ui, label=label)
        for name, body in (characters or {}).items():
            st.stats["角色列表"][name] = cls._fill(body)
        if current:
            st.stats["当前角色ID"] = current
        if world is not None:
            if not isinstance(world, dict):
                raise StateError("world 必须是对象")
            st.stats["世界"] = copy.deepcopy(world)
        return st

    @staticmethod
    def _fill(body: dict) -> dict:
        """用模板补齐缺的字段。**只补结构，不编数值。**"""
        out = copy.deepcopy(CHARACTER_TEMPLATE)
        for k, v in (body or {}).items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = {**out[k], **v}
            else:
                out[k] = copy.deepcopy(v)
        return out

    # ---------- 读（只读，不产生审计） ----------

    def read(self, path: str, default: Any = None) -> Any:
        try:
            toks = parse_path(path)
        except PatchError as exc:
            raise StateError(str(exc)) from None
        cur: Any = self.stats
        for i, t in enumerate(toks):
            if isinstance(cur, dict):
                if t not in cur:
                    return default
                cur = cur[t]
            elif isinstance(cur, list):
                try:
                    cur = cur[int(t)]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return cur

    def characters(self) -> list[str]:
        return list(self.read(P_CHARACTERS, {}) or {})

    def has_character(self, name: str) -> bool:
        return name in (self.read(P_CHARACTERS, {}) or {})

    def hp(self, name: str) -> tuple[int, int, int]:
        """(当前, 最大, 临时)"""
        b = self.read(_p(name, "生命值"), {}) or {}
        return (int(b.get("当前", 0)), int(b.get("最大", 0)), int(b.get("临时", 0)))

    def spell_slots(self, name: str) -> dict[int, tuple[int, int]]:
        """{环阶: (当前, 最大)}"""
        raw = self.read(_p(name, "施法", "法术位"), {}) or {}
        out: dict[int, tuple[int, int]] = {}
        for lv, v in raw.items():
            try:
                out[int(lv)] = (int(v.get("当前", 0)), int(v.get("最大", 0)))
            except (AttributeError, ValueError):
                continue
        return out

    def is_alive(self, name: str) -> bool:
        cur, mx, _t = self.hp(name)
        return cur > 0

    # ---------- 写（唯一入口：commit patch） ----------

    def commit(self, patch: list[dict] | None, *, actor: str, reason: str,
               strict: bool = True) -> list[str]:
        """应用一份 patch 并记审计。返回"人话变化摘要"。

        **这就是唯一的写入口。** 想让状态变，就得走 patch —— 于是每一次改动
        都自动带来一条"谁改的、为什么、改了多少"的记录。
        """
        patch = list(patch or [])
        if not patch:
            return []
        before = copy.deepcopy(self.stats)
        try:
            apply_patch(self.stats, patch, in_place=True, strict=strict)
        except PatchError:
            # apply_patch 是逐条改 in_place 的，失败时**手动回滚**（保住事务语义）
            self.stats = before
            raise
        changes = _describe(before, self.stats, patch)
        problems = validate_stats(self.stats)
        if problems:
            # schema 是最后一道闸：写坏了就整体回滚，别让 None/字符串漏进树里。
            self.stats = before
            raise StateError("状态不符合 schema：" + "；".join(problems[:6]))
        self._seq += 1
        self._audit.append(AuditEntry(seq=self._seq, actor=actor, reason=reason,
                                      patch=copy.deepcopy(patch), changes=changes))
        return changes

    def commit_diff(self, new_stats: dict, *, actor: str, reason: str) -> list[str]:
        """把整棵树设成 `new_stats`，但**自动生成最小的 patch** 再落库。

        给"代码算完一堆改动"的场景用：改完传整棵树进来，patch 由 diff 生成，
        审计里看到的仍然是"具体变了哪几个字段"，而不是"整棵树被覆盖"。
        """
        patch = diff(self.stats, new_stats)
        return self.commit(patch, actor=actor, reason=reason)

    # ---------- 审计 ----------

    @property
    def audit(self) -> list[AuditEntry]:
        return list(self._audit)

    def audit_text(self, limit: int | None = None) -> str:
        rows = self._audit if limit is None else self._audit[-limit:]
        return "\n".join(e.line() for e in rows)

    # ---------- 快照 / 序列化 ----------

    def snapshot(self) -> dict:
        """不可变快照（深拷贝），可以用来做"同种子复算"的基线。"""
        return copy.deepcopy(self.stats)

    def restore(self, snap: dict, *, actor: str = "系统",
                reason: str = "回滚到动作前") -> bool:
        """把整棵树还原成 `snap`。**返回是否真的改了东西。**

        - 快照必须是一棵合法的 `stat_data`（防止把坏档当快照）；
        - 与当前完全一样 → 什么都不做、**不记审计**（普通校验失败不该刷日志）；
        - 真的回滚了 → 记一条 `patch=[]` 的审计，写明恢复了几处字段。

        ⚠️ 只保证**状态树**。内存里的 `Unit`（战斗 HP/资源）、台账、记忆库
        不在树里，调用方要自己决定是否重建/重同步（网页动作有 `_resync_pc_units`）。
        """
        if not isinstance(snap, dict):
            raise StateError(f"快照必须是对象，拿到 {type(snap).__name__}")
        problems = validate_stats(snap)
        if problems:
            raise StateError("快照本身不符合 schema："
                             + "；".join(problems[:6]))
        if self.stats == snap:
            return False
        before = copy.deepcopy(self.stats)
        self.stats = copy.deepcopy(snap)
        moved = len(diff(before, self.stats))
        self._seq += 1
        self._audit.append(AuditEntry(
            seq=self._seq, actor=actor, reason=reason, patch=[],
            changes=[f"整体回滚：{moved} 处字段恢复原值"]))
        return True

    @contextmanager
    def transaction(self, *, actor: str = "系统", reason: str = "一个动作"):
        """动作级事务：块内抛异常 → 整棵树回到块前，再原样抛出。

            with st.transaction(actor="玩家:甲", reason="买药"):
                ... 可能 commit 好几次 ...
        """
        snap = self.snapshot()
        try:
            yield self
        except BaseException as exc:
            try:
                self.restore(snap, actor=actor,
                             reason=f"「{reason}」失败，整体回滚")
            except Exception as rexc:              # 回滚都失败：不能掩盖原始异常
                raise StateError(f"回滚失败（原始异常：{exc}）：{rexc}") from exc
            raise

    def to_dict(self, *, include_audit: bool = False) -> dict:
        """导出状态树。

        `include_audit=True` 时把审计流水也带上（存档用）。默认不带 ——
        保持老的"纯 stat_data 快照"口径，避免把日志塞进给模型看的数据里。
        """
        d = {"label": self.label, "stat_data": copy.deepcopy(self.stats),
             "ui": copy.deepcopy(self.ui)}
        if include_audit:
            d["audit"] = [
                {"seq": e.seq, "actor": e.actor, "reason": e.reason,
                 "patch": copy.deepcopy(e.patch), "changes": list(e.changes),
                 "at": e.at}
                for e in self._audit]
            d["audit_seq"] = self._seq
        return d

    def to_json(self, *, indent: int | None = None, ensure_ascii: bool = False) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=ensure_ascii, indent=indent,
                          sort_keys=False)

    @classmethod
    def from_dict(cls, d: dict) -> "StateTree":
        if not isinstance(d, dict):
            raise StateError(f"状态树存档必须是对象，拿到的是 {type(d).__name__}")
        raw = d.get("stat_data")
        if raw is not None and not isinstance(raw, dict):
            raise StateError(f"stat_data 必须是对象，拿到的是 {type(raw).__name__}")
        st = cls(raw, ui=d.get("ui"), label=d.get("label", ""))
        for row in (d.get("audit") or []):
            if not isinstance(row, dict) or "seq" not in row:
                raise StateError(f"存档里的审计条目格式不对：{row!r}")
            try:
                st._audit.append(AuditEntry(
                    seq=int(row["seq"]),
                    actor=str(row.get("actor", "")),
                    reason=str(row.get("reason", "")),
                    patch=copy.deepcopy(list(row.get("patch") or [])),
                    changes=[str(x) for x in (row.get("changes") or [])],
                    at=str(row.get("at", "")),
                ))
            except (TypeError, ValueError) as exc:
                raise StateError(f"存档里的审计条目格式不对：{row!r}") from exc
        if st._audit:
            st._seq = max(e.seq for e in st._audit)
        saved_seq = d.get("audit_seq")
        if isinstance(saved_seq, int) and saved_seq > st._seq:
            st._seq = saved_seq
        return st

    # ---------- 给模型看的渲染 ----------

    def render(self, name: str, *, include_spells: bool = True) -> str:
        """把一个角色的状态渲染成**给模型看**的文本块。

        不是整棵树 —— 只渲染这个人自己的、且这份场景需要的字段。
        （对应决策记录 §4："战斗中不需要整张角色卡"。）
        """
        if not self.has_character(name):
            raise StateError(f"角色列表里没有 {name!r}；现有：{self.characters()}")
        c = self.read(_p(name), {}) or {}
        L: list[str] = [f"【{name}】"]

        jobs = c.get("职业") or {}
        if jobs:
            L.append("职业：" + "、".join(f"{k} {v} 级" for k, v in jobs.items()))
        ab = c.get("属性") or {}
        if ab:
            from ..rules.constants import ability_modifier as AM
            L.append("属性：" + "、".join(
                f"{k} {v}({AM(int(v)):+d})" for k, v in ab.items()))
        hp = c.get("生命值") or {}
        hp_line = f"生命值 {hp.get('当前', 0)}/{hp.get('最大', 0)}"
        if hp.get("临时"):
            hp_line += f"（临时 {hp.get('临时')}）"
        hd = hp.get("生命骰") or {}
        if hd.get("总数"):
            hp_line += f"｜生命骰 {hd.get('当前')}/{hd.get('总数')}{hd.get('面数','')}"
        L.append(hp_line)

        ac, init = c.get("护甲等级"), c.get("先攻")
        sp = c.get("速度") or {}
        bits = []
        if ac is not None:
            bits.append(f"护甲等级 {ac}")
        if init is not None:
            bits.append(f"先攻 {int(init):+d}" if init else "先攻 +0")
        if sp:
            bits.append("速度 " + "/".join(f"{k} {v}" for k, v in sp.items()))
        if bits:
            L.append("｜".join(bits))

        st = c.get("当前状态")
        if st:
            L.append(f"当前状态：{st}")

        prof = c.get("熟练配置") or {}
        if prof.get("技能"):
            L.append("技能：" + "、".join(f"{k}({v})" for k, v in prof["技能"].items()))
        if prof.get("豁免"):
            L.append("豁免熟练：" + "、".join(prof["豁免"]))
        other = [f"{k}：{'、'.join(v)}" for k, v in prof.items()
                 if k not in ("技能", "豁免") and v]
        if other:
            L.append("｜".join(other))

        for rname, rv in (c.get("资源") or {}).items():
            if isinstance(rv, dict):
                L.append(f"资源「{rname}」{rv.get('当前值')}/{rv.get('最大值')}"
                         + (f"（{rv.get('恢复规则')}恢复）" if rv.get("恢复规则") else ""))

        items = c.get("物品") or {}
        if items:
            shown = []
            for iname, iv in items.items():
                if isinstance(iv, dict):
                    tag = "已装备" if iv.get("已装备") else "未装备"
                    dmg = f" {iv.get('伤害公式')} {iv.get('伤害类型','')}".rstrip()
                    shown.append(f"{iname}({tag}{dmg})")
                else:
                    shown.append(str(iname))
            L.append("物品：" + "、".join(shown))

        if include_spells:
            sp_ = c.get("施法") or {}
            slots = sp_.get("法术位") or {}
            if slots:
                L.append("法术位：" + "、".join(
                    f"{lv} 环 {v.get('当前')}/{v.get('最大')}"
                    for lv, v in sorted(slots.items(), key=lambda kv: str(kv[0]))))
            book = sp_.get("法术书") or {}
            if book:
                prepared = [k for k, v in book.items()
                            if isinstance(v, dict) and v.get("准备中")]
                known = list(book)
                L.append(f"法术书 {len(known)} 条；已准备："
                         + ("、".join(prepared) if prepared else "（无）"))

        feats = c.get("特性") or {}
        if feats:
            lines = []
            for src, group in feats.items():
                if isinstance(group, dict):
                    lines.append(f"{src}：" + "、".join(group))
            if lines:
                L.append("特性：" + "；".join(lines))
        return "\n".join(L)

    def render_all(self, *, include_spells: bool = True) -> str:
        return "\n\n".join(self.render(n, include_spells=include_spells)
                           for n in self.characters())

    # ---------- 校验 ----------

    def validate(self) -> list[str]:
        """结构自检。返回问题清单（空 = 没问题）。

        **校验的是结构，不是数值合理性** —— 数值合理性由规则引擎负责
        （比如"当前 HP 不该超过最大 HP"是结算的职责，不是状态树的）。
        """
        problems: list[str] = []
        if not isinstance(self.stats, dict):
            return ["stat_data 根不是对象"]
        if "角色列表" not in self.stats:
            problems.append("缺「角色列表」根字段")
        cur = self.stats.get("当前角色ID", "")
        chars = self.stats.get("角色列表")
        if not isinstance(chars, dict):
            problems.append("「角色列表」不是对象")
            chars = {}
        if cur and cur not in chars:
            problems.append(f"「当前角色ID」指向不存在的角色：{cur!r}")
        for name, body in chars.items():
            if not isinstance(body, dict):
                problems.append(f"角色 {name!r} 不是对象")
                continue
            for k in ("生命值", "属性", "速度"):
                if k not in body:
                    problems.append(f"角色 {name!r} 缺 {k}")
            hp = body.get("生命值")
            if isinstance(hp, dict) and "当前" not in hp:
                problems.append(f"角色 {name!r} 的生命值缺「当前」")
            for k in body:
                if k.startswith("_"):
                    problems.append(f"角色 {name!r} 有下划线字段 {k!r}（只读字段不该在 stat_data 里）")

        # 世界树（可选）：结构 + 当前地点的一致性
        world = self.stats.get("世界")
        if world is not None:
            if not isinstance(world, dict):
                problems.append("「世界」不是对象")
            else:
                for key in ("地点", "派系", "任务"):
                    if key in world and not isinstance(world[key], dict):
                        problems.append(f"世界.{key} 不是对象")
                locs = world.get("地点")
                cur_loc = world.get("当前地点")
                if cur_loc and isinstance(cur_loc, str):
                    if not isinstance(locs, dict) or cur_loc not in locs:
                        problems.append(f"当前地点 {cur_loc!r} 不在地点表里")
                    elif isinstance(locs.get(cur_loc), dict) \
                            and not locs[cur_loc].get("已发现"):
                        problems.append(f"当前地点 {cur_loc!r} 还没发现")

        for f in self.ui:
            if not f.startswith("_"):
                problems.append(f"ui 里的 {f!r} 建议用下划线开头，好跟 stat_data 区分")
        return problems


# --------------------------------------------------------------------------- #
# 变化摘要（给人看的人话）
# --------------------------------------------------------------------------- #

def _describe(before: Any, after: Any, patch: list[dict]) -> list[str]:
    """把 patch 翻成"X 从 A 变成 B"这样的人话。"""
    out: list[str] = []
    for d in patch:
        try:
            o = Op.from_dict(d)
        except PatchError:
            continue
        p = o.path
        try:
            old = _read_path(before, parse_path(p))
        except Exception:                       # noqa: BLE001
            old = None
        try:
            new = _read_path(after, parse_path(p))
        except Exception:                       # noqa: BLE001
            new = None
        short = _short(p)
        if o.op == "delta":
            out.append(f"{short} {_fmt(old)} → {_fmt(new)}（{o.value:+}）"
                       if isinstance(o.value, (int, float)) else f"{short} 变化")
        elif o.op == "remove":
            out.append(f"删除 {short}")
        elif old is None:
            out.append(f"新增 {short} = {_fmt(new)}")
        else:
            out.append(f"{short} {_fmt(old)} → {_fmt(new)}")
    return out


def _read_path(doc: Any, toks: list[str]) -> Any:
    cur = doc
    for t in toks:
        if isinstance(cur, dict):
            cur = cur[t]
        elif isinstance(cur, list):
            cur = cur[int(t)]
        else:
            return None
    return cur


def _short(path: str) -> str:
    toks = [unescape_token(x) for x in path.lstrip("/").split("/")]
    if len(toks) >= 3 and toks[0] == "角色列表":
        return f"{toks[1]}·" + "·".join(toks[2:])
    return "·".join(toks)


def _fmt(v: Any) -> str:
    if isinstance(v, str):
        return v if len(v) <= 24 else v[:24] + "…"
    if isinstance(v, dict):
        return "{…}" if len(v) > 3 else json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ",".join(_fmt(x) for x in v[:4]) + ("…" if len(v) > 4 else "") + "]"
    return str(v)


# --------------------------------------------------------------------------- #
# 便捷构造
# --------------------------------------------------------------------------- #

def new_character(name: str, *, level: int = 1, job: str = "战士",
                  abilities: dict[str, int] | None = None,
                  hp_max: int | None = None, ac: int = 10,
                  speed: int = 30, **extra: Any) -> dict:
    """造一个角色的 stat_data 片段。

    ⚠️ **不在状态层做规则计算**（HP 上限、AC 该多少是规则引擎的事）。
    这里只负责"把给定的数值放进正确的位置"，缺的字段留空等填表补。
    """
    from ..rules.constants import ability_modifier as AM

    ab = dict(abilities or {})
    body = copy.deepcopy(CHARACTER_TEMPLATE)
    body["职业"] = {job: int(level)} if job else {}
    body["属性"] = ab
    hp = int(hp_max if hp_max is not None else max(1, 8 + AM(ab.get("体质", 10))))
    body["生命值"] = {"当前": hp, "最大": hp, "临时": 0,
                     "生命骰": {"当前": int(level), "总数": int(level),
                                "面数": "d10" if job in ("战士", "圣骑士") else "d8"}}
    body["护甲等级"] = int(ac)
    body["先攻"] = AM(ab.get("敏捷", 10))
    body["速度"] = {"步行": int(speed)}
    for k, v in extra.items():
        body[k] = v
    return body


def init_from_template(characters: dict[str, dict] | None = None, *,
                       current: str = "") -> StateTree:
    return StateTree.from_template(characters, current=current)
