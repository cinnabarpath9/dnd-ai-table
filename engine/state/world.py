# coding=utf-8
"""世界状态：地点 / 派系 / 任务 / 时间。

对应决策记录 §5.17（U4）。**纯本地、零模型调用。**

`stat_data` 原来只有"角色列表"一棵树，跑长战役不够用：玩家走到哪、跟谁结仇、
任务推到哪一步，都必须能存、能查、能按视角渲染。这层就是那棵"世界树"：

    世界.当前地点
    世界.时间.{日期, 时段, 天气}
    世界.地点.<名>.{描述, 区域, 已发现, 已听说, 秘密}
    世界.通道            [{从, 到, 说明, 已知道, 秘密}]
    世界.派系.<名>.{描述, 态度, 已知, 秘密}
    世界.任务.<名>.{状态, 描述, 目标, 备注, 公开, 秘密, 更新轮}
    世界.物品志.<名>.{描述, 机制, 数值, 效果, 背景, 补述者}   # DM 设定，非引擎验算
    世界.法术志.<名>.{...}
    世界.技能志.<名>.{...}

三条纪律：
1. **写操作全部走 `StateTree.commit()`** —— 和角色状态一样，每次改动自动留审计；
2. **秘密只给 DM**：`render("dm")` 带 `秘密`，玩家视角永远不渲染；
3. **未知就是未知**：没发现的地点、没听说的派系不往玩家视角里塞，
   不靠"模型自觉别复述"。
"""

from __future__ import annotations

import copy
from typing import Any

from .patch import escape_token
from .tree import StateTree
from .visibility import DM

__all__ = [
    "World", "WorldError", "WORLD_TEMPLATE", "WORLD_ROOT",
    "QUEST_STATUSES", "FACTION_ATTITUDES", "new_world", "LORE_KINDS",
    "LORE_FIELDS",
]

WORLD_ROOT = "世界"

#: DM 可补述的志类。**只补描述，不改伤害/环阶/熟练等机械数值。**
LORE_KINDS = {"物品": "物品志", "法术": "法术志", "技能": "技能志"}
#: DM 可写的字段：世界书只给框架，缺的数值/机制/效果/背景由 DM 按桌面情况补。
LORE_FIELDS = ("描述", "机制", "数值", "效果", "背景")

QUEST_STATUSES = ("未开始", "进行中", "已完成", "失败", "搁置")
FACTION_ATTITUDES = ("友好", "中立", "敌对", "未知")

#: 线索技能 → 中文（只用于渲染，不引入规则层的依赖）
_CLUE_SKILL_CN = {"perception": "察觉", "investigation": "调查"}

WORLD_TEMPLATE: dict[str, Any] = {
    "当前地点": "",
    "时间": {"日期": "", "时段": "", "天气": ""},
    "地点": {},
    #: 地点之间的通道（U13 世界节点图）：`[{从, 到, 说明, 已知道, 秘密}]`。
    #: **边和点分开存**：地点只管"在哪、去过没"，通道只管"怎么走、玩家知不知道"。
    "通道": [],
    "派系": {},
    "任务": {},
    #: NPC 人格与后台状态（五骰 + 情绪/目标/知情）。**只给 DM 渲染。**
    "角色": {},
    #: 可发现的痕迹（P5-G2）：哪条已发现、谁试过。**只给 DM 渲染**；
    #: 玩家是通过台账/记忆拿到"已经发现的那句话"的，不走世界块。
    "线索": {},
    #: DM 补述（2026-11-06 P1）：不能给世界上每件东西都写百科，缺的描述由 DM
    #: 按世界设定补；只影响"怎么讲"，不影响伤害/环阶/熟练等机械值。
    "物品志": {},
    "法术志": {},
    "技能志": {},
}


class WorldError(ValueError):
    """世界状态的操作不成立（地点没发现、任务不存在、态度非法…）。"""


def new_world(*, current: str = "", date: str = "", period: str = "",
              weather: str = "") -> dict:
    """造一棵空世界树（给 `StateTree.from_template(world=...)` 用）。"""
    w = copy.deepcopy(WORLD_TEMPLATE)
    w["当前地点"] = current
    w["时间"] = {"日期": date, "时段": period, "天气": weather}
    return w


def _wp(*parts: str) -> str:
    return "/" + "/".join(escape_token(x) for x in (WORLD_ROOT, *parts))


def _str(d: dict, key: str, default: str = "") -> str:
    v = d.get(key, default)
    return "" if v is None else str(v)


def _trim(names: list, limit: int) -> tuple[list, str]:
    """长战役里世界清单只增不减 → 只保留最近 N 条，并明说少了多少。"""
    if len(names) <= limit:
        return names, ""
    return names[-limit:], f"（另有 {len(names) - limit} 项更早的未列出）"


class World:
    """状态树上的一层薄封装。**读随便读，写全部走 commit。**"""

    def __init__(self, state: StateTree):
        self.state = state

    # ---------- 根 ----------

    def exists(self) -> bool:
        return isinstance(self.state.read(f"/{WORLD_ROOT}"), dict)

    def ensure(self, *, actor: str = "引擎",
               reason: str = "初始化世界状态") -> "World":
        """没有世界树就建一棵；有但缺子字段就补齐。**不改已有值。**"""
        root = self.state.read(f"/{WORLD_ROOT}")
        if root is not None and not isinstance(root, dict):
            raise WorldError(f"/{WORLD_ROOT} 不是对象（存档坏了？）")
        if root is None:
            self.state.commit(
                [{"op": "insert", "path": f"/{WORLD_ROOT}",
                  "value": copy.deepcopy(WORLD_TEMPLATE)}],
                actor=actor, reason=reason)
            return self

        patches = []
        for key, default in WORLD_TEMPLATE.items():
            if self.state.read(_wp(key)) is None:
                patches.append({"op": "insert", "path": _wp(key),
                                "value": copy.deepcopy(default)})
        if patches:
            self.state.commit(patches, actor=actor, reason=reason)
        return self

    # ---------- 时间 ----------

    def _read_str(self, path: str) -> str:
        v = self.state.read(path, "")
        return "" if v is None else str(v)

    def _names(self, key: str) -> list[str]:
        raw = self.state.read(_wp(key), {})
        if not isinstance(raw, dict):
            return []
        return list(raw)

    @property
    def current_location(self) -> str:
        return self._read_str(_wp("当前地点"))

    @property
    def clock(self) -> dict:
        raw = self.state.read(_wp("时间"), {})
        if not isinstance(raw, dict):
            raw = {}
        return {k: _str(raw, k) for k in ("日期", "时段", "天气")}

    def advance_time(self, *, date: str | None = None, period: str | None = None,
                     weather: str | None = None, actor: str = "DM",
                     reason: str = "推进时间") -> list[str]:
        self.ensure()
        patch = []
        for key, val in (("日期", date), ("时段", period), ("天气", weather)):
            if val is None:
                continue
            if self._read_str(_wp("时间", key)) != str(val):
                patch.append({"op": "replace", "path": _wp("时间", key),
                              "value": str(val)})
        return self.state.commit(patch, actor=actor, reason=reason) if patch else []

    # ---------- 地点 ----------

    def location(self, name: str) -> dict | None:
        v = self.state.read(_wp("地点", name))
        return v if isinstance(v, dict) else None

    def locations(self) -> list[str]:
        return self._names("地点")

    def discovered_locations(self) -> list[str]:
        return [n for n in self.locations()
                if (self.location(n) or {}).get("已发现")]

    def register_location(self, name: str, *, description: str = "",
                          region: str = "", secret: str = "",
                          discovered: bool = False, heard: bool = False,
                          actor: str = "DM",
                          reason: str = "登记地点") -> list[str]:
        """DM 预登记一个地点（可以先不发现，等玩家走到再说）。"""
        self.ensure()
        name = (name or "").strip()
        if not name:
            raise WorldError("地点名不能为空")
        if self.location(name) is not None:
            raise WorldError(f"地点 {name!r} 已经登记过了")
        patch = [{"op": "insert", "path": _wp("地点", name),
                  "value": {"描述": description, "区域": region,
                            "已发现": bool(discovered),
                            "已听说": bool(heard), "秘密": secret}}]
        if discovered and not self.current_location:
            patch.append({"op": "replace", "path": _wp("当前地点"),
                          "value": name})
        return self.state.commit(patch, actor=actor, reason=reason)

    def discover(self, name: str, *, description: str = "", region: str = "",
                 secret: str = "", actor: str = "DM",
                 reason: str = "发现地点") -> list[str]:
        """把一个地点标为已发现；新建时顺手写成当前地点（如果还没有）。"""
        self.ensure()
        name = (name or "").strip()
        if not name:
            raise WorldError("地点名不能为空")
        node = self.location(name)
        patch: list[dict] = []
        if node is None:
            patch.append({"op": "insert", "path": _wp("地点", name),
                          "value": {"描述": description, "区域": region,
                                    "已发现": True, "已听说": False,
                                    "秘密": secret}})
        else:
            if not node.get("已发现"):
                patch.append({"op": "replace",
                              "path": _wp("地点", name, "已发现"), "value": True})
            for field, val in (("描述", description), ("区域", region),
                               ("秘密", secret)):
                if val and val != node.get(field):
                    patch.append({"op": "replace",
                                  "path": _wp("地点", name, field),
                                  "value": val})
        if not self.current_location:
            patch.append({"op": "replace", "path": _wp("当前地点"),
                          "value": name})
        return self.state.commit(patch, actor=actor, reason=reason) if patch else []

    def travel(self, name: str, *, actor: str = "DM",
               reason: str = "移动") -> list[str]:
        """切换当前地点。**只允许去已发现的地点** —— 没发现就是没发现。"""
        self.ensure()
        node = self.location(name)
        if node is None or not node.get("已发现"):
            raise WorldError(f"地点 {name!r} 还没发现，不能直接过去"
                             f"（已发现：{self.discovered_locations()}）")
        if self.current_location == name:
            return []
        return self.state.commit(
            [{"op": "replace", "path": _wp("当前地点"), "value": name}],
            actor=actor, reason=reason)

    # ---------- 听说 / 通道 / 节点图（U13） ----------

    def heard_locations(self) -> list[str]:
        return [n for n in self.locations()
                if (self.location(n) or {}).get("已听说")
                and not (self.location(n) or {}).get("已发现")]

    def hear_about(self, name: str, *, description: str = "", region: str = "",
                   secret: str = "", actor: str = "DM",
                   reason: str = "玩家听说某地") -> list[str]:
        """玩家**听说过**一个地方（有新入口/传闻），但还没去过。

        和 `discover()` 分开：听说过只让它在图谱里显成一个"虚点"，
        **不能 travel 过去**（`travel()` 仍然只认 `已发现`）。
        """
        self.ensure()
        name = (name or "").strip()
        if not name:
            raise WorldError("地点名不能为空")
        node = self.location(name)
        patch: list[dict] = []
        if node is None:
            patch.append({"op": "insert", "path": _wp("地点", name),
                          "value": {"描述": description, "区域": region,
                                    "已发现": False, "已听说": True,
                                    "秘密": secret}})
        elif not node.get("已听说"):
            patch.append({"op": "replace",
                          "path": _wp("地点", name, "已听说"), "value": True})
        return self.state.commit(patch, actor=actor, reason=reason) if patch else []

    def passages(self) -> list[dict]:
        """所有通道（**不筛视角** —— 筛选交给 `graph()` / `render()`）。"""
        raw = self.state.read(_wp("通道"), [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise WorldError("/世界/通道 不是数组（存档坏了？）")
        return [r for r in raw if isinstance(r, dict)]

    def passage(self, a: str, b: str) -> dict | None:
        """两端之间的那条通道（**不分方向**）；没有返回 None。"""
        a, b = (a or "").strip(), (b or "").strip()
        for row in self.passages():
            x, y = str(row.get("从") or ""), str(row.get("到") or "")
            if {x, y} == {a, b}:
                return row
        return None

    def add_passage(self, a: str, b: str, *, note: str = "", known: bool = False,
                    secret: str = "", actor: str = "DM",
                    reason: str = "登记通道") -> list[str]:
        """登记一条通道。**通道是双向的，只存一条**（从/到只是写法）。

        - 两端必须已经登记过（先 `register_location` / `hear_about`）；
        - 不能连到自己、不能重复登记（重复就是数据坏了，不是"多一条路"）；
        - `known=True` 表示**玩家知道有这么条路**（地图上画实线）；
          秘密通道只有 DM 看得到。
        """
        self.ensure()
        a, b = (a or "").strip(), (b or "").strip()
        if not a or not b:
            raise WorldError("通道两端都要有地点名")
        if a == b:
            raise WorldError(f"通道不能把 {a!r} 连到它自己")
        for n in (a, b):
            if self.location(n) is None:
                raise WorldError(f"通道端点 {n!r} 还没登记"
                                 f"（先 register_location / hear_about）")
        if self.passage(a, b) is not None:
            raise WorldError(f"{a!r} 与 {b!r} 之间已经有一条通道了"
                             f"（要改就改那条，别叠一条）")
        rows = self.passages()
        rows.append({"从": a, "到": b, "说明": str(note or ""),
                     "已知道": bool(known), "秘密": str(secret or "")})
        return self.state.commit(
            [{"op": "replace", "path": _wp("通道"), "value": rows}],
            actor=actor, reason=reason)

    def graph(self, viewer: str = DM) -> dict:
        """世界节点图（给网页地图/调试用）。**按视角过滤，迷雾在数据层就砍掉。**

        节点 `state` 三档：`current`（当前）/ `discovered`（去过）/
        `heard`（只听说过）；玩家的未知地点**根本不出现在返回里**。
        边只在两端对这个人可见、且玩家已经知道这条路时才出现。
        """
        dm = viewer == DM
        out = {"viewer": "dm" if dm else "player", "current": "",
               "nodes": [], "edges": [],
               "fog": {"hidden": 0, "heard": 0}}
        if not self.exists():
            return out

        cur = self.current_location
        visible: set[str] = set()
        for name in self.locations():
            node = self.location(name) or {}
            found = bool(node.get("已发现"))
            heard = bool(node.get("已听说"))
            if not dm and not (found or heard):
                continue
            visible.add(name)
            state = ("current" if (name == cur and (dm or found))
                     else "discovered" if found else "heard")
            out["nodes"].append({
                "name": name,
                "region": str(node.get("区域") or ""),
                "state": state,
                "description": str(node.get("描述") or ""),
                **({"secret": str(node.get("秘密") or ""),
                    "discovered": found} if dm else {}),
            })
            if not dm and state == "heard":
                out["fog"]["heard"] += 1

        for row in self.passages():
            a, b = str(row.get("从") or ""), str(row.get("到") or "")
            known = bool(row.get("已知道"))
            if not dm and not known:
                continue
            if a not in visible or b not in visible:
                continue
            edge = {"a": a, "b": b, "note": str(row.get("说明") or ""),
                    "known": known}
            if dm:
                edge["secret"] = str(row.get("秘密") or "")
            out["edges"].append(edge)

        if not dm:
            out["fog"]["hidden"] = len(self.locations()) - len(visible)
        if cur and (dm or cur in visible):
            out["current"] = cur
        return out

    def can_move_to(self, name: str) -> tuple[bool, str]:
        """玩家能不能从当前地点沿**已知通道**走过去。

        返回 `(能不能, 为什么)`。规则：
          · 必须已在某个地点上；
          · 两点之间要有一条**已知道**的通道（秘密通道玩家看不见，也走不了）；
          · 目的地必须是"去过"或"只听说过"（没听说过的连路都不该知道）。
        """
        name = (name or "").strip()
        cur = self.current_location
        if not cur:
            return False, "还不知道自己在哪"
        if not name:
            return False, "没给目的地"
        if name == cur:
            return False, "已经在这里了"
        node = self.location(name)
        if node is None:
            return False, f"没有叫 {name!r} 的地方"
        if not (node.get("已发现") or node.get("已听说")):
            return False, f"{name!r} 还没听说过，走不过去"
        row = self.passage(cur, name)
        if row is None:
            return False, f"从「{cur}」到「{name}」没有路"
        if not row.get("已知道"):
            return False, f"从「{cur}」到「{name}」的路玩家还不知道"
        return True, ""

    def move_to(self, name: str, *, actor: str = "玩家",
                reason: str = "沿通道移动") -> list[str]:
        """沿**已知通道**移动。到达"只听说过"的地点时顺手把它变成"去过"。

        和 `travel()` 的区别：`travel()` 是 DM/剧本的**直接摆放**（读档、进场景），
        可以不去检查通道；`move_to()` 是**玩家自己走**，必须真的有一条已知的路。
        """
        self.ensure()
        ok, why = self.can_move_to(name)
        if not ok:
            raise WorldError(why)
        name = name.strip()
        node = self.location(name) or {}
        patch: list[dict] = [
            {"op": "replace", "path": _wp("当前地点"), "value": name}]
        if not node.get("已发现"):
            patch.append({"op": "replace",
                          "path": _wp("地点", name, "已发现"), "value": True})
        row = self.passage(self.current_location, name) or {}
        if row.get("说明"):
            reason = f"{reason}：{row['说明']}"
        return self.state.commit(patch, actor=actor, reason=reason)

    # ---------- 商店（U12：地点有没有货架） ----------

    def set_shop(self, name: str, items: list[str], *, actor: str = "DM",
                 reason: str = "登记商店") -> list[str]:
        """给一个地点挂货架（`商店` 列表）。**幂等**：内容一样就不写。

        货架只回答"这里卖什么"；价格、钱够不够、给不给货，全在
        `engine/rules/economy.py` 结算。
        """
        self.ensure()
        if self.location(name) is None:
            raise WorldError(f"地点 {name!r} 还没登记，不能挂商店")
        clean = [str(x).strip() for x in (items or []) if str(x).strip()]
        old = self.state.read(_wp("地点", name, "商店"))
        if old == clean:
            return []
        op = "insert" if old is None else "replace"
        return self.state.commit(
            [{"op": op, "path": _wp("地点", name, "商店"), "value": clean}],
            actor=actor, reason=reason)

    def shop_items(self, name: str) -> list[str]:
        node = self.location(name) or {}
        raw = node.get("商店") or []
        return [str(x) for x in raw] if isinstance(raw, list) else []

    # ---------- 派系 ----------

    def faction(self, name: str) -> dict | None:
        v = self.state.read(_wp("派系", name))
        return v if isinstance(v, dict) else None

    def factions(self) -> list[str]:
        return self._names("派系")

    def add_faction(self, name: str, *, attitude: str = "中立",
                    description: str = "", known: bool = False,
                    secret: str = "", actor: str = "DM",
                    reason: str = "登记派系") -> list[str]:
        self.ensure()
        name = (name or "").strip()
        if not name:
            raise WorldError("派系名不能为空")
        if attitude not in FACTION_ATTITUDES:
            raise WorldError(f"态度必须是 {FACTION_ATTITUDES} 之一，"
                             f"拿到 {attitude!r}")
        if self.faction(name) is not None:
            raise WorldError(f"派系 {name!r} 已经登记过了（用 set_attitude 改态度）")
        return self.state.commit(
            [{"op": "insert", "path": _wp("派系", name),
              "value": {"描述": description, "态度": attitude,
                        "已知": bool(known), "秘密": secret}}],
            actor=actor, reason=reason)

    def know_faction(self, name: str, *, actor: str = "DM",
                     reason: str = "玩家得知该派系") -> list[str]:
        node = self.faction(name)
        if node is None:
            raise WorldError(f"派系 {name!r} 不存在")
        if node.get("已知"):
            return []
        return self.state.commit(
            [{"op": "replace", "path": _wp("派系", name, "已知"), "value": True}],
            actor=actor, reason=reason)

    def set_attitude(self, name: str, attitude: str, *, actor: str = "DM",
                     reason: str = "派系态度变化") -> list[str]:
        if attitude not in FACTION_ATTITUDES:
            raise WorldError(f"态度必须是 {FACTION_ATTITUDES} 之一")
        node = self.faction(name)
        if node is None:
            raise WorldError(f"派系 {name!r} 不存在")
        if node.get("态度") == attitude:
            return []
        return self.state.commit(
            [{"op": "replace", "path": _wp("派系", name, "态度"),
              "value": attitude}], actor=actor, reason=reason)

    # ---------- 任务 ----------

    def quest(self, name: str) -> dict | None:
        v = self.state.read(_wp("任务", name))
        return v if isinstance(v, dict) else None

    def quests(self, *, status: str | None = None) -> list[str]:
        names = self._names("任务")
        if status is None:
            return names
        return [n for n in names if (self.quest(n) or {}).get("状态") == status]

    def open_quest(self, name: str, *, description: str = "",
                   objective: str = "", public: bool = True, secret: str = "",
                   round: int = 0, actor: str = "DM",
                   reason: str = "开启任务") -> list[str]:
        self.ensure()
        name = (name or "").strip()
        if not name:
            raise WorldError("任务名不能为空")
        if self.quest(name) is not None:
            raise WorldError(f"任务 {name!r} 已经存在（用 update_quest 推进）")
        return self.state.commit(
            [{"op": "insert", "path": _wp("任务", name),
              "value": {"状态": "进行中", "描述": description, "目标": objective,
                        "备注": "", "公开": bool(public), "秘密": secret,
                        "更新轮": int(round)}}],
            actor=actor, reason=reason)

    def update_quest(self, name: str, *, status: str | None = None,
                     objective: str | None = None, note: str | None = None,
                     public: bool | None = None, round: int = 0,
                     actor: str = "DM", reason: str = "推进任务") -> list[str]:
        node = self.quest(name)
        if node is None:
            raise WorldError(f"任务 {name!r} 不存在")
        if status is not None and status not in QUEST_STATUSES:
            raise WorldError(f"任务状态必须是 {QUEST_STATUSES} 之一，"
                             f"拿到 {status!r}")
        patch: list[dict] = []
        if status is not None and node.get("状态") != status:
            patch.append({"op": "replace", "path": _wp("任务", name, "状态"),
                          "value": status})
        if objective is not None and node.get("目标") != objective:
            patch.append({"op": "replace", "path": _wp("任务", name, "目标"),
                          "value": objective})
        if public is not None and bool(node.get("公开")) != bool(public):
            patch.append({"op": "replace", "path": _wp("任务", name, "公开"),
                          "value": bool(public)})
        if note:
            old = _str(node, "备注")
            new = (old + "\n" if old else "") + str(note)
            patch.append({"op": "replace", "path": _wp("任务", name, "备注"),
                          "value": new})
        if patch and round:
            patch.append({"op": "replace", "path": _wp("任务", name, "更新轮"),
                          "value": int(round)})
        return self.state.commit(patch, actor=actor, reason=reason) if patch else []

    # ---------- NPC 人格与状态（只给 DM） ----------

    def npc(self, name: str) -> dict | None:
        v = self.state.read(_wp("角色", name))
        return v if isinstance(v, dict) else None

    def npcs(self) -> list[str]:
        return self._names("角色")

    def register_npc(self, name: str, *, role: str = "",
                     persona: dict | None = None,
                     state: dict | None = None,
                     actor: str = "DM", reason: str = "登记 NPC") -> list[str]:
        """登记一个 NPC：五骰人格 + 初始后台状态。**重复登记直接报错。**"""
        self.ensure()
        name = (name or "").strip()
        if not name:
            raise WorldError("NPC 名字不能为空")
        if self.npc(name) is not None:
            raise WorldError(f"NPC {name!r} 已经登记过了")
        value = {"身份": str(role or ""),
                 "人格": dict(persona or {}),
                 "状态": dict(state or {}),
                 "已登记": True}
        return self.state.commit(
            [{"op": "insert", "path": _wp("角色", name), "value": value}],
            actor=actor, reason=reason)

    def update_npc_state(self, name: str, state: dict, *,
                         actor: str = "DM",
                         reason: str = "更新 NPC 背景状态") -> list[str]:
        """整块替换 NPC 的后台状态（情绪/目标/知情…），**走 commit 留审计**。"""
        node = self.npc(name)
        if node is None:
            raise WorldError(f"NPC {name!r} 不存在")
        return self.state.commit(
            [{"op": "replace", "path": _wp("角色", name, "状态"),
              "value": dict(state or {})}],
            actor=actor, reason=reason)

    def set_npc_stats(self, name: str, stats: dict, *, actor: str = "DM",
                      reason: str = "设置 NPC 数值") -> list[str]:
        """写入/替换 NPC 的数值块（H2）。**只写结构化数值，人格/状态不动。**"""
        node = self.npc(name)
        if node is None:
            raise WorldError(f"NPC {name!r} 不存在")
        if not isinstance(stats, dict) or not stats:
            raise WorldError("数值块必须是非空对象")
        op = "replace" if isinstance(node.get("数值"), dict) else "insert"
        return self.state.commit(
            [{"op": op, "path": _wp("角色", name, "数值"),
              "value": dict(stats)}],
            actor=actor, reason=reason)

    # ---------- 词条库（H3：DM 自写词条，只在本战役有效） ----------

    def entries(self) -> dict:
        """本战役自写的词条：`{id: 词条}`（空 = 没写过）。"""
        v = self.state.read(_wp("词条库"))
        return dict(v) if isinstance(v, dict) else {}

    def register_entry(self, entry_id: str, data: dict, *, actor: str = "DM",
                       reason: str = "DM 写词条") -> list[str]:
        """写入/覆盖一条本战役词条。**id 不能和内置词条冲突**（在调用方检查）。"""
        eid = str(entry_id or "").strip()
        if not eid:
            raise WorldError("词条 id 不能为空")
        # 内置词条 id 不许被覆盖：调用方漏检时，`_merge` 以前会静默忽略（外审 GLM B 第 15 条）。
        from ..rules.effect_catalog import is_builtin  # 局部导入：state 不反向依赖 rules
        if is_builtin(eid) and eid not in self.entries():
            raise WorldError(f"词条 id「{eid}」是内置词条，不能覆盖；换个 id")
        if not isinstance(data, dict) or not data:
            raise WorldError("词条内容必须是非空对象")
        self.ensure()
        parent = _wp("词条库")
        patches: list[dict] = []
        if self.state.read(parent) is None:
            patches.append({"op": "insert", "path": parent, "value": {}})
        exists = isinstance((self.entries() or {}).get(eid), dict)
        patches.append({"op": "replace" if exists else "insert",
                        "path": _wp("词条库", eid), "value": dict(data)})
        return self.state.commit(patches, actor=actor, reason=reason)

    # ---------- 线索（P5-G2） ----------

    def clue(self, cid: str) -> dict | None:
        v = self.state.read(_wp("线索", cid))
        return v if isinstance(v, dict) else None

    def clues(self) -> list[str]:
        return self._names("线索")

    def register_clue(self, cid: str, *, scene: str = "", place: str = "",
                      skill: str = "", dc: int = 0, text: str = "",
                      secret: bool = True, source: str = "",
                      actor: str = "DM",
                      reason: str = "登记可发现的痕迹") -> list[str]:
        """登记一条线索。**已经登记过就原样返回、不重复写**（重复跑同一场不该重置状态）。"""
        self.ensure()
        cid = (cid or "").strip()
        if not cid:
            raise WorldError("线索 id 不能为空")
        if self.clue(cid) is not None:
            return []
        value = {
            "场景": str(scene or ""),
            "地点": str(place or ""),
            "技能": str(skill or ""),
            "DC": int(dc or 0),
            "描述": str(text or ""),
            "秘密": bool(secret),
            "背后": str(source or ""),
            "已发现": False,
            "发现者": "",
            "发现轮": 0,
            "尝试": [],
        }
        return self.state.commit(
            [{"op": "insert", "path": _wp("线索", cid), "value": value}],
            actor=actor, reason=reason)

    def mark_clue_tried(self, cid: str, who: str, *,
                        scene: str = "", actor: str = "引擎",
                        reason: str = "线索检定失败（记一次尝试）") -> list[str]:
        """记"这个人试过了"。**同一场里同一个人只试一次。**

        `scene` 记"试的时候在哪个场景"：换场 = 条件变了，同一人可以再试一次
        （2026-11-06 P5-G3 的"条件变化可重试"）。不传 scene = 与旧行为一致。
        """
        node = self.clue(cid)
        if node is None:
            raise WorldError(f"线索 {cid!r} 没有登记")
        tried = [str(x) for x in (node.get("尝试") or [])]
        who = (who or "").strip()
        if not who:
            raise WorldError("尝试者不能为空")
        patches: list[dict] = []
        if who not in tried:
            tried.append(who)
            patches.append({"op": "replace", "path": _wp("线索", cid, "尝试"),
                            "value": tried})
        if scene:
            scenes = node.get("尝试场")
            if not isinstance(scenes, dict):
                scenes = {}
            if scenes.get(who) != scene:
                scenes = dict(scenes)
                scenes[who] = str(scene)
                patches.append({"op": "insert", "path": _wp("线索", cid, "尝试场"),
                                "value": scenes})
        if not patches:
            return []
        return self.state.commit(patches, actor=actor, reason=reason)

    def clue_tried_scene(self, cid: str, who: str) -> str:
        """这个人上次是在哪个场景试的这条线索（没记过返回空串）。"""
        node = self.clue(cid) or {}
        scenes = node.get("尝试场")
        if not isinstance(scenes, dict):
            return ""
        return str(scenes.get((who or "").strip()) or "")

    def mark_clue_found(self, cid: str, *, by: str, round: int = 0,
                        actor: str = "引擎",
                        reason: str = "线索检定成功") -> list[str]:
        """标记"已被谁发现"。重复标记返回空清单（不覆盖第一个发现者）。"""
        node = self.clue(cid)
        if node is None:
            raise WorldError(f"线索 {cid!r} 没有登记")
        if node.get("已发现"):
            return []
        by = (by or "").strip()
        if not by:
            raise WorldError("发现者不能为空")
        return self.state.commit([
            {"op": "replace", "path": _wp("线索", cid, "已发现"), "value": True},
            {"op": "replace", "path": _wp("线索", cid, "发现者"), "value": by},
            {"op": "replace", "path": _wp("线索", cid, "发现轮"), "value": int(round)},
        ], actor=actor, reason=reason)

    def found_clues(self) -> list[str]:
        return [c for c in self.clues() if (self.clue(c) or {}).get("已发现")]

    # ---------- DM 补述（P1 物品/法术/技能卡） ----------

    def lore(self, kind: str, name: str) -> dict:
        """读一条 DM 补述。没有就返回空 dict；**不臆造默认描述**。"""
        table = LORE_KINDS.get(str(kind or ""))
        if table is None or not str(name or "").strip():
            return {}
        v = self.state.read(_wp(table, str(name).strip()))
        return dict(v) if isinstance(v, dict) else {}

    def set_lore(self, kind: str, name: str, text: str, *,
                 actor: str = "DM", reason: str = "DM 补述") -> list[str]:
        """写一条"只有描述"的补述（旧调用/单字段快捷方式）。"""
        return self.set_lore_fields(kind, name, {"描述": text},
                                    actor=actor, reason=reason)

    def set_lore_fields(self, kind: str, name: str, fields: dict, *,
                        actor: str = "DM", reason: str = "DM 设定") -> list[str]:
        """写入/清空 DM 设定。**可写描述/机制/数值/效果/背景，不写引擎字段。**

        这些值是**桌面裁定**，会存进世界树并跟存档；引擎不会拿它们去自动
        验算伤害/环阶/熟练。要自动执行，得另做结构化 homebrew，不在这里假装。
        """
        table = LORE_KINDS.get(str(kind or ""))
        if table is None:
            raise WorldError(f"补述类别必须是 {sorted(LORE_KINDS)}，"
                             f"拿到 {kind!r}")
        name = str(name or "").strip()
        if not name:
            raise WorldError("补述对象不能为空")
        if not isinstance(fields, dict):
            raise WorldError("fields 必须是对象")
        clean: dict[str, str] = {}
        for key, val in fields.items():
            if key not in LORE_FIELDS:
                raise WorldError(f"不认识的字段 {key!r}；可用 {LORE_FIELDS}")
            text = str(val or "").strip()
            if len(text) > 2000:
                raise WorldError(f"「{key}」最多 2000 字；世界设定不是百科，写重点")
            if text:
                clean[key] = text
        self.ensure()
        parent = _wp(table)
        child = _wp(table, name)
        patches: list[dict] = []
        if self.state.read(parent) is None:
            patches.append({"op": "insert", "path": parent, "value": {}})
        if not clean:
            if self.state.read(child) is not None:
                patches.append({"op": "remove", "path": child})
        else:
            value = {**clean, "补述者": str(actor or "DM")}
            op = "insert" if self.state.read(child) is None else "replace"
            patches.append({"op": op, "path": child, "value": value})
        if not patches:
            return []
        return self.state.commit(patches, actor=actor, reason=reason)

    def lore_all(self, kind: str) -> dict:
        """某一类全部补述（给测试/后台看；页面按卡名单取）。"""
        table = LORE_KINDS.get(str(kind or ""))
        if table is None:
            return {}
        v = self.state.read(_wp(table))
        return dict(v) if isinstance(v, dict) else {}

    def clue_tried_by(self, cid: str) -> list[str]:
        node = self.clue(cid) or {}
        return [str(x) for x in (node.get("尝试") or [])]

    # ---------- 渲染 ----------

    def render(self, viewer: str = DM) -> str:
        """按视角渲染。**秘密永远不出现在非 DM 视角。**"""
        if not self.exists():
            return ""
        dm = viewer == DM
        lines: list[str] = []

        clock = self.clock
        bits = [v for v in (clock["日期"], clock["时段"]) if v]
        if bits or clock["天气"]:
            s = "【时间】" + "·".join(bits)
            if clock["天气"]:
                s += f"｜天气 {clock['天气']}"
            lines.append(s)

        cur = self.current_location
        locs = self.locations()
        cur_visible = bool(cur) and (dm or bool((self.location(cur) or {}).get("已发现")))
        if cur_visible:
            node = self.location(cur) or {}
            head = f"【当前地点】{cur}"
            if node.get("区域"):
                head += f"（{node['区域']}）"
            lines.append(head)
            if node.get("描述"):
                lines.append("  " + str(node["描述"]))
            if dm and node.get("秘密"):
                lines.append("  🔒秘密：" + str(node["秘密"]))
        elif dm and not locs:
            lines.append("【当前地点】（空）")

        shown = locs if dm else [
            n for n in locs
            if (self.location(n) or {}).get("已发现")
            or (self.location(n) or {}).get("已听说")]
        shown, shown_note = _trim(shown, 20)
        if shown:
            lines.append("【地点】")
            for n in shown:
                node = self.location(n) or {}
                if node.get("已发现"):
                    tag = ""
                elif not dm and node.get("已听说"):
                    tag = "（只听说过）"
                else:
                    tag = "（未发现）"
                s = f"  · {n}{tag}"
                if node.get("区域"):
                    s += f"（{node['区域']}）"
                if node.get("描述"):
                    s += f"：{node['描述']}"
                lines.append(s)
                if dm and node.get("秘密"):
                    lines.append("    🔒秘密：" + str(node["秘密"]))
            if shown_note:
                lines.append("  " + shown_note)

        # 通道（U13 节点图的文字版）：玩家只看到"已经知道、且两端都可见"的路。
        if dm:
            visible_nodes = set(locs)
        else:
            visible_nodes = {n for n in locs
                             if (self.location(n) or {}).get("已发现")
                             or (self.location(n) or {}).get("已听说")}
        rows, rows_note = _trim(self.passages(), 12)
        edge_lines: list[str] = []
        for row in rows:
            a, b = str(row.get("从") or ""), str(row.get("到") or "")
            known = bool(row.get("已知道"))
            if not dm and (not known or a not in visible_nodes
                           or b not in visible_nodes):
                continue
            s = f"  · {a} ↔ {b}"
            if row.get("说明"):
                s += f"：{row['说明']}"
            if dm and not known:
                s += "（玩家还不知道这条路）"
            edge_lines.append(s)
            if dm and row.get("秘密"):
                edge_lines.append("    🔒秘密：" + str(row["秘密"]))
        if edge_lines:
            lines.append("【通道】")
            lines.extend(edge_lines)
            if rows_note:
                lines.append("  " + rows_note)

        fs = self.factions() if dm else [
            n for n in self.factions() if (self.faction(n) or {}).get("已知")]
        fs, fs_note = _trim(fs, 20)
        if fs:
            lines.append("【派系】")
            for n in fs:
                node = self.faction(n) or {}
                s = f"  · {n}（{node.get('态度', '未知')}"
                if not node.get("已知"):
                    s += "，玩家未知" if dm else "，未知"
                s += "）"
                if node.get("描述"):
                    s += f"：{node['描述']}"
                lines.append(s)
                if dm and node.get("秘密"):
                    lines.append("    🔒秘密：" + str(node["秘密"]))
            if fs_note:
                lines.append("  " + fs_note)

        qs = self.quests() if dm else [
            n for n in self.quests() if (self.quest(n) or {}).get("公开")]
        qs, qs_note = _trim(qs, 20)
        if qs:
            lines.append("【任务】")
            for n in qs:
                node = self.quest(n) or {}
                s = f"  · {n}（{node.get('状态', '未知')}）"
                if node.get("目标"):
                    s += f"｜目标：{node['目标']}"
                lines.append(s)
                if node.get("描述"):
                    lines.append("    " + str(node["描述"]))
                if dm and node.get("备注"):
                    lines.append("    备注：" + str(node["备注"]).replace("\n", "；"))
                if dm and node.get("秘密"):
                    lines.append("    🔒秘密：" + str(node["秘密"]))
            if qs_note:
                lines.append("  " + qs_note)

        # NPC 人格/后台状态：**只给 DM**（玩家只看到行为，不看到内心）
        if dm:
            chars = self.state.read(_wp("角色"), {}) or {}
            if isinstance(chars, dict) and chars:
                rows = list(chars.items())
                npc_note = ""
                if len(rows) > 12:
                    npc_note = f"（另有 {len(rows) - 12} 位更早的未列出）"
                    rows = rows[-12:]
                lines.append("【NPC · 仅 DM】")
                if npc_note:
                    lines.append("  " + npc_note)
                for n, node in rows:
                    if not isinstance(node, dict):
                        continue
                    lines.append(f"  · {n}（{node.get('身份') or '身份未定'}）")
                    p = node.get("人格") or {}
                    for key in ("核心动机", "行为面具", "暗面破绽",
                                "破绽触发", "核心欲望", "核心恐惧", "矛盾线"):
                        if p.get(key):
                            lines.append(f"    {key}：{p[key]}")
                    st = node.get("状态") or {}
                    head = f"    情绪 {st.get('情绪') or '平静'}" \
                           f"({st.get('强度', 30)})"
                    if st.get("短期目标"):
                        head += f"｜短期目标：{st['短期目标']}"
                    if st.get("底线"):
                        head += f"｜底线：{st['底线']}"
                    lines.append(head)
                    known = [x.get("fact") for x in (st.get("知情") or [])
                             if isinstance(x, dict) and x.get("state") == "已知"]
                    wrong = [x.get("fact") for x in (st.get("知情") or [])
                             if isinstance(x, dict) and x.get("state") == "误解"]
                    if known:
                        lines.append("    已知：" + "；".join(str(x) for x in known))
                    if wrong:
                        lines.append("    误解：" + "；".join(str(x) for x in wrong))
                    if st.get("秘密"):
                        lines.append("    🔒秘密：" + "；".join(
                            str(x) for x in st["秘密"]))

        # 可发现的痕迹：**只给 DM**。玩家是从台账/记忆拿到"已经发现的那句话"的，
        # 未发现的那部分连"存在"都不该出现在玩家视角里（含失败时）。
        if dm:
            cids = self.clues()
            if cids:
                done = self.found_clues()
                # ⚠️ 只列**最近** 6 条：线索是只加不减的，整本列进世界块会让
                #    DM 的包随场次线性膨胀（P5-B2 实测 12 场 1.1k → 3.5k tok）。
                #    更早的痕迹已经进了台账/记忆，需要时从那里看。
                shown = cids[-6:]
                head = f"【线索 · 仅 DM】（共 {len(cids)} 条，已发现 {len(done)}"
                if len(cids) > len(shown):
                    head += f"；只列最近 {len(shown)} 条，更早的见台账/记忆"
                lines.append(head + "）")
                for cid in shown:
                    node = self.clue(cid) or {}
                    skill = _CLUE_SKILL_CN.get(str(node.get("技能") or ""),
                                               str(node.get("技能") or "?"))
                    head = f"  · {cid}［{skill} DC {node.get('DC') or '?'}｜"
                    if node.get("已发现"):
                        head += f"已被 {node.get('发现者') or '?'} 发现"
                    else:
                        head += "未发现"
                        tried = self.clue_tried_by(cid)
                        if tried:
                            head += "；试过：" + "、".join(tried)
                    lines.append(head + "］" + (
                        "：" + str(node["描述"]) if node.get("描述") else ""))
                    if node.get("背后"):
                        lines.append("    背后：" + str(node["背后"]))

        return "\n".join(lines)


def render_world(state: StateTree, viewer: str = DM) -> str:
    """一行拿世界渲染（没有世界树就返回空串）。"""
    return World(state).render(viewer)
